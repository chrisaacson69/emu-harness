"""Sea-16 VM SEMANTIC / STACK-EFFECT audit — Rung 2 of validating the VM (Pass 2 / M2).

Rung 1 (`vm-roundtrip.py`) proved the bytes round-trip — the encoding model is complete.
But bytes can round-trip while the *meaning* is wrong: the Pass-1 `$30-$3F` bug modeled
"PUSH a frame slot" as "store regB to a slot", which round-trips fine yet corrupted 996
decompiled sites. Rung 2 closes that gap by checking MEANING against the live ROM handlers.

Method (in-context replay over real bank bytecode — the approved M2 method, realized as
per-occurrence controlled execution so the gate stays simple and obviously correct):
  For EVERY opcode occurrence in the 4 code banks (enumerated from the same disassembly the
  round-trip uses), set up a controlled VM frame, point vm_pc at that instruction, and run
  exactly ONE opcode through `nobunaga_vm.step_one_vm_op` — which executes the REAL 6502
  handler in ROM and lands back at the dispatcher. We snapshot (SP, FP, regL, regR,
  call-depth) before/after and compare the OBSERVED deltas to the claim in
  `vm_stack_effect.STACK_EFFECT`. Because a data-stack effect is data-independent, every
  occurrence of a gated opcode must agree; a disagreement is a real finding with an address.

Why per-occurrence (not chasing control flow): the effect we audit is branch-independent,
so following jumps/calls would add a fragile flow engine without strengthening the gate —
and the oracle must be simpler than the thing it validates. We still execute the genuine
ROM bytecode at its genuine address with its genuine operands, for every static occurrence,
which is the coverage the "in-context replay" choice was about. Calls/returns are executed
exactly one step (the handler sets up / tears down the frame and returns to the dispatcher;
the callee is NOT run), so even those are safe to sample in isolation.

Gate (see vm_stack_effect.py for the per-opcode claims):
  data/push/pop/store/control : observed dSP == 2*(pops-pushes) AND every 'keep' reg is
                                unchanged; plus targeted value-checks (qimm/abs).
  call                        : real call -> depth +1; host-stubbed $E9 -> depth 0 & dSP==adj.
  return                      : depth -1 (frame teardown; dSP not asserted).
  probe                       : report-only — observed deltas printed, never fails the gate
                                (ambiguous/unmodeled/dead/syscall/ext-prefix opcodes).

Exit non-zero if any GATED opcode mismatches. Opcodes never emitted in the banks are logged
explicitly as UNOBSERVED (no silent coverage caps).

Usage:
  py -3 tools/vm-stack-audit.py                 # all 4 code banks, full report
  py -3 tools/vm-stack-audit.py 2               # one bank
  py -3 tools/vm-stack-audit.py --max-per-op 50 # cap occurrences/opcode (LOGGED), for speed
  py -3 tools/vm-stack-audit.py --emit-verified # print the PASS list for the toml write-back
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

# vm_assemble is NA1's (na1-decompiler tools/): run this gate from that folder, as before.
from emu_harness.vm.koei.nobunaga_vm import NobunagaVM
from vm_assemble import find_stubs, bank_range, walk_sub
from emu_harness.vm.koei.vm_stack_effect import STACK_EFFECT, GATED_CLASSES, effect

CODE_BANKS = (0, 1, 2, 10, 14, 15)

# Controlled frame: FP high in RAM, SP below it with headroom so data-stack pushes
# (descending) never collide with locals (FP-2..-24) or args (FP+0x0B..+0x11).
FRAME_FP = 0x0500
FRAME_SP = 0x04C0
SENT_L = 0xAAAA          # regL sentinel — any write changes it (no collision with real values)
SENT_R = 0x0003          # regR sentinel — small + nonzero: a valid shift count (avoids a
                         # 20k-iteration shift loop on $BD/$BE/$BF) and a safe divisor.
WIN_LO, WIN_HI = 0x0400, 0x0540   # RAM window zeroed before each occurrence


def _signed16(v):
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def prep(vm, pc):
    """Reset to a clean controlled frame and aim the VM at one instruction."""
    for a in range(WIN_LO, WIN_HI):
        vm.mem.write(a, 0)
    vm.vm_fp = FRAME_FP
    vm.vm_sp = FRAME_SP
    vm.regL = SENT_L
    vm.regR = SENT_R
    vm.vm_pc = pc
    vm.vm_call_depth = 0
    vm.cpu.pc = vm.DISPATCHER_ADDR


def measure(vm, pc, op, valid_body=None):
    """Execute one opcode through the live handler; return an observation dict or None
    on execution failure (timeout/exception — recorded so it never reads as a pass).

    For indirect calls ($DD/$EA) regL is the call TARGET; pointing it at a real sub body
    (valid_body) lets the call build a proper frame and return to the dispatcher instead
    of executing the garbage sentinel address as native code."""
    eff = effect(op)
    operand1 = vm.mem.read(pc + 1)
    operand2 = vm.mem.read(pc + 2)
    operand_w = operand1 | (operand2 << 8)

    # value-check pre-reads (taken before the handler runs)
    pre = {}
    if eff and eff.vcheck == 'absL':
        pre['mem'] = vm.mem.read(operand_w) | (vm.mem.read((operand_w + 1) & 0xFFFF) << 8)

    prep(vm, pc)
    if op in (0xDD, 0xEA) and valid_body is not None:
        vm.regL = valid_body            # indirect-call target -> a real sub stub
    sp0, fp0, l0, r0 = vm.vm_sp, vm.vm_fp, vm.regL, vm.regR
    depth0 = vm.vm_call_depth
    try:
        executed = vm.step_one_vm_op(max_cpu_steps=4000)
    except Exception as ex:                       # a stubbed syscall etc. may raise
        return {'fail': f'exec_error:{type(ex).__name__}'}
    if executed is None:
        return {'fail': 'timeout'}

    obs = {
        'dsp': _signed16(vm.vm_sp - sp0),
        'dfp': _signed16(vm.vm_fp - fp0),
        'regL_chg': vm.regL != l0,
        'regR_chg': vm.regR != r0,
        'ddepth': vm.vm_call_depth - depth0,
        'regL_after': vm.regL,
        'regR_after': vm.regR,
        'sp0': sp0, 'operand1': operand1, 'operand_w': operand_w,
        'stubbed': (op == 0xE9 and operand_w in vm.host_call_stubs),
        'adj': vm.mem.read(pc + 3) if op == 0xE9 else None,
    }
    # value-check resolution
    vc = eff.vcheck if eff else None
    if vc == 'qimm_L':
        obs['vc'] = (vm.regL == (op & 0x0F))
    elif vc == 'qimm_R':
        obs['vc'] = (vm.regR == (op & 0x0F))
    elif vc == 'qpush':
        # ROM push: SP -= 2, then store the word little-endian AT the new SP (LSB at SP,
        # MSB at SP+1) — SP points at the top element. (Confirmed by direct dump; this is
        # the real handler order, not vm_push's helper convention.)
        spa = vm.vm_sp
        word = vm.mem.read(spa) | (vm.mem.read((spa + 1) & 0xFFFF) << 8)
        obs['vc'] = (word == (op & 0x0F))
    elif vc == 'absL':
        obs['vc'] = (vm.regL == pre['mem'])
    elif vc == 'absS':
        if operand_w < 0x8000:                     # only meaningful for writable RAM/SRAM
            back = vm.mem.read(operand_w) | (vm.mem.read((operand_w + 1) & 0xFFFF) << 8)
            obs['vc'] = (back == l0)
        else:
            obs['vc'] = None                        # ROM target — skip (not a writable store site)
    else:
        obs['vc'] = None
    return obs


def gate(op, eff, obs):
    """Return (ok, reason) for a single observation of a GATED opcode."""
    if 'fail' in obs:
        return False, obs['fail']
    cls = eff.cls
    if cls == 'call':
        if obs['stubbed']:
            if obs['ddepth'] != 0:
                return False, f"stub call but ddepth={obs['ddepth']}"
            if obs['dsp'] != obs['adj']:
                return False, f"stub dSP={obs['dsp']} != adj={obs['adj']}"
            return True, ''
        return (obs['ddepth'] == 1), (f"real call ddepth={obs['ddepth']} (want +1)"
                                      if obs['ddepth'] != 1 else '')
    if cls == 'return':
        return (obs['ddepth'] == -1), (f"ddepth={obs['ddepth']} (want -1)"
                                       if obs['ddepth'] != -1 else '')
    # generic data-stack gate
    exp = eff.expected_dsp
    if obs['dsp'] != exp:
        return False, f"dSP={obs['dsp']} (want {exp})"
    if eff.regL == 'k' and obs['regL_chg']:
        return False, "regL changed (should keep)"
    if eff.regR == 'k' and obs['regR_chg']:
        return False, "regR changed (should keep)"
    if obs['vc'] is False:
        return False, f"value-check {eff.vcheck} failed"
    return True, ''


def validate_nop_b2(vm):
    """Settle the M2 NOP-vs-PUSHL dispute for $B2 with a SYNTHETIC occurrence (it is
    never emitted in banks 0/1/2/15, so the occurrence-driven loop can't reach it —
    same situation as $CF RETURN). Place a lone $B2 in RAM, run exactly one op through
    the real ROM handler, observe. dSP==0 + regs kept => NOP (OPCODE_INFO/v2 toml were
    right); dSP==-2 + a pushed word == regL would mean PUSHL. Oracle result 2026-06-03:
    NOP (dSP=0, regL/regR unchanged, pc+1)."""
    m = vm.mem
    prog = 0x0700
    for a in range(prog, prog + 8):
        m.write(a, 0xB2)
    for a in range(WIN_LO, WIN_HI):
        m.write(a, 0)
    vm.vm_fp = FRAME_FP; vm.vm_sp = FRAME_SP
    vm.regL = SENT_L; vm.regR = SENT_R
    vm.vm_pc = prog; vm.vm_call_depth = 0; vm.cpu.pc = vm.DISPATCHER_ADDR
    sp0, l0, r0 = vm.vm_sp, vm.regL, vm.regR
    try:
        ex = vm.step_one_vm_op(4000)
    except Exception as ex_:
        return {'fail': f'exec_error:{type(ex_).__name__}'}
    if ex is None:
        return {'fail': 'timeout'}
    return {'dsp': _signed16(vm.vm_sp - sp0), 'dfp': 0,
            'regL_chg': vm.regL != l0, 'regR_chg': vm.regR != r0,
            'ddepth': vm.vm_call_depth, 'regL_after': vm.regL, 'regR_after': vm.regR,
            'sp0': sp0, 'operand1': 0, 'operand_w': 0,
            'stubbed': False, 'adj': None, 'vc': None}


GATED_OR_SPECIAL = GATED_CLASSES | {'call', 'return'}


def validate_return(vm):
    """Validate $CF RETURN with a REAL caller frame (it cannot be isolated — executing
    the outermost RETURN restores garbage and crashes; see run_until_outermost_return).

    Craft a tiny program in RAM: CALL a callee that is just `prologue + RETURN`. The real
    $E9 handler builds a valid frame for the callee; the callee's RETURN then restores it.
    A clean depth round-trip 1 -> 0 with control landing past the CALL proves RETURN."""
    m = vm.mem
    prog, callee = 0x0700, 0x0710
    m.write(prog, 0xE9); m.write(prog + 1, callee & 0xFF)
    m.write(prog + 2, callee >> 8); m.write(prog + 3, 0)     # CALL_abs_imm1 $0710, adj 0
    m.write(prog + 4, 0xB2)                                  # landing-spot marker
    m.write(callee, 0x20); m.write(callee + 1, 0x23); m.write(callee + 2, 0xE8)
    m.write(callee + 3, 0); m.write(callee + 4, 0)           # prologue: JSR $E823, frame off 0
    m.write(callee + 5, 0xCF)                                # RETURN
    for a in range(0x0680, 0x0540 + 0x100):                  # clear a wide RAM window
        m.write(a, 0)
    vm.vm_fp = FRAME_FP; vm.vm_sp = FRAME_SP
    vm.regL = SENT_L; vm.regR = SENT_R
    vm.vm_pc = prog; vm.vm_call_depth = 0; vm.cpu.pc = vm.DISPATCHER_ADDR
    try:
        vm.step_one_vm_op(4000)                              # CALL -> builds frame, depth +1
        d_call = vm.vm_call_depth
        depth_before_ret = vm.vm_call_depth
        sp_before_ret = vm.vm_sp
        vm.step_one_vm_op(4000)                              # RETURN -> restores frame, depth -1
        d_ret = vm.vm_call_depth
    except Exception as ex:
        return {'fail': f'exec_error:{type(ex).__name__}'}
    ok = (d_call == 1 and d_ret == 0)
    return {'ddepth': d_ret - depth_before_ret, 'd_call': d_call, 'd_ret': d_ret,
            'dsp': _signed16(vm.vm_sp - sp_before_ret), 'ok_pair': ok,
            'regL_chg': False, 'regR_chg': False, 'stubbed': False, 'adj': None,
            'vc': None}


def audit(banks, max_per_op=None, verbose=False):
    # occurrences[op] = list of (bank, addr); valid_body[bank] = a real sub body
    occ = defaultdict(list)
    valid_body = {}
    for bank in banks:
        vm = NobunagaVM()
        vm.switch_bank(bank)
        mem = vm.mem
        base, size = bank_range(bank)
        stubs = find_stubs(mem, base, size)
        if stubs:
            valid_body[bank] = stubs[0]          # stub start (prologue) = a valid call target
        bounds = stubs + [base + size]
        for i, stub in enumerate(stubs):
            instrs, _, _ = walk_sub(mem, stub + 5, bounds[i + 1], {})
            for ins in instrs:
                occ[ins['op']].append((bank, ins['addr']))

    # measure (re-create a VM per bank, switch as needed)
    results = defaultdict(list)        # op -> list of (bank, addr, obs)
    capped = {}
    vms = {}
    for op in sorted(occ):
        if op == 0xCF:
            continue                   # RETURN can't be isolated — validated via the paired test
        sites = occ[op]
        if max_per_op is not None and len(sites) > max_per_op:
            capped[op] = (len(sites), max_per_op)
            # sample evenly across the occurrence list (deterministic)
            step = len(sites) / max_per_op
            sites = [sites[int(k * step)] for k in range(max_per_op)]
        for bank, addr in sites:
            if bank not in vms:
                v = NobunagaVM(); v.switch_bank(bank); vms[bank] = v
            obs = measure(vms[bank], addr, op, valid_body.get(bank))
            results[op].append((bank, addr, obs))

    # RETURN ($CF): paired CALL->RETURN micro-test (real caller frame)
    if 0xCF in occ:
        vm = next(iter(vms.values())) if vms else NobunagaVM()
        results[0xCF].append((-1, 0x0700, validate_return(vm)))

    # $B2: 0 ROM occurrences -> synthetic micro-test to settle the NOP-vs-PUSHL dispute.
    vmb = next(iter(vms.values())) if vms else NobunagaVM()
    results[0xB2].append((-1, 0x0700, validate_nop_b2(vmb)))

    return occ, results, capped


def report(occ, results, capped, emit_verified=False):
    gated_fail = 0
    verified_pass = []      # (op, eff) that passed the gate on all occurrences
    print(f"{'op':>4} {'mnemonic':<16} {'cls':<8} {'n':>5}  verdict")
    print("-" * 72)
    for op in sorted(results):
        eff = effect(op)
        rs = [obs for (_, _, obs) in results[op]]
        n = len(occ.get(op, rs))          # real occurrence count (CF is paired-tested with 1 obs)
        execfails = [o for o in rs if 'fail' in o]

        if eff.cls == 'probe':
            # report-only: summarize observed deltas (and resolve known disputes)
            ok = [o for o in rs if 'fail' not in o]
            if ok:
                dsps = {o['dsp'] for o in ok}
                dd = {o['ddepth'] for o in ok}
                rl = any(o['regL_chg'] for o in ok); rr = any(o['regR_chg'] for o in ok)
                note = f"dSP{sorted(dsps)} ddepth{sorted(dd)} regL{'~' if rl else '='} regR{'~' if rr else '='}"
            else:
                note = "no clean observation"
            extra = ""
            if op == 0xB2:
                # Resolved via the synthetic micro-test (0 ROM occurrences).
                d = next((o['dsp'] for o in ok), None)
                rc = any(o['regL_chg'] or o['regR_chg'] for o in ok)
                verdict_b2 = ("NOP (oracle-confirmed)" if d == 0 and not rc
                              else f"PUSHL (dSP={d})" if d == -2
                              else f"UNRESOLVED (dSP={d}, regchg={rc})")
                extra = f"  <-- synthetic micro-test => {verdict_b2}"
            if op in (0xAE, 0xAF) and ok:
                ops1 = sorted({(_signed16(o['operand1']) if op == 0xAE else o['operand_w']) for o in ok})
                extra = f"  operand{ops1} (compare to dSP)"
            print(f"${op:02X}  {eff.mnem:<16} {eff.cls:<8} {n:>5}  PROBE  {note}{extra}")
            continue

        # gated / special
        first_fail = None
        all_ok = True
        for (bank, addr, obs) in results[op]:
            ok, reason = gate(op, eff, obs)
            if not ok:
                all_ok = False
                if first_fail is None:
                    first_fail = (bank, addr, reason)
        if all_ok and not execfails:
            verdict = "PASS"
            verified_pass.append((op, eff))
        else:
            verdict = "FAIL"
            gated_fail += 1
        tail = ""
        if first_fail:
            b, a, why = first_fail
            tail = f"  first@bank{b} ${a:04X}: {why}"
        if execfails and verdict == "FAIL" and not first_fail:
            tail = f"  {len(execfails)} exec-fail ({execfails[0]['fail']})"
        cap = f"  [capped {capped[op][1]}/{capped[op][0]}]" if op in capped else ""
        if op == 0xCF:
            tail = (tail or "") + "  [paired CALL->RETURN test]"
        print(f"${op:02X}  {eff.mnem:<16} {eff.cls:<8} {n:>5}  {verdict}{tail}{cap}")

    # coverage: gated/special opcodes that never appeared
    unobserved = [op for op, e in STACK_EFFECT.items()
                  if e.cls in GATED_OR_SPECIAL and op not in results]
    print("-" * 72)
    n_gated_present = sum(1 for op in results if effect(op).cls in GATED_OR_SPECIAL)
    n_gated_total = sum(1 for e in STACK_EFFECT.values() if e.cls in GATED_OR_SPECIAL)
    print(f"coverage: {n_gated_present}/{n_gated_total} gated opcodes exercised; "
          f"{len(unobserved)} unobserved")
    if unobserved:
        groups = ", ".join(f"${op:02X}({effect(op).mnem})" for op in sorted(unobserved))
        print(f"  UNOBSERVED (not emitted in these banks): {groups}")
    if capped:
        print(f"  capped opcodes (sampled, LOGGED): "
              + ", ".join(f"${op:02X}={capped[op][1]}/{capped[op][0]}" for op in sorted(capped)))

    if emit_verified:
        print("-" * 72)
        print("VERIFIED (passed the gate — for vm-opcodes-v2.toml write-back):")
        for op, eff in verified_pass:
            if eff.cls in ('call', 'return'):
                print(f"  ${op:02X} {eff.mnem:<16} cls={eff.cls}")
            else:
                print(f"  ${op:02X} {eff.mnem:<16} pops={eff.pops} pushes={eff.pushes}")

    print("-" * 72)
    if gated_fail == 0:
        print(f"STACK-EFFECT AUDIT CLEAN — {len(verified_pass)} gated opcodes confirmed vs the live handler")
    else:
        print(f"{gated_fail} GATED OPCODES FAILED — model and emulator disagree (see first@ above)")
    return gated_fail, {op for op, _ in verified_pass}


def write_toml(passed, path):
    """Conservatively annotate vm-opcodes-v2.toml from the audit result: add pops/pushes +
    verified=true to confirmed gated sections, verified=true to confirmed call/return, and
    a `# audit:` status comment to probe / unobserved sections. Pure line INSERTION — never
    reorders or removes; idempotent (re-runs add nothing). Returns (changed, summary)."""
    import re
    p = Path(path)
    lines = p.read_text(encoding='utf-8').splitlines()
    headers = [(i, int(m.group(1), 16))
               for i, ln in enumerate(lines)
               for m in [re.match(r'\[op_([0-9A-Fa-f]+)\]', ln)] if m]
    spans = {op: (idx, headers[k + 1][0] if k + 1 < len(headers) else len(lines))
             for k, (idx, op) in enumerate(headers)}
    new_lines = list(lines)
    summary = {'verified': 0, 'probe': 0, 'unobserved': 0}
    for op in sorted(spans, reverse=True):     # bottom-up so original indices stay valid
        start, end = spans[op]
        block = lines[start:end]
        eff = effect(op)
        if eff is None:
            continue
        confirmed = (any((op | n) in passed for n in range(16)) if op < 0x80
                     else op in passed)
        has_ver = any(re.match(r'\s*verified\s*=', b) for b in block)
        has_pops = any(re.match(r'\s*pops\s*=', b) for b in block)
        has_audit = any('# audit:' in b for b in block)
        mline = next((j for j, b in enumerate(block) if re.match(r'\s*mnemonic\s*=', b)), 0)
        ins = start + mline + 1
        add = []
        if eff.cls in GATED_CLASSES and confirmed:
            if not has_pops:
                add += [f"pops = {eff.pops}", f"pushes = {eff.pushes}"]
            if not has_ver:
                add.append("verified = true")
            if add:
                summary['verified'] += 1
        elif eff.cls in ('call', 'return') and confirmed:
            if not has_ver:
                add.append("verified = true")
            if not has_audit:
                add.append(f"# audit: {eff.mnem} confirmed vs emulator "
                           f"(call-depth {'+1' if eff.cls == 'call' else '-1'}; frame effect)")
            if add:
                summary['verified'] += 1
        elif eff.cls == 'probe':
            if not has_audit:
                add.append("# audit: report-only (effect varies/unmodeled — see vm_stack_effect.py)")
                summary['probe'] += 1
        else:                                   # gated class but never emitted in these banks
            if not has_audit:
                add.append("# audit: unobserved in banks 0/1/2/15 (model unexercised)")
                summary['unobserved'] += 1
        if add:
            new_lines[ins:ins] = add
    p.write_text("\n".join(new_lines) + "\n", encoding='utf-8')
    return summary


def main():
    ap = argparse.ArgumentParser(description="Sea-16 VM semantic/stack-effect audit (Rung 2)")
    ap.add_argument("bank", nargs="?", type=lambda x: int(x, 0),
                    help="single bank (default: all code banks 0,1,2,15)")
    ap.add_argument("--max-per-op", type=int, default=None,
                    help="cap occurrences measured per opcode (sampled evenly + LOGGED)")
    ap.add_argument("--emit-verified", action="store_true",
                    help="print the PASS list for the toml write-back")
    ap.add_argument("--write-toml", action="store_true",
                    help="annotate vm-opcodes-v2.toml with verified/pops/pushes (needs a CLEAN "
                         "full-bank audit; refuses on any gated failure)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    banks = [args.bank] if args.bank is not None else list(CODE_BANKS)

    occ, results, capped = audit(banks, args.max_per_op, args.verbose)
    total_occ = sum(len(v) for v in occ.values())
    print(f"{total_occ} opcode occurrences enumerated across bank(s) {banks}\n")
    fails, passed = report(occ, results, capped, args.emit_verified)

    if args.write_toml:
        if fails:
            print("\nREFUSING --write-toml: the audit is not clean.")
            sys.exit(1)
        if args.bank is not None or args.max_per_op is not None:
            print("\nREFUSING --write-toml: run a full uncapped audit (all banks) first.")
            sys.exit(1)
        toml = Path(__file__).resolve().parents[1] / "vm-opcodes-v2.toml"   # koeivm/ (package data)
        summ = write_toml(passed, toml)
        print(f"\nwrote {toml.name}: {summ['verified']} sections verified+stack, "
              f"{summ['probe']} probe-noted, {summ['unobserved']} unobserved-noted")
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
