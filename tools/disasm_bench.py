"""disasm_bench.py - score 6502 disassemblers against the running CPU (and an expert source).

    py -3.14 tools/disasm_bench.py capture <rom.nes> <out.cdl> [--frames N] [--starts N]
    py -3.14 tools/disasm_bench.py score

Every contender is reduced to one verdict per PRG byte: I (instruction start), O (operand byte),
D (data), or nothing (no claim). Oracles:
  - Mesen's code/data log (`capture`): bytes the CPU executed (code) and only read (data).
    Lowest artifact available; covers only what the run reached.
  - Elite's annotated source (nes-elite-beebasm, sibling repo): instruction vs data for every
    byte. Expert judgment, not ground truth; checked 100% consistent with the CPU log where both
    speak (2026-10-22).
Metrics: share of executed / source-code bytes called code (recall), share of data bytes called
code (false code), share of claimed instruction starts that are real (start precision).
"+ nes-disasm6" rows keep a contender's claims and take nes-disasm6's verdict for every byte the
contender left unclaimed. Results and reading: MIGRATION.md row B. Captures live in _scratch/
(gitignored).
"""
import argparse
import glob
import re
import sys
from pathlib import Path

from emu_harness.backends.mesen import Mesen, load_config
from emu_harness.cpu.m6502 import disasm as D
from emu_harness.cpu.m6502.descent import Window, descend, verdicts
from emu_harness.machines.nes import nes_disasm6

ROOT = Path(__file__).resolve().parents[2]          # the folder the sibling repos live in
SCRATCH = Path(__file__).resolve().parents[1] / "_scratch"
KEYS = ["up", "down", "left", "right", "select", "start", "b", "a"]
KOEI_BRIDGES = {0xE299: 3, 0xE338: 3}               # JSR targets followed by 3 inline data bytes

UWLINE = re.compile(r"^\s*\$[0-9A-F]{4}:\s+((?:[0-9a-f]{2} )+)\s*(\.db|[a-z]{3})\b.*PRG \$([0-9A-F]{5})")
UWSTUB = re.compile(r"^\w+:\s+jsr \$E299\s+;\s*(\d+):([0-9A-F]{4})")


def prg_of(rom: bytes) -> bytes:
    return rom[16:16 + rom[4] * 0x4000]             # never rom[16:]: that includes CHR-ROM


# ---- contenders -> verdicts --------------------------------------------------------------

def from_uw_native(paths, size):
    """UW disasm_native listings: `$ADDR: bytes mnem ... ; PRG $offset`; stub prologue
    `name: jsr $E299 ; bank:addr` followed by 3 inline operand bytes."""
    v = [""] * size
    for p in paths:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            m = UWLINE.match(line)
            if m:
                off = int(m.group(3), 16)
                for j in range(len(m.group(1).split())):
                    v[off + j] = "D" if m.group(2) == ".db" else ("I" if j == 0 else "O")
            elif s := UWSTUB.match(line):
                off = int(s.group(1)) * 0x2000 + (int(s.group(2), 16) & 0x1FFF)
                v[off:off + 6] = ["I", "O", "O", "D", "D", "D"]
    return v


def from_bitmap(bits):
    return ["O" if b else "" for b in bits]            # code map: no instruction-start claim


def cold_descent(prg, fo, base, size, mirrors=(), **kw):
    """The shared descent from the reset/NMI/IRQ vectors only: no tables, no banking. BRK ends a
    path. -> verdicts over the whole PRG (the window's bytes at file offset `fo`)."""
    win = Window(prg[fo:fo + size], base, mirrors)
    d = descend(win, vectors(prg, fo + size - 0x4000), stop_brk=True, **kw)
    v = [""] * len(prg)
    v[fo:fo + size] = verdicts(win, d.starts, d.operands)
    for a in d.inline:                                 # bridge operands are data, claimed as such
        v[fo + win.offset(a)] = "D"
    return v


def fill(primary, secondary):
    """`primary`'s claims; `secondary`'s verdict wherever primary made none."""
    return [p or s for p, s in zip(primary, secondary)]


# ---- oracles -----------------------------------------------------------------------------

def elite_truth(src_dir, size):
    """Moxon's py8dis source: each line `; ADDR: bytes`, spanning to the next line's address."""
    v, addr_re = [""] * size, re.compile(r";\s*([0-9A-F]{4}):")
    for bank in range(8):
        base, rows = (0xC000 if bank == 7 else 0x8000), []
        for line in (Path(src_dir) / f"elite-source-bank-{bank}.asm").read_text(encoding="utf-8").splitlines():
            if m := addr_re.search(line):
                code = line.split(";", 1)[0].strip()
                rows.append((int(m.group(1), 16), "D" if re.match(r"(EQU[BWS]|SKIP|INCBIN)", code) else "I"))
        for k, (a, kind) in enumerate(rows):
            end = rows[k + 1][0] if k + 1 < len(rows) else base + 0x4000
            for x in range(a, end):
                v[bank * 0x4000 + x - base] = kind if (kind == "D" or x == a) else "O"
    return v


def score(v, truth=None, cdl=None):
    code = ("I", "O")
    def frac(test, idx):
        idx = list(idx)
        return sum(test(v[i]) for i in idx) / len(idx) if idx else float("nan")
    out = {}
    if truth:
        out["source: code found"] = frac(lambda x: x in code, (i for i, t in enumerate(truth) if t in code))
        out["source: data called code"] = frac(lambda x: x in code, (i for i, t in enumerate(truth) if t == "D"))
        out["source: starts that are real"] = (sum(truth[i] == "I" for i, x in enumerate(v) if x == "I")
                                               / max(1, sum(x == "I" for x in v)))
    if cdl:
        out["CPU: executed found"] = frac(lambda x: x in code, (i for i, f in enumerate(cdl) if f & 1))
        out["CPU: data called code"] = frac(lambda x: x in code, (i for i, f in enumerate(cdl) if f & 2 and not f & 1))
        out["CPU: entries as starts"] = frac(lambda x: x == "I", (i for i, f in enumerate(cdl) if f & 0x0C))
    return out


# ---- commands ----------------------------------------------------------------------------

def capture(rom, out, frames, starts):
    with Mesen(Path(rom), keys=KEYS) as m:
        m.step(frames=frames)
        for _ in range(starts):
            m.step(["start"], frames=4); m.step(["a"], frames=4); m.step(frames=180)
        cdl = m.cdl("nesPrgRom", Path(out))
    print(f"{out}: {len(cdl)} bytes, executed {sum(1 for f in cdl if f & 1)}, "
          f"data-read {sum(1 for f in cdl if f & 2 and not f & 1)}")


def vectors(prg, fo):
    return {prg[fo + 0x3FFA + k] | prg[fo + 0x3FFB + k] << 8 for k in (0, 2, 4)}


def run_scores():
    tool = load_config()["nes_disasm6"]
    linear = lambda rom: D.listing_verdicts("".join(D.render(rom, b) + "\n" for b in range(rom[4])), rom[4] * 0x4000)
    def table(title, rows):
        cols = sorted({k for _, r in rows for k in r}, key=lambda k: (not k.startswith("source"), k))
        print(f"\n### {title}\n{'contender':38}" + "".join(f"{c:>28}" for c in cols))
        for name, r in rows:
            print(f"{name:38}" + "".join(f"{(f'{r[c]*100:.1f}%' if c in r else '-'):>28}" for c in cols))

    p = ROOT / "Elite-decompiler" / "Elite (World) (Unl).nes"
    rom = p.read_bytes(); prg = prg_of(rom); cdl = list((SCRATCH / "elite.cdl").read_bytes())
    truth = elite_truth(ROOT / "nes-elite-beebasm/2-build-files/py8dis/source-files", len(prg))
    nd6, cold = nes_disasm6.verdicts(p, tool), cold_descent(prg, 0x1C000, 0xC000, 0x4000)
    table("Elite (MMC1, 128K): Moxon source + CPU log", [
        ("linear sweep (ours)", score(linear(rom), truth, cdl)),
        ("nes-disasm6", score(nd6, truth, cdl)),
        ("descent, cold (vectors)", score(cold, truth, cdl)),
        ("descent, cold + nes-disasm6", score(fill(cold, nd6), truth, cdl))])

    M = ROOT / "mappy-decompiler"
    p = M / "Mappy (Japan).nes"; rom = p.read_bytes(); prg = prg_of(rom); cdl = list((SCRATCH / "mappy.cdl").read_bytes())
    nd6, cold = nes_disasm6.verdicts(p, tool), cold_descent(prg, 0, 0xC000, 0x4000, mirrors=(0x8000,))
    rows = [("linear sweep (ours)", score(linear(rom), cdl=cdl)),
            ("nes-disasm6", score(nd6, cdl=cdl)),
            ("descent, cold (vectors)", score(cold, cdl=cdl)),
            ("descent, cold + nes-disasm6", score(fill(cold, nd6), cdl=cdl))]
    for name, f in (("mappy trace (Python 6502)", "coverage.bin"), ("mappy static_expand", "coverage_static.bin")):
        if (M / f).exists():                       # local outputs of the Mappy pipeline
            v = from_bitmap((M / f).read_bytes())
            rows += [(name, score(v, cdl=cdl)), (name + " + nes-disasm6", score(fill(v, nd6), cdl=cdl))]
    table("Mappy (NROM, 16K): CPU log of the attract demo", rows)

    U = ROOT / "UW-decompile"
    p = U / "Uncharted Waters (USA).nes"; rom = p.read_bytes(); prg = prg_of(rom); cdl = list((SCRATCH / "uw.cdl").read_bytes())
    nat = sorted(glob.glob(str(U / "source/1-asm-6502/bank*.asm")))
    banks = {int(Path(x).stem[4:]) for x in nat}
    scoped = [f if i // 0x2000 in banks else 0 for i, f in enumerate(cdl)]
    lin, nd6 = linear(rom), nes_disasm6.verdicts(p, tool)
    fixed = len(prg) - 0x4000
    cold = cold_descent(prg, fixed, 0xC000, 0x4000)
    koei = cold_descent(prg, fixed, 0xC000, 0x4000, inline=KOEI_BRIDGES)
    uwn = from_uw_native(nat, len(prg))
    table("Uncharted Waters (KOEI MMC5, 512K): CPU log, boot + title", [
        ("linear sweep (ours)", score(lin, cdl=cdl)), ("nes-disasm6", score(nd6, cdl=cdl)),
        ("descent, cold (vectors)", score(cold, cdl=cdl)),
        ("descent, cold, KOEI bridges", score(koei, cdl=cdl)),
        ("descent, cold, bridges + nes-disasm6", score(fill(koei, nd6), cdl=cdl))])
    table(f"Uncharted Waters, the {len(banks)} banks UW disasm_native covers", [
        ("linear sweep (ours)", score(lin, cdl=scoped)), ("nes-disasm6", score(nd6, cdl=scoped)),
        ("UW disasm_native", score(uwn, cdl=scoped)),
        ("UW disasm_native + nes-disasm6", score(fill(uwn, nd6), cdl=scoped))])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("rom"); c.add_argument("out")
    c.add_argument("--frames", type=int, default=3600); c.add_argument("--starts", type=int, default=0)
    sub.add_parser("score")
    a = ap.parse_args(argv)
    SCRATCH.mkdir(exist_ok=True)
    if a.cmd == "capture":
        capture(a.rom, a.out, a.frames, a.starts)
    else:
        run_scores()
    return 0


if __name__ == "__main__":
    sys.exit(main())
