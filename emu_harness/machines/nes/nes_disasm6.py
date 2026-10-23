"""nes-disasm6 as a plugin: the best generic 6502 code/data separator we measured (MIGRATION.md row B).

github.com/mcgrew/nes-disasm6, Apache-2.0. Run from a local clone, never vendored; the clone's
folder is `nes_disasm6` in config.toml. It reads an iNES file and emits a disasm6 listing, which
`cpu.m6502.disasm.listing_verdicts` turns into one verdict per PRG byte.

Where to use it: the regions a seeded descent (`cpu.m6502.descent`) cannot reach. It does not know
KOEI's inline-operand calls (3 data bytes after `JSR $E299` / `$E338`), so on a KOEI bank a
descent with `inline=` beats it.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from emu_harness.cpu.m6502.disasm import listing_verdicts


def run(rom: Path, tool: Path) -> str:
    """The listing for `rom`, from the clone at `tool`. `-r`: no CHR output (the README's `-c` is
    the CHR size in this version)."""
    with tempfile.TemporaryDirectory() as cwd:          # it may drop files next to itself
        r = subprocess.run([sys.executable, "-I", str(Path(tool) / "disasm.py"), "--stdout", "-r", str(rom)],
                           capture_output=True, text=True, cwd=cwd)
    if r.returncode:
        raise RuntimeError(f"nes-disasm6 failed: {r.stderr[-300:]}")
    return r.stdout


def verdicts(rom: Path, tool: Path) -> list[str]:
    """One verdict per PRG byte ("I" start, "O" operand, "D" data, "" unlisted)."""
    data = Path(rom).read_bytes()
    return listing_verdicts(run(rom, tool), data[4] * 0x4000)
