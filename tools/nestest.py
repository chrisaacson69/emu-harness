"""nestest.py - run a 6502 core through kevtris's nestest and compare it with the golden log.

    py -3.14 tools/nestest.py [--core module:Class] [--all]

nestest (an NROM test ROM) run from $C000 in "automation" mode exercises every instruction and
logs the registers before each one; nestest.log is the reference (Nintendulator). This compares
PC, A, X, Y, P and SP line by line and reports the first divergence. By default it stops where
the log reaches the undocumented opcodes (`*NOP` and friends): the cores here implement the
documented 6502 only, as the disassembler does. Cycles are not compared.

The ROM and log are third-party test data: fetch them into _scratch/nestest/ (gitignored) from
github.com/christopherpow/nes-test-roms (other/nestest.nes, other/nestest.log). MIGRATION.md row C.
"""
import argparse
import importlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "_scratch" / "nestest"
LINE = re.compile(r"^([0-9A-F]{4})  ((?:[0-9A-F]{2} ){1,3})\s*(\*?)([A-Z]{3}).*"
                  r"A:([0-9A-F]{2}) X:([0-9A-F]{2}) Y:([0-9A-F]{2}) P:([0-9A-F]{2}) SP:([0-9A-F]{2})")


class NromBus:
    """NROM-128: 2K RAM mirrored to $2000, the 16K PRG at $8000 and $C000; I/O reads 0."""

    def __init__(self, rom: bytes):
        self.ram = bytearray(0x800)
        self.prg = rom[16:16 + 0x4000]

    def read(self, a):
        a &= 0xFFFF
        if a < 0x2000:
            return self.ram[a & 0x7FF]
        if a >= 0x8000:
            return self.prg[a & 0x3FFF]
        return 0

    def write(self, a, v):
        a &= 0xFFFF
        if a < 0x2000:
            self.ram[a & 0x7FF] = v & 0xFF

    def read_word(self, a):
        return self.read(a) | self.read(a + 1) << 8

    def write_word(self, a, v):
        self.write(a, v & 0xFF)
        self.write(a + 1, v >> 8)


def status(cpu):
    """P as a byte, from either core's representation (bit 5 always set, B clear between instructions)."""
    if hasattr(cpu, "p"):
        return cpu.p
    return (0x20 | cpu.n << 7 | cpu.v << 6 | cpu.d << 3 | cpu.i << 2 | cpu.z << 1 | cpu.c)


def run(core_cls, documented_only=True):
    rom = (HERE / "nestest.nes").read_bytes()
    log = [m for m in map(LINE.match, (HERE / "nestest.log").read_text().splitlines())]
    cpu = core_cls(NromBus(rom))
    cpu.pc, cpu.sp = 0xC000, 0xFD
    if hasattr(cpu, "p"):
        cpu.p = 0x24
    else:
        cpu.c = cpu.z = cpu.d = cpu.v = cpu.n = False
        cpu.i = True
    for k, m in enumerate(log):
        if documented_only and m.group(3) == "*":
            return k, None
        want = tuple(int(m.group(i), 16) for i in (1, 5, 6, 7, 8, 9))
        got = (cpu.pc, cpu.a, cpu.x, cpu.y, status(cpu), cpu.sp)
        if got != want:
            names = ("PC", "A", "X", "Y", "P", "SP")
            diff = ", ".join(f"{n} {g:02X} want {w:02X}" for n, g, w in zip(names, got, want) if g != w)
            prev = log[k - 1].group(0)[:48] if k else ""
            return k, f"line {k + 1}: {diff}  (after: {prev})"
        try:
            cpu.step()
        except NotImplementedError as e:
            return k, f"line {k + 1}: {e}  ({m.group(0)[:48]})"
    return len(log), None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--core", default="emu_harness.cpu.m6502.cpu:CPU6502")
    ap.add_argument("--all", action="store_true", help="continue into the undocumented opcodes")
    a = ap.parse_args(argv)
    mod, cls = a.core.split(":")
    n, err = run(getattr(importlib.import_module(mod), cls), documented_only=not a.all)
    print(f"{a.core}: {n} instructions match the log" + (f"; first divergence {err}" if err else
          " (all documented-opcode lines)" if not a.all else ""))
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
