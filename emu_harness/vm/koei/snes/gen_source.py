#!/usr/bin/env python3
"""
Derive the bytecode "source" listing of one KOEI SNES module -- the .src layer of the layered
decompilation (raw bytes -> this listing -> the C lift). Every byte of the module is listed,
classified by the walker (vm_walk), never by a linear sweep through data:

  header      the module's `JMP base+3`
  routine     a label (its name, else sub_XXXX), then the native trampoline (routine_sig +
              its frame word), then the routine's reachable bytecode in address order
  bytecode    address, raw bytes, the instruction with EVERY operand value (vm_isa.render:
              'ldA    #$1C69', 'ldA    local5 [fp-$0B]', 'call.p $1169,6', 'jz     $20FA'; a
              switch prints one row per table entry) and an annotation (listing.Lister: callee
              / target names, syscall names -- the id every path pushes, by dataflow --, symbols,
              "string previews", frame-slot variables)
  native      65C816 code inside the module: the stubs bytecode calls (e.g. the syscall stub) and
              the trampolines' JSR target, decoded by RECURSIVE DESCENT (native_walk: both arms of
              every branch, jumps and JSR/JSL callees inside the module, per-path M/X widths), so
              a helper's whole body is listed, not just its first straight run
  data        everything else: `db` rows (strings as "text",0), with any symbol as a label

The address space for names and string previews is the module + its resident + coresident
modules (profile). Decoding goes through the ONE shared ISA (vm_isa; sizes derived from the
ROM's handlers and cross-checked).

    py -3 tools/gen_source.py <rom> [--game g] --module COMMON --syms <title>/data/COMMON > COMMON.src
    (legacy --start/--base select the module too; --scan is checked against the archive length)
"""
import argparse
import os
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import strip_copier_header  # noqa: E402
from emu_harness.cpu.w65c816 import isa as w65c816  # noqa: E402
from emu_harness.vm.koei.snes import archive  # noqa: E402
from emu_harness.vm.koei.snes import listing  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402
from emu_harness.vm.koei.snes import vm_isa as ISA  # noqa: E402
from emu_harness.vm.koei.snes import vm_lift  # noqa: E402
from emu_harness.vm.koei.snes import vm_walk as VW  # noqa: E402
from emu_harness.vm.koei.snes.symbols import Symbols  # noqa: E402

DB_ROW = 16                     # data bytes per db row
NATIVE_CAP = 5000               # native instructions per module before the decode stops (marked)
ENDS = {"RTS", "RTL", "RTI", "BRK", "STP", "COP"}
COND = {"BPL", "BMI", "BVC", "BVS", "BCC", "BCS", "BNE", "BEQ"}


def native_targets(rom, P, isa, mod):
    """Native (non-routine) call targets inside `mod` that bytecode calls -- from every code
    module that can see `mod` -- plus the JSR target of routine_sig (the trampoline's stub)."""
    out = {}
    sig = bytes(P.get("routine_sig", []))
    if len(sig) == 3 and sig[0] == 0x20:                       # JSR abs
        t = sig[1] | (sig[2] << 8)
        if mod.base <= t < mod.end:
            out[t] = "routine trampoline stub (routine_sig JSR target)"
    for m in archive.code_modules(rom, P):
        if mod.name not in _profile.visible_modules(P, m.name):
            continue
        sp = VW.Space(rom, P, m)
        W = VW.walk_module(isa, sp)
        for _site, _e, cls, mname, t, _op in W.calls:
            if cls in ("syscall", "native") and mname == mod.name and t not in out:
                out[t] = "native stub called from bytecode" + (" (syscall: JSL to abi_syscall)" if cls == "syscall" else "")
    return out


def native_walk(mod, entries, bytecode, abi_lo, abi_hi):
    """Recursive-descent 65C816 decode of the module's native code from `entries` (m16/x16, as the
    VM's launcher enters a stub). Followed inside the module: both arms of each conditional branch,
    BRA/BRL/JMP/JML targets, JSR/JSL callees (decoding also continues after the call). A path ends
    at RTS/RTL/RTI/BRK/STP/COP, an indirect JMP, a JSL into the ABI table [abi_lo, abi_hi) (it does
    not return to the stub), a transfer out of the module, or a byte the bytecode walk owns.
    Returns ({addr: decoded ins}, {addr: label text for callees}, [(addr, note)])."""
    insns, owner, callees, notes = {}, {}, {}, []

    def inside(t):
        return t is not None and t[0] == 0x7E and mod.base <= t[1] < mod.end

    memo, active = {}, set()

    def exit_widths(entry, m8, x8):
        """The (m8, x8) a callee entered at `entry` with (m8, x8) returns with: REP/SEP via the decoder,
        PHP/PLP per path, nested in-module calls by their own exit widths, out-of-module callees assumed
        to preserve M/X (the same assumption the walk below makes). None when the exits disagree or a
        path cannot be followed -- the caller then stops decoding after the call instead of guessing."""
        key = (entry, m8, x8)
        if key in memo:
            return memo[key]
        if key in active:
            return None                                         # recursive helper: give up
        active.add(key)
        exits, seen, stack, ok = set(), set(), [(entry, m8, x8, ())], True
        while stack and ok:
            a, cm8, cx8, php = stack.pop()
            while True:
                if (a, cm8, cx8, php) in seen:
                    break
                seen.add((a, cm8, cx8, php))
                if not (mod.base <= a < mod.end) or len(seen) > NATIVE_CAP:
                    ok = False
                    break
                try:
                    ins, nm8, nx8 = w65c816.decode(mod.image, a - mod.base, 0x7E, a, cm8, cx8)
                except (IndexError, KeyError):
                    ok = False
                    break
                mn, md, tgt = ins["mnem"], ins["mode"], ins.get("target")
                if mn in ("RTS", "RTL"):
                    exits.add((nm8, nx8))
                    break
                if mn in ENDS or mn == "XCE":
                    ok = mn != "XCE" and mn not in ("RTI",)     # BRK/STP/COP end the path; RTI/XCE: unknown
                    break
                if mn == "PHP":
                    php = php + ((nm8, nx8),)
                elif mn == "PLP":
                    if not php:
                        ok = False                              # flags pulled from a push we did not see
                        break
                    (nm8, nx8), php = php[-1], php[:-1]
                elif mn in ("JSR", "JSL"):
                    if md in ("abs", "abl") and tgt and tgt[0] == 0 and abi_lo <= tgt[1] < abi_hi and mn == "JSL":
                        break                                   # into the ABI: this path does not return
                    if md in ("abs", "abl") and tgt and inside(tgt):
                        ew = exit_widths(tgt[1], nm8, nx8)
                        if ew is None:
                            ok = False
                            break
                        nm8, nx8 = ew
                elif mn in ("BRA", "BRL", "JMP", "JML"):
                    if md in ("abs", "abl", "rel", "rell") and tgt and inside(tgt):
                        stack.append((tgt[1], nm8, nx8, php))
                    else:
                        ok = False                              # indirect or out of the module
                    break
                elif mn in COND and tgt:
                    if not inside(tgt):
                        ok = False
                        break
                    stack.append((tgt[1], nm8, nx8, php))
                a, cm8, cx8 = a + ins["len"], nm8, nx8
        active.discard(key)
        memo[key] = res = next(iter(exits)) if ok and len(exits) == 1 else None
        return res

    work = [(a, False, False) for a in sorted(entries, reverse=True)]
    while work:
        a, m8, x8 = work.pop()
        while True:
            if a in insns:
                break
            if not (mod.base <= a < mod.end):
                notes.append((a, "path leaves the module"))
                break
            if a in bytecode or a in owner:
                notes.append((a, "path runs into %s" % ("bytecode" if a in bytecode else "the middle of an instruction")))
                break
            if len(insns) >= NATIVE_CAP:
                notes.append((a, "native decode stopped after %d instructions" % NATIVE_CAP))
                work = []
                break
            off = a - mod.base
            try:
                ins, nm8, nx8 = w65c816.decode(mod.image, off, 0x7E, a, m8, x8)
            except (IndexError, KeyError):
                notes.append((a, "undecodable"))
                break
            n = ins["len"]
            if off + n > len(mod.image) or any((a + k) in bytecode or (a + k) in owner for k in range(1, n)):
                notes.append((a, "instruction would overlap bytecode / another instruction"))
                break
            insns[a] = ins
            for k in range(n):
                owner[a + k] = a
            mn, md, tgt = ins["mnem"], ins["mode"], ins.get("target")
            nxt = a + n
            if mn in ENDS:
                break
            if mn in ("JSR", "JSL"):
                if md in ("abs", "abl") and tgt:
                    if tgt[0] == 0 and abi_lo <= tgt[1] < abi_hi and mn == "JSL":
                        break                                   # into the ABI: does not return here
                    if inside(tgt):
                        callees.setdefault(tgt[1], "native helper (%s target from $%04X)" % (mn, a))
                        work.append((tgt[1], nm8, nx8))
                        ew = exit_widths(tgt[1], nm8, nx8)
                        if ew is None:                          # the callee may return in other widths
                            notes.append((a, "%s $%04X returns with unknown or varying M/X; decode stops after the call"
                                          % (mn, tgt[1])))
                            break
                        nm8, nx8 = ew
                a, m8, x8 = nxt, nm8, nx8                       # a call returns (out-of-module: M/X assumed kept)
                continue
            if mn in ("BRA", "BRL", "JMP", "JML"):
                if md in ("abs", "abl", "rel", "rell") and tgt:
                    if inside(tgt):
                        work.append((tgt[1], nm8, nx8))
                    else:
                        notes.append((a, "%s out of the module" % mn))
                break                                           # indirect: target unknown, path ends
            if mn in COND and tgt:
                if inside(tgt):
                    work.append((tgt[1], nm8, nx8))
                else:
                    notes.append((a, "%s out of the module" % mn))
            a, m8, x8 = nxt, nm8, nx8
    return insns, callees, notes


def native_line(ins, S, abi_lo, abi_hi):
    a, mn, tgt = ins["addr"], ins["mnem"], ins.get("target")
    ann = ""
    if tgt and mn in ("JSR", "JSL", "JMP", "JML"):
        if tgt[0] == 0 and abi_lo <= tgt[1] < abi_hi:
            ann = "ABI trampoline $00:%04X" % tgt[1]
        elif tgt[0] == 0x7E and S.string(tgt[1]):
            ann = S.string(tgt[1])
    text = ("%-6s %s" % (mn, ins.get("text", ""))).rstrip()
    return ("  $%04X  %-18s %-30s%s" % (a, listing.hexbytes(ins["raw"]), text, ("; " + ann) if ann else "")).rstrip()


def db_lines(mod, a, end, read_any):
    """db rows for the data bytes [a, end): NUL-terminated ASCII strings as "text",0 rows."""
    out = []
    while a < end:
        s = listing.string_at(lambda v: mod.image[v - mod.base] if mod.base <= v < end else None, a, limit=end - a)
        if s is not None and not s.endswith("..."):
            n = len(s.replace("\\n", "\n")) + 1                    # bytes incl. the NUL
            for k in range(0, n, DB_ROW):
                chunk = mod.image[a - mod.base + k:a - mod.base + min(n, k + DB_ROW)]
                txt = "".join("\\n" if b == 0x0A else chr(b) for b in chunk if b != 0)
                tail = ",0" if k + DB_ROW >= n else ""
                out.append("  $%04X  %-47s db %s%s" % (a + k, listing.hexbytes(chunk), listing.quoted(txt), tail))
            a += n
            continue
        k = 1                                                      # a binary run up to the next string start
        while k < DB_ROW and a + k < end:
            if listing.string_at(lambda v: mod.image[v - mod.base] if mod.base <= v < end else None, a + k,
                                 limit=end - a - k) is not None:
                break
            k += 1
        chunk = mod.image[a - mod.base:a - mod.base + k]
        out.append("  $%04X  %-47s db %s" % (a, listing.hexbytes(chunk), ",".join("$%02X" % b for b in chunk)))
        a += k
    return out


def main(argv):
    ap = argparse.ArgumentParser(description="Bytecode .src listing of one KOEI SNES module (every byte).")
    ap.add_argument("rom")
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ROM header)")
    ap.add_argument("--module", default=None, help="archive name (instead of --start/--base)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--scan", default=None, help="legacy module end (checked against the archive length)")
    ap.add_argument("--syms", required=True)
    a = ap.parse_args(argv[1:])
    if not (a.module or a.start):
        ap.error("give --module NAME or --start/--base")
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    isa = ISA.bind(rom, P)
    mod = VW.module_from_args(rom, P, a)
    if a.scan and int(a.scan, 16) != mod.end:
        print("note: --scan %s ignored; module %s ends at $%04X (archive length)" % (a.scan, mod.name, mod.end),
              file=sys.stderr)
    space = VW.Space(rom, P, mod)
    S = Symbols(a.syms, profile=P)
    W = VW.walk_module(isa, space)
    abi_lo, abi_hi = P["abi_table"], P["reset"]
    bytecode = VW.covered(W, space)
    natives = native_targets(rom, P, isa, mod)
    nat, callees, nnotes = native_walk(mod, natives, bytecode, abi_lo, abi_hi)
    nlabel = dict(callees)
    nlabel.update(natives)                            # an entry's own reason wins over "helper"
    code = set(bytecode)
    for v, ins in nat.items():
        code.update(range(v, v + ins["len"]))
    L = listing.Lister(isa, S, space, syscall_ids=vm_lift.module_syscall_ids(space, W), code=code)
    routines = set(W.routines)
    starts = set(W.insns) | routines | set(nat)
    r = mod.row
    st = W.stats()
    print("; ==== %s source (derived by koei-snes gen_source) -- profile %s ====" % (mod.name, P["game"]))
    print("; module %s: WRAM $7E:%04X-$%04X, archive row %d at ROM %02X:%04X, $%X bytes; %d routines, %d instructions,"
          % (mod.name, mod.base, mod.end - 1, r.index, r.bank, r.addr, mod.length, st["routines"], st["instructions"]))
    print("; %d conflicts, %d dead-op hits, %d decode errors; %d native entr%s, %d native instructions; %d symbols "
          "visible (%s)" % (st["conflicts"], st["dead_hits"], st["decode_errors"], len(natives),
                            "y" if len(natives) == 1 else "ies", len(nat), len(S.strings),
                            ", ".join(os.path.basename(d) for d in S.loaded)))
    for v, why in nnotes:
        print("; native decode note at $%04X: %s" % (v, why))
    print("; line: $addr  raw bytes  instruction with operand values  ; annotation   (f8/f16 operands:")
    print(";   variable [fp+offset]; call.p target,popped-bytes; r8 branches shown as their target)")
    print()
    print("  $%04X  %-18s %-30s; module header: JMP base+3" % (mod.base, listing.hexbytes(mod.image[0:3]),
                                                              "JMP    $%04X" % mod.entry))
    v = mod.entry
    while v < mod.end:
        if v in routines:
            o = v - mod.base
            tr = mod.image[o:o + space.body_off]
            nm = S.string(v) or "sub_%04X" % v
            print("\n%s:" % nm)
            if space.body_off >= len(space.sig) + 2:
                fw = tr[len(space.sig)] | (tr[len(space.sig) + 1] << 8)
                txt = "JSR    $%04X, dw $%04X" % (space.sig[1] | (space.sig[2] << 8), fw) if len(space.sig) == 3 \
                    else "routine_sig, dw $%04X" % fw
                print("  $%04X  %-18s %-30s; routine trampoline (frame word %s), body $%04X"
                      % (v, listing.hexbytes(tr), txt, ISA.hexs(fw - 0x10000 if fw & 0x8000 else fw), v + space.body_off))
            else:
                print("  $%04X  %-18s %-30s; routine trampoline, body $%04X"
                      % (v, listing.hexbytes(tr), "routine_sig", v + space.body_off))
            v += max(1, space.body_off)
            continue
        ins = W.insns.get(v)
        if ins is not None:
            for line in L.lines(ins):
                print(line)
            v += max(1, ins.length)
            continue
        if v in nat:
            if v in nlabel:
                print("\n%s:   ; native 65C816 -- %s" % (S.string(v) or "native_%04X" % v, nlabel[v]))
            print(native_line(nat[v], S, abi_lo, abi_hi))
            v += nat[v]["len"]
            continue
        end = v + 1                                    # a data run up to the next listed start
        while end < mod.end and end not in starts:
            end += 1
        k = v
        while k < end:                                 # split the run at named data addresses
            nxt = k + 1
            while nxt < end and not S.string(nxt):
                nxt += 1
            if S.string(k):
                print("%s:   ; %s" % (S.string(k), S.kind(k) or "symbol"))
            for line in db_lines(mod, k, nxt, space.read_any):
                print(line)
            k = nxt
        v = end
    print("\n; ==== end of %s ($%04X) ====" % (mod.name, mod.end))
    return 0


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
