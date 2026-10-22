"""Mesen 2 label files (.mlb): Symbols out, in the format Mesen itself exports.

Grounding: Mesen 2 source, UI/Debugger/Labels/{CodeLabel,LabelManager,MesenLabelFile}.cs.

    Type:Address:Label[:Comment]

- Type is the MemoryType enum name. Mesen also reads the legacy prefixes P/R/S/W/G,
  so older files still load, but it writes the long names and so do we.
- Address is hex, at least 4 digits ("X4"), relative to the start of that memory.
- Mesen splits on the first three colons only, so a comment may contain colons. It writes
  newlines as the two characters "\\n" and reads them back; the comment is omitted when blank.
- A label must match LabelRegex, ^[@_a-zA-Z]+[@_a-zA-Z0-9]*$, or Mesen counts the line as an
  error and drops it.
- Two labels at the same (type, address): Mesen keeps the LAST it reads. We write one per key
  (the first given) and report the rest, so what the file says is what Mesen shows.

Mesen auto-loads <rom name>.mlb from beside the ROM.
"""
from __future__ import annotations

import re
from pathlib import Path

from ...core.symbols import Symbol

MEMORY_TYPE = {
    "nes.prg_rom": "NesPrgRom",
    "nes.internal_ram": "NesInternalRam",
    "nes.save_ram": "NesSaveRam",
    "nes.work_ram": "NesWorkRam",
    "nes.cpu_bus": "NesMemory",
    "snes.prg_rom": "SnesPrgRom",
    "snes.work_ram": "SnesWorkRam",
}
LABEL_RE = re.compile(r"^[@_a-zA-Z]+[@_a-zA-Z0-9]*$")


def format_label(sym: Symbol) -> str:
    """One .mlb line, as Mesen's CodeLabel.ToString writes it."""
    line = f"{MEMORY_TYPE[sym.region]}:{sym.offset:04X}:{sym.name}"
    if sym.comment.strip():
        line += ":" + sym.comment.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return line


def render_mlb(symbols) -> tuple[str, list[str]]:
    """-> (file text, problems). Problems are lines Mesen would drop or overwrite."""
    lines, problems, seen = [], [], {}
    for sym in symbols:
        if not LABEL_RE.match(sym.name):
            problems.append(f"invalid name {sym.name!r} ({sym.region} {sym.offset:04X}): Mesen rejects it")
            continue
        key = (sym.region, sym.offset)
        if key in seen:
            problems.append(f"{sym.region} {sym.offset:04X}: {sym.name!r} dropped, {seen[key]!r} kept")
            continue
        seen[key] = sym.name
        lines.append(format_label(sym))
    return "\n".join(lines) + "\n", problems


def write_mlb(symbols, path: str | Path) -> list[str]:
    text, problems = render_mlb(symbols)
    Path(path).write_text(text, encoding="utf-8", newline="\n")
    return problems
