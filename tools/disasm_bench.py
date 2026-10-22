"""disasm_bench.py - score 6502 disassemblers against the running CPU (and an expert source).

    py -3.14 tools/disasm_bench.py capture <rom.nes> <out.cdl> [--frames N] [--starts N]
    py -3.14 tools/disasm_bench.py score

Every contender is reduced to one verdict per PRG byte: I (instruction start), O (operand byte),
D (data). Oracles:
  - Mesen's code/data log (`capture`): bytes the CPU executed (code) and only read (data).
    Lowest artifact available; covers only what the run reached.
  - Elite's annotated source (nes-elite-beebasm, sibling repo): instruction vs data for every
    byte. Expert judgment, not ground truth; checked 100% consistent with the CPU log where both
    speak (2026-10-22).
Metrics: share of executed / source-code bytes called code (recall), share of data bytes called
code (false code), share of claimed instruction starts that are real (start precision).
Results and reading: MIGRATION.md row B. Captures live in _scratch/ (gitignored).
"""
import argparse
import glob
import re
import subprocess
import sys
from pathlib import Path

from emu_harness.backends.mesen import Mesen, load_config
from emu_harness.cpu.m6502 import disasm as D

ROOT = Path(__file__).resolve().parents[2]          # the folder the sibling repos live in
SCRATCH = Path(__file__).resolve().parents[1] / "_scratch"
KEYS = ["up", "down", "left", "right", "select", "start", "b", "a"]

LISTING = re.compile(r";\s*([0-9A-Fa-f]{1,6}):\s+([0-9a-fA-F ]+?)\s*(?:;.*)?$")   # unpadded offsets too
MNEM = re.compile(r"^\s*(?:[@\w]+:)?\s*([a-z]{3})\b")
UWLINE = re.compile(r"^\s*\$[0-9A-F]{4}:\s+((?:[0-9a-f]{2} )+)\s*(\.db|[a-z]{3})\b.*PRG \$([0-9A-F]{5})")
UWSTUB = re.compile(r"^\w+:\s+jsr \$E299\s+;\s*(\d+):([0-9A-F]{4})")


def prg_of(rom: bytes) -> bytes:
    return rom[16:16 + rom[4] * 0x4000]             # never rom[16:]: that includes CHR-ROM


# ---- contenders -> verdicts --------------------------------------------------------------

def from_listing(text, size):
    """disasm6-style listing (ours and nes-disasm6): `... ; 0C22C:  ad 73 00`; `hex`/`.db` = data."""
    v = [""] * size
    for line in text.splitlines():
        m = LISTING.search(line)
        if not m:
            continue
        off, n = int(m.group(1), 16), len(m.group(2).split())
        mm = MNEM.match(line.split(";", 1)[0])
        insn = bool(mm) and mm.group(1) != "hex"
        for j in range(n):
            if off + j < size:
                v[off + j] = ("I" if j == 0 else "O") if insn else "D"
    return v


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
    return ["O" if b else "D" for b in bits]           # code map: no instruction-start claim


def recursive_descent(prg, regions, seeds):
    """Cold descent from `seeds` over `regions` [(file_off, cpu_base, size)]: both branch sides,
    JSR (+fall-through), JMP abs; stops at rts/rti/brk/jmp(ind)/illegal. No tables, no banking."""
    def to_off(a):
        for fo, base, size in regions:
            if base <= a < base + size:
                return fo + (a - base)
        return None
    v, seen, work = [""] * len(prg), set(), list(seeds)
    while work:
        pc = work.pop()
        while (o := to_off(pc)) is not None and o not in seen and prg[o] in D.OPS:
            op = prg[o]
            mode = D.OPS[op][1]
            ln = 1 + D.MODE_LEN[mode]
            if v[o] == "O" or any(v[o + j] for j in range(1, ln)):
                break                                   # would overlap a decoded instruction
            seen.add(o)
            v[o:o + ln] = ["I"] + ["O"] * (ln - 1)
            nxt = (pc + ln) & 0xFFFF
            if mode == D.REL:
                work.append((nxt + ((prg[o + 1] ^ 0x80) - 0x80)) & 0xFFFF)
            elif op in (0x20, 0x4C):
                work.append(prg[o + 1] | prg[o + 2] << 8)
                if op == 0x4C:
                    break
            if op in (0x60, 0x40, 0x00, 0x6C):
                break
            pc = nxt
    return [x or "D" for x in v]


def nes_disasm6(rom_path, cfg):
    """Run the upstream nes-disasm6 clone (Apache-2.0, never vendored; path in config.toml)."""
    tool = Path(cfg["nes_disasm6"]) / "disasm.py"
    r = subprocess.run([sys.executable, "-I", str(tool), "--stdout", "-r", str(rom_path)],
                       capture_output=True, text=True, cwd=SCRATCH)
    if r.returncode:
        raise RuntimeError(f"nes-disasm6 failed: {r.stderr[-300:]}")
    return r.stdout


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
    cfg = load_config()
    linear = lambda rom: "".join(D.render(rom, b) + "\n" for b in range(rom[4]))
    def table(title, rows):
        cols = sorted({k for _, r in rows for k in r}, key=lambda k: (not k.startswith("source"), k))
        print(f"\n### {title}\n{'contender':30}" + "".join(f"{c:>28}" for c in cols))
        for name, r in rows:
            print(f"{name:30}" + "".join(f"{(f'{r[c]*100:.1f}%' if c in r else '-'):>28}" for c in cols))

    p = ROOT / "Elite-decompiler" / "Elite (World) (Unl).nes"
    rom = p.read_bytes(); prg = prg_of(rom); cdl = list((SCRATCH / "elite.cdl").read_bytes())
    truth = elite_truth(ROOT / "nes-elite-beebasm/2-build-files/py8dis/source-files", len(prg))
    table("Elite (MMC1, 128K): Moxon source + CPU log", [
        ("linear sweep (ours)", score(from_listing(linear(rom), len(prg)), truth, cdl)),
        ("nes-disasm6", score(from_listing(nes_disasm6(p, cfg), len(prg)), truth, cdl)),
        ("recursive descent, cold", score(recursive_descent(prg, [(0x1C000, 0xC000, 0x4000)], vectors(prg, 0x1C000)), truth, cdl))])

    M = ROOT / "game-annotation" / "mappy"
    p = M / "Mappy (Japan).nes"; rom = p.read_bytes(); prg = prg_of(rom); cdl = list((SCRATCH / "mappy.cdl").read_bytes())
    rows = [("linear sweep (ours)", score(from_listing(linear(rom), len(prg)), cdl=cdl)),
            ("nes-disasm6", score(from_listing(nes_disasm6(p, cfg), len(prg)), cdl=cdl)),
            ("recursive descent, cold", score(recursive_descent(prg, [(0, 0x8000, 0x4000), (0, 0xC000, 0x4000)], vectors(prg, 0)), cdl=cdl))]
    for name, f in (("mappy trace (Python 6502)", "coverage.bin"), ("mappy static_expand", "coverage_static.bin")):
        if (M / f).exists():                       # local outputs of the Mappy pipeline
            rows.append((name, score(from_bitmap((M / f).read_bytes()), cdl=cdl)))
    table("Mappy (NROM, 16K): CPU log of the attract demo", rows)

    U = ROOT / "UW-decompile"
    p = U / "Uncharted Waters (USA).nes"; rom = p.read_bytes(); prg = prg_of(rom); cdl = list((SCRATCH / "uw.cdl").read_bytes())
    nat = sorted(glob.glob(str(U / "source/1-asm-6502/bank*.asm")))
    banks = {int(Path(x).stem[4:]) for x in nat}
    scoped = [f if i // 0x2000 in banks else 0 for i, f in enumerate(cdl)]
    lin, nd6 = from_listing(linear(rom), len(prg)), from_listing(nes_disasm6(p, cfg), len(prg))
    rd = recursive_descent(prg, [(len(prg) - 0x4000, 0xC000, 0x4000)], vectors(prg, len(prg) - 0x4000))
    table("Uncharted Waters (KOEI MMC5, 512K): CPU log, boot + title", [
        ("linear sweep (ours)", score(lin, cdl=cdl)), ("nes-disasm6", score(nd6, cdl=cdl)),
        ("recursive descent, cold", score(rd, cdl=cdl))])
    table(f"Uncharted Waters, the {len(banks)} banks UW disasm_native covers", [
        ("linear sweep (ours)", score(lin, cdl=scoped)), ("nes-disasm6", score(nd6, cdl=scoped)),
        ("UW disasm_native", score(from_uw_native(nat, len(prg)), cdl=scoped))])


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
