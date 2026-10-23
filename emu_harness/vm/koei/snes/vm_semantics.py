#!/usr/bin/env python3
"""
Fingerprint every KOEI SNES bytecode handler for semantic naming.

For each opcode's native handler it records: stack push/pop balance, which VM registers it
reads/writes, the ALU mnemonics it uses, plus the operand size and control-flow class that
vm_opcodes.derive() reads off the handler (all paths) and the name the shared table gives it.
From these fingerprints the ALU/stack/store handlers can be named in clusters, then compared
opcode-by-opcode to the NES engine. All addresses come from the profile. (The fingerprint walk
follows every branch without flag pruning, so it can include unreachable fall-through code --
e.g. $D8's BEQ/BNE pair falls into $E3's ADC; the size/class columns come from vm_opcodes'
pruned all-paths analysis, not from this walk. BRA/BRL are followed as jumps.)

VM model (direct page, from the handlers): regA=$0A (L32 low word), $0C-$0D = L32 high word
("Lhi" -- there is no independent regB), regC=$0E (R32 low), $10-$11 = R32 high ("Rhi"),
W=$12-$15; FP=$04; scratch pointer $00-$02; PC=$06-$08; operand stack via PHA/PLA.

    py -3 tools/vm_semantics.py "<rom>.sfc" [--game g]           # fingerprint $80-$FF
    py -3 tools/vm_semantics.py "<rom>.sfc" [--game g] --op B0   # detail one
"""
import argparse, os, sys
from emu_harness.machines.snes.mapper import cpu_to_file, strip_copier_header  # noqa: E402
from emu_harness.cpu.w65c816 import isa as w65c816  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402
from emu_harness.vm.koei.snes import vm_isa as ISA  # noqa: E402
from emu_harness.vm.koei.snes import vm_opcodes as V  # noqa: E402

REG = {0x0A: "A", 0x0B: "A", 0x0C: "Lhi", 0x0D: "Lhi", 0x0E: "C", 0x0F: "C",
       0x10: "Rhi", 0x11: "Rhi", 0x12: "W", 0x13: "W", 0x14: "W", 0x15: "W",
       0x04: "FP", 0x05: "FP", 0x00: "P", 0x01: "P", 0x06: "PC", 0x07: "PC", 0x08: "PB"}
ALU = {"ADC", "SBC", "AND", "ORA", "EOR", "ASL", "LSR", "ROL", "ROR", "CMP", "DEC", "INC", "BIT"}
PUSH = {"PHA", "PHX", "PHY"}
POP = {"PLA", "PLX", "PLY"}


def fingerprint(rom, handler, loop, fetch, mapping, cap=120):
    def ro(a):
        if a >= 0x8000:
            f = cpu_to_file(mapping, 0, a)
            return f if f < len(rom) else None
        return None
    seen = set(); st = [(handler, False, False)]
    reads, writes, alu, calls = set(), set(), set(), set()
    push = pop = 0
    while st:
        a, m8, x8 = st.pop()
        while True:
            if a in seen or a in loop or len(seen) > cap:
                break
            f = ro(a)
            if f is None:
                break
            seen.add(a)
            ins, m8, x8 = w65c816.decode(rom, f, 0, a, m8, x8)
            mn, md, raw, tgt = ins["mnem"], ins["mode"], ins["raw"], ins["target"]
            if mn in PUSH: push += 1
            if mn in POP: pop += 1
            if mn in ALU: alu.add(mn)
            if md in ("dp", "dpx", "dpy") and len(raw) > 1:
                r = REG.get(raw[1])
                if r:
                    if mn in ("STA", "STX", "STY", "STZ"): writes.add(r)
                    elif mn in ("INC", "DEC", "ASL", "LSR", "ROL", "ROR", "TSB", "TRB"):
                        writes.add(r); reads.add(r)
                    else: reads.add(r)
            if mn in ("JSR", "JSL") and tgt:
                calls.add(tgt[1])
                if md in ("abs", "abl") and tgt[0] == 0 and tgt[1] not in fetch:
                    st.append((tgt[1], m8, x8))
            if mn in ("JMP", "JML"):
                if tgt and tgt[1] in loop: break
                if tgt and tgt[0] == 0: st.append((tgt[1], m8, x8))
                break
            if mn in ("RTS", "RTL", "RTI", "BRK"): break
            if mn in ("BRA", "BRL"):                     # unconditional: continue at the target only
                if tgt and tgt[0] == 0: st.append((tgt[1], m8, x8))
                break
            if mn in ("BPL", "BMI", "BVC", "BVS", "BCC", "BCS", "BNE", "BEQ") and tgt and tgt[0] == 0:
                st.append((tgt[1], m8, x8))
            a += ins["len"]
    return dict(push=push, pop=pop, reads=reads, writes=writes, alu=alu, calls=calls)


def main(argv):
    ap = argparse.ArgumentParser(); ap.add_argument("rom"); ap.add_argument("--op", default=None)
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ROM header)")
    a = ap.parse_args(argv[1:])
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    derived = V.derive(rom, P, ISA.TABLE.ext_count)["primary"]
    loop, fetch = V.loop_addrs(P), set(P.get("fetch_helpers", {}))
    ops = [int(a.op, 16)] if a.op else [i for i in range(0x80, 0x100)]
    print("op  hdlr   size class  stk     reads            writes           alu              name")
    for i in ops:
        d = derived[i]; h = d["handler"]
        t = ISA.TABLE.primary.get(i)
        if d["cls"] == "dead":
            print("%02X  $%04X  (dead: BRK)" % (i, h)); continue
        fp = fingerprint(rom, h, loop, fetch, _profile.mapping(P))
        stk = "+%d-%d" % (fp["push"], fp["pop"])
        size = "var" if d["cls"] == "switch" else "/".join(str(s) for s in d["sizes"])
        print("%02X  $%04X  %-4s %-6s %-7s r:%-14s w:%-14s %-16s %s" % (
            i, h, size, d["cls"], stk, ",".join(sorted(fp["reads"])), ",".join(sorted(fp["writes"])),
            ",".join(sorted(fp["alu"])), t.name if t else "?"))


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
