"""Decompile-all driver — the "see the whole picture" payoff (EPIC step 3b/4).

Persists `decompiled/bank_NN.c`: every bytecode subroutine in the 4 CODE banks
(0, 1, 2, 15 — banks 03-14 are data, 0 bytecode subs), decompiled to readable C.
Deterministic: fixed ROM -> identical C forever, so this is run ONCE and the .c
files are committed. Reading the engine becomes opening a file, never re-running
a tool. Regenerate only when vm-disasm.py or vm_decompile.py themselves improve.

Pipeline (all execution-validated; koeivm package, see ARCHITECTURE.md / ROADMAP):
  vm_disasm     ROM -> VM-asm listing (correct lengths, recursive-descent subs)
  vm_decompile  listing -> per-block C  (spec-agnostic: dispatch keyed on the opcode byte)
  dream         structure -> readable C  (DREAM primary; vm_reduce/V2 fallback)

Usage (run from the tools/ dir so `koeivm` resolves):
  py -3 -m koeivm.cli.decompile_all                 # canonical DREAM C + asm -> decompiled/
  py -3 -m koeivm.cli.decompile_all --basic         # the basic goto form -> bank_NN.raw.c
  py -3 -m koeivm.cli.decompile_all --banks 1       # one bank
  py -3 -m koeivm.cli.decompile_all --out-dir foo   # alternate output dir
"""

import argparse
import io
import re
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

# This module lives at koeivm/cli/; the project root (nobunaga/) is three levels up
# (cli -> koeivm -> tools -> nobunaga). Add tools/ to sys.path so `koeivm` resolves
# when run as a loose script as well as via `python -m koeivm.cli.decompile_all`.
ROOT = Path(__file__).resolve().parents[3]

LABELS_TOML = ROOT / "mesen-labels.toml"


def _switchable_section_labels():
    """Per-bank names for the switchable $8000-$BFFF window, parsed straight from the
    [prg.bankN] toml sections. Returns (bank_names, sectioned):
      bank_names[bank][addr] = name  — the authoritative name for that addr IN that bank
      sectioned = {addr, ...}        — every window addr that appears in ANY [prg.bankN]

    Banks 0/1/2 share the $8000-$BFFF CPU window, and the SAME CPU address can hold a
    DIFFERENT function in each bank (different ROM bytes at the same address — e.g. $8003
    is display_fullscreen_graphic_sequence in bank 0 but prompt_message_and_redraw in
    bank 1). `nobunaga_vm.load_labels()` flattens the toml to {addr: name}, which (a)
    discards the section AND (b) keeps only one name per address — so the decompiler was
    both bank-blind and dual-bank-blind for the window (bug surfaced 2026-05-31 by the
    regen guard for the section problem; the dual-bank case surfaced 2026-06-01 by the
    bank_00 label-walk: $8003/$8DE1 stayed anon because bank 1's entries shadowed bank 0's).
    Keying names by (bank, addr) resolves both: each bank's C uses its own section's name.

    RAM/ZP (<$8000) and fixed bank-15 ($C000+) labels are bank-independent and stay global
    (applied from vm.labels), so they are intentionally not collected here. ~73 $C000+
    labels mis-filed under [prg.bank0] are >= $C000, fall outside the window, remain global.
    """
    text = LABELS_TOML.read_text(encoding="utf-8")
    sec_re = re.compile(r'^\s*\[prg\.bank(\d+)\]')
    lbl_re = re.compile(r'"0x([0-9A-Fa-f]+)"\s*=\s*\{\s*name\s*=\s*"([^"]*)"')
    bank_names, sectioned, cur = {}, set(), None
    for line in text.splitlines():
        m = sec_re.match(line)
        if m:
            cur = int(m.group(1)); continue
        if line.lstrip().startswith('['):      # left the prg.bankN sections
            cur = None; continue
        m = lbl_re.search(line)
        if m and cur is not None:
            addr = int(m.group(1), 16)
            if 0x8000 <= addr < 0xC000:
                bank_names.setdefault(cur, {})[addr] = m.group(2)
                sectioned.add(addr)
    return bank_names, sectioned


SWITCHABLE_NAMES, SECTIONED_WINDOW = _switchable_section_labels()


from emu_harness.vm.koei import vm_disasm as vmdisasm
from emu_harness.vm.koei import vm_decompile
from emu_harness.vm.koei import dream         # DREAM structurer (CFG epic rung-6 cutover; PRIMARY structurer)
from emu_harness.vm.koei.nobunaga_vm import NobunagaVM

# Wire the DREAM cutover hook: decompile()'s STRUCTURE_DREAM path calls structure_dream
# per sub, using DREAM where its AST-native gate passes and falling back to V2 otherwise.
# Done here (not in vm_decompile) so vm_decompile stays import-light for the gate tools.
vm_decompile.structure_dream = dream.dream_structure_gated

CODE_BANKS = [0, 1, 2, 10, 14, 15]   # banks containing bytecode (banks 3-9/11-13 are pure data).
# 10 = audio trigger (1 sub), 14 = AI-turn cutscene engine (3 subs); both invoked via call_bank
# (syscall 7 = JSR bank $8000 -> JSR $E823 vm_entry). Added 2026-06-10 — see chapter 19 / ch.5.

# The recovered "source" is committed as a pipeline ladder under source/ (see
# tools/koeivm/ARCHITECTURE.md §artifacts): each decompiler stage is its own dir.
# This driver writes stages 2-4; stage 1 (native 6502) is mesen-labels.py's territory.
STAGE_VM    = "2-asm-vm"      # Sea-16 VM bytecode disassembly (bank_NN_vm.asm)
STAGE_BASIC = "3-c-basic"    # bytecode -> direct goto C (bank_NN.raw.c)
STAGE_C     = "4-c"          # structured C (DREAM; canonical) / V2 audit (bank_NN.c)

# Var-walk names (M4): {(bank, sub_addr): {slot: semantic_name}} from the toml's
# [vars.bankN."0xADDR"] sections. Loaded once; bank_subs passes the per-sub dict to
# vm_decompile.decompile so bank_NN.c AND the merged all_banks.c (which reuses
# bank_subs) get the same renames — single source, can't diverge.
VAR_NAMES = vm_decompile.load_var_names(LABELS_TOML)


def bank_subs(bank, tmp_dir, on_sub=None):
    """Decompile every sub in `bank`, returning (labels, subs, misaligned) where
    subs = [(stub_addr, sub_c_text), ...]. The shared decompile core of the per-bank
    writer (`decompile_bank`) AND the merged PRG view (`decompile-merged.py`), so both
    produce byte-identical C from the same pipeline (single source of truth).

    `on_sub(stub, collect)`, if given, is called per sub with the decompiler's
    `collect` dict (raw + structured control-flow line lists + decoded instructions)
    — the hook the CFG equivalence gate (tools/vm-cfg-gate.py) reads. None = no
    collection (the default production path is untouched).

    Bank-aware label set: global labels (<$8000 RAM/ZP, >=$C000 fixed bank 15) and any
    window addr with NO [prg.bankN] section entry come from the flattened vm.labels (the
    latter preserves prior default-keep behavior). For the switchable $8000-$BFFF window,
    names come from THIS bank's toml section (SWITCHABLE_NAMES[bank]) — never the flattened
    dict, which holds only one name per address and would bleed another bank's name onto a
    CPU address that legitimately holds a different function in each bank.
    """
    vm = NobunagaVM()
    vm.switch_bank(bank)
    mem = vm.mem
    base, size = vmdisasm.bank_range(bank)
    stubs = vmdisasm.find_stubs(mem, base, size)
    labels = {a: n for a, (n, _c) in vm.labels.items()
              if not (0x8000 <= a < 0xC000) or a not in SECTIONED_WINDOW}
    labels.update(SWITCHABLE_NAMES.get(bank, {}))

    # Render the execution-validated VM-asm listing for this bank. Written to tmp as the
    # decompiler's input; decompile_bank also persists it to the committed `bank_NN_vm.asm`
    # asm oracle on a canonical run (the same deterministic listing, kept beside the C).
    listing, misaligned = vmdisasm.render(bank, stubs, mem, labels, len(vm.labels))
    asm_path = tmp_dir / f"bank_{bank:02d}_vm.asm"
    asm_path.write_text(listing, encoding="utf-8")

    # Decompile each sub (vm_decompile prints to stdout — capture per sub). Drop the
    # per-sub "// Decompiled from <path>" line: it names the ephemeral temp listing
    # (non-deterministic path) and is redundant with the bank header.
    subs = []
    for stub in stubs:
        buf = io.StringIO()
        collect = {} if on_sub else None
        with redirect_stdout(buf):
            vm_decompile.decompile(str(asm_path), stub, labels,
                                   var_names=VAR_NAMES.get((bank, stub)),
                                   collect=collect)
        sub_c = "\n".join(l for l in buf.getvalue().splitlines()
                          if not l.startswith("// Decompiled from")).rstrip("\n")
        # Readability: inline trivially-single-use synthetic temps (DREAM canonical only).
        if vm_decompile.STRUCTURE and getattr(vm_decompile, "STRUCTURE_DREAM", False):
            sub_c = vm_decompile.inline_trivial_temps(sub_c)
        subs.append((stub, sub_c))
        if on_sub:
            on_sub(stub, collect)
    return labels, subs, misaligned


def decompile_bank(bank, out_dir, tmp_dir, suffix=""):
    """Decompile every sub in `bank` into the source/ ladder under `out_dir`. Returns
    (n_subs, n_lines). The C lands in its stage dir — ".raw" (basic) -> 3-c-basic/, else
    -> 4-c/ (DREAM canonical, or ".v2" audit sibling) — and a canonical run also persists
    the deterministic VM-asm listing to 2-asm-vm/bank_NN_vm.asm (same listing the decompiler
    consumed). `suffix`: "" = DREAM hybrid, ".raw" = basic witness, ".v2" = V2 audit."""
    _labels, subs, misaligned = bank_subs(bank, tmp_dir)
    dream_hybrid = vm_decompile.STRUCTURE and getattr(vm_decompile, "STRUCTURE_DREAM", False)
    v2 = vm_decompile.STRUCTURE and vm_decompile.STRUCTURE_V2
    basic = not vm_decompile.STRUCTURE
    if dream_hybrid:
        pipeline = ("vm-disasm.py (execution-validated lengths) -> vm_decompile.py "
                    "(opcode-keyed front-end) -> dream.py (DREAM structurer; V2 fallback).")
        tag = ""
    elif v2:
        pipeline = ("vm-disasm.py -> vm_decompile.py -> vm_reduce.py (region reducer).")
        tag = "  (--v2: region-reducer structurer, audit/diff only — not canonical)"
    elif basic:
        pipeline = ("vm-disasm.py (execution-validated lengths) -> vm_decompile.py "
                    "(opcode-keyed) — NO structuring: the direct goto/label form.")
        tag = ("  (BASIC: bytecode -> direct goto-infused C, the intermediary form the "
               "structure engine folds; the CFG-equivalence reference for bank_%02d.c)" % bank)
    else:
        pipeline = ("vm-disasm.py -> vm_decompile.py (opcode-keyed).")
        tag = "  (V1 templates — legacy)"
    c_stage = STAGE_BASIC if basic else STAGE_C
    parts = [
        f"// ============================================================",
        f"// source/{c_stage}/bank_{bank:02d}{suffix}.c  —  GENERATED by `python -m koeivm.cli.decompile_all`" + tag,
        f"// {len(subs)} bytecode subroutines from bank {bank} of "
        f"Nobunaga's Ambition (USA).nes",
        f"// Deterministic (fixed ROM -> identical C). DO NOT hand-edit — "
        f"regenerate via the tool.",
        f"// Pipeline: {pipeline}",
        f"// ============================================================",
        "",
    ]
    if misaligned:
        parts.append(f"// WARNING: {misaligned} subs did not tile cleanly "
                     f"(see vm-disasm.py --check {bank}).\n")

    for _stub, sub_c in subs:
        parts.append(sub_c)
        parts.append("")  # blank line between subs

    text = "\n".join(parts) + "\n"
    c_dir = out_dir / c_stage
    c_dir.mkdir(parents=True, exist_ok=True)
    (c_dir / f"bank_{bank:02d}{suffix}.c").write_text(text, encoding="utf-8")

    # Persist the VM-asm oracle (stage 2) on a canonical run — one deterministic listing/bank.
    if dream_hybrid:
        asm_src = tmp_dir / f"bank_{bank:02d}_vm.asm"
        if asm_src.exists():
            asm_dir = out_dir / STAGE_VM
            asm_dir.mkdir(parents=True, exist_ok=True)
            (asm_dir / f"bank_{bank:02d}_vm.asm").write_text(
                asm_src.read_text(encoding="utf-8"), encoding="utf-8")
    return len(subs), text.count("\n")


def run_self_check():
    """VM-model drift-guard (M3): decompile all 4 code banks with vm_decompile.SELF_CHECK
    on, then assert each gated opcode's OBSERVED data-stack Δ (from the real handlers)
    matches the audited vm_stack_effect.STACK_EFFECT. Catches the $30-$3F / $B2 bug class.
    Lives here (not in vm_decompile) so it shares THIS module's `import vm_decompile`
    instance — the flag/trace globals must be the ones bank_subs() actually writes."""
    from emu_harness.vm.koei.vm_stack_effect import STACK_EFFECT, GATED_CLASSES
    vm_decompile.SELF_CHECK = True
    vm_decompile._STACK_TRACE = []
    try:
        with tempfile.TemporaryDirectory() as td:
            for bank in CODE_BANKS:
                buf = io.StringIO()
                with redirect_stdout(buf):           # swallow the decompiled C
                    bank_subs(bank, Path(td))
    finally:
        vm_decompile.SELF_CHECK = False
    mism, observed = vm_decompile.check_stack_trace()
    n_gated = sum(1 for op in observed
                  if (STACK_EFFECT.get(op) and STACK_EFFECT[op].cls in GATED_CLASSES
                      and op not in vm_decompile._SELF_CHECK_EXCLUDE))
    print(f"VM-model drift-guard: {len(observed)} opcodes observed across banks "
          f"{list(CODE_BANKS)}; {n_gated} gated checked vs STACK_EFFECT")
    if vm_decompile._SELF_CHECK_EXCLUDE:
        print("  excluded (documented divergences): "
              + ", ".join(f"${op:02X}" for op in sorted(vm_decompile._SELF_CHECK_EXCLUDE)))
    if mism:
        print(f"\n{len(mism)} OPCODE(S) DISAGREE with the audited table "
              f"(decompiler data-stack model drifted):")
        for op, exp, got, mnem in mism:
            print(f"  ${op:02X} {mnem:<14} expected dStack={exp:+d}, observed dStack={got}")
        sys.exit(1)
    print("DRIFT-GUARD CLEAN -- decompiler data-stack model agrees with the audited authority")
    sys.exit(0)


def main():
    if "--self-check" in sys.argv:
        return run_self_check()
    ap = argparse.ArgumentParser(description="Decompile-all driver")
    ap.add_argument("--self-check", action="store_true",
                    help="VM-model drift-guard: assert the decompiler's data-stack effect per "
                         "opcode matches the audited vm_stack_effect.STACK_EFFECT (no regen)")
    ap.add_argument("--banks", default=",".join(map(str, CODE_BANKS)),
                    help="comma-separated bank list (default: the 4 code banks)")
    ap.add_argument("--out-dir", default=None,
                    help="output directory for bank_NN.c (default: ./decompiled)")
    ap.add_argument("--no-merged", action="store_true",
                    help="skip the merged PRG-keyed all_banks.c view (auto-built when the "
                         "full default bank set is decompiled; see decompile-merged.py)")
    ap.add_argument("--basic", "--raw", dest="basic", action="store_true",
                    help="emit the BASIC form: bytecode -> direct goto/label C (NO structuring) "
                         "as bank_NN.raw.c, committed beside the canonical bank_NN.c. This is "
                         "the intermediary the structure engine folds AND the CFG-equivalence "
                         "reference the gate proves bank_NN.c equivalent to. (--raw is an alias.)")
    ap.add_argument("--v2", action="store_true",
                    help="structure with the region reducer (vm_reduce) instead of DREAM, writing "
                         "bank_NN.v2.c / all_banks.v2.c beside the canonical files — audit/diff "
                         "only (V2 is the per-sub FALLBACK engine; not the canonical output).")
    args = ap.parse_args()

    if args.basic and args.v2:
        ap.error("--basic/--raw and --v2 are mutually exclusive (--basic skips structuring entirely)")
    # CFG epic rung-6 CUTOVER: DREAM-hybrid (DREAM primary, V2 fallback) is the DEFAULT
    # canonical structurer. --basic strips structuring to the goto witness; --v2 selects the
    # region reducer for an audit diff. Exactly one structurer mode is active per run.
    suffix = ""
    if args.basic:
        vm_decompile.STRUCTURE = False               # basic: goto/label witness
        suffix = ".raw"
    elif args.v2:
        vm_decompile.STRUCTURE_V2 = True             # V2 region reducer (audit)
        suffix = ".v2"
    else:
        vm_decompile.STRUCTURE_DREAM = True          # canonical: DREAM primary + V2 fallback
    banks = [int(b, 0) for b in args.banks.split(",") if b.strip()]
    # Output base is the source/ ladder (decompile_bank routes each artifact to its stage dir).
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "source"
    out_dir.mkdir(parents=True, exist_ok=True)
    c_stage = STAGE_BASIC if args.basic else STAGE_C

    total_subs = total_lines = 0
    # Intermediate VM-asm listings are regenerable (vm_disasm is deterministic), so
    # they live in an ephemeral temp dir — only the committed stage artifacts persist.
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        for bank in banks:
            n_subs, n_lines = decompile_bank(bank, out_dir, tmp_dir, suffix=suffix)
            total_subs += n_subs
            total_lines += n_lines
            print(f"  bank {bank:>2}: {n_subs:>3} subs -> "
                  f"source/{c_stage}/bank_{bank:02d}{suffix}.c ({n_lines} lines)")
    print(f"\nDecompiled {total_subs} subroutines across {len(banks)} banks "
          f"({total_lines} lines of C) -> {out_dir}/")

    # Also regenerate the merged PRG-keyed view so it never drifts from the per-bank
    # files — a single run keeps both in sync. Only when the full default bank set was
    # requested (a partial --banks run is a per-bank iteration; the merged view is always
    # all 4 code banks). Lazy import avoids a circular load (decompile_merged reuses bank_subs).
    if not args.no_merged and banks == CODE_BANKS:
        from emu_harness.vm.koei.cli import decompile_merged as merged
        records, warnings = merged.build(bank_subs)
        merged_path = out_dir / c_stage / f"all_banks{suffix}.c"
        merged_path.parent.mkdir(parents=True, exist_ok=True)
        merged_path.write_text(merged.render(records, warnings), encoding="utf-8")
        print(f"Merged PRG-keyed view: {len(records)} subs -> "
              f"source/{c_stage}/all_banks{suffix}.c")
        for w in warnings:
            print(f"  warning: {w}")


if __name__ == "__main__":
    main()
