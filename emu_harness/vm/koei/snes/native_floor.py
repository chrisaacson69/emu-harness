#!/usr/bin/env python3
"""
Walk ALL native engine code in the fixed bank and report coverage.

Seeds every entry point the engine has -- the reset + native NMI/IRQ/BRK vectors (those that
point into ROM), every JMP target of the ABI trampoline table (abi_table up to reset: run-VM,
syscall, ext-op, and any extra title-specific entries), the VM spine (run_vm_entry,
ext_interpreter, syscall_dispatch, loader), every primary/extended opcode handler, every
syscall handler and the fetch helpers -- walks them with the recursive-descent native walker
(`snes-decompiler/walk65816.py`), and reports how much of bank $00 is code vs data: the
completeness check that says the native side is fully mapped so effort can move to bytecode
+ data. All addresses come from the profile.

    py -3 tools/native_floor.py <rom> [--game rot3k2] [--gaps]
(profile: --game, else $KOEI_SNES_GAME, else the ROM header -- checked against the ROM)
"""
import argparse, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import cpu_to_file, file_to_cpu, strip_copier_header, LOROM  # noqa: E402
from emu_harness.machines.snes import walk as W  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402
from emu_harness.vm.koei.snes import vm_isa as ISA  # noqa: E402

NATIVE_VECTORS = {"NMI": 0xFFEA, "IRQ": 0xFFEE, "BRK": 0xFFE6}   # native-mode vector words


def rd(rom, a):
    o = cpu_to_file(LOROM, 0, a); return rom[o] | (rom[o + 1] << 8)


def tbl(rom, addr, n):
    b = cpu_to_file(LOROM, 0, addr); return [rom[b + 2 * i] | (rom[b + 2 * i + 1] << 8) for i in range(n)]


def abi_targets(rom, P):
    """JMP targets of the ABI trampoline table: consecutive `4C lo hi` from abi_table to reset."""
    out, a = [], P["abi_table"]
    while a + 3 <= P["reset"]:
        o = cpu_to_file(LOROM, 0, a)
        if rom[o] != 0x4C:
            break
        out.append((a, rom[o + 1] | (rom[o + 2] << 8)))
        a += 3
    return out


def main(argv):
    ap = argparse.ArgumentParser(); ap.add_argument("rom")
    ap.add_argument("--game", default=None); ap.add_argument("--gaps", action="store_true")
    a = ap.parse_args(argv[1:])
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)

    E = set()
    seeds = []
    if 0x8000 <= P["reset"] <= 0xFFFF:                              # reset: emulation mode, 8-bit
        E.add((0, P["reset"], True, True)); seeds.append("reset $%04X" % P["reset"])
    for nm, vec in NATIVE_VECTORS.items():                          # native vectors that point into ROM
        v = rd(rom, vec)
        if 0x8000 <= v <= 0xFFFF:
            E.add((0, v, True, True)); seeds.append("%s $%04X" % (nm, v))
    for at, t in abi_targets(rom, P):                               # the ABI trampolines' targets
        if 0x8000 <= t <= 0xFFFF:
            E.add((0, t, False, False)); seeds.append("abi $%04X->$%04X" % (at, t))
    for a2 in (P["run_vm_entry"], P["ext_interpreter"], P["syscall_dispatch"], P["loader"]):
        E.add((0, a2, False, False))
    for h in P["fetch_helpers"]:
        E.add((0, h, False, False))
    for h in tbl(rom, P["primary_table"], 256) + tbl(rom, P["extended_table"], ISA.TABLE.ext_count) + tbl(rom, P["syscall_table"], P["syscall_count"]):
        if 0x8000 <= h <= 0xFFFF: E.add((0, h, False, False))

    r = W.walk(W.AddressSpace(rom, LOROM, []), list(E), max_insns=500000)
    cov = set()
    for akey, ins in r["starts"].items():
        if akey >> 16 == 0:
            o = cpu_to_file(LOROM, 0, akey & 0xFFFF)
            cov |= set(range(o, o + ins["len"]))
    code = [(k & 0xFFFF) for k in r["starts"] if k >> 16 == 0]
    print("== native floor (bank $00) ==")
    print("seeds        : %s" % ", ".join(seeds))
    print("instructions : %d" % len(r["starts"]))
    print("code bytes   : %d / 32768 (%.1f%%)" % (len(cov), 100 * len(cov) / 0x8000))
    print("code span    : $%04X - $%04X   (above this = data)" % (min(code), max(code)))
    print("indirects    : %d unresolved" % len(r["indirects"]))
    if a.gaps:
        un = sorted(set(range(0, 0x8000)) - cov)
        runs, i = [], 0
        while i < len(un):
            j = i
            while j + 1 < len(un) and un[j + 1] == un[j] + 1:
                j += 1
            runs.append((un[i], un[j])); i = j + 1
        runs.sort(key=lambda t: -(t[1] - t[0]))
        print("largest unreached (data tables / archive / vectors):")
        for x, y in runs[:12]:
            print("  $%04X-$%04X (%d)" % (file_to_cpu(LOROM, x)[1], file_to_cpu(LOROM, y)[1], y - x + 1))


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
