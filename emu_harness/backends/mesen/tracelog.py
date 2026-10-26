"""Mesen's trace logger text format -> one Step per executed instruction (row Q).

A line (the fullest layout na1's traces use):

  E869  LDA (ptr3_lo),Y [$A2F2] = $3C    A:A2 X:E9 Y:00 S:F9 P:nv--dIZc Fr:17190 Cycle:511900188 BC:B1 06

The disassembly text may show labels instead of addresses, so the instruction's own bytes (`BC:`)
are kept: with the registers they give every effective address (see machines/nes/trace).
`[$XXXX]` is Mesen's resolved address, `= $XX` the value at it before the instruction ran. Register
values are the state before the instruction. Mesen's columns are configurable: A X Y are required,
S P Fr Cycle BC are read when present. Lines that are not instructions are skipped.
"""
from __future__ import annotations

import gzip
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_LINE = re.compile(r"^([0-9A-F]{4}) +(.*?) +(A:[0-9A-F]{2} .*)$")
_COL = re.compile(r"(\w+):(\S+)")
_ADDR = re.compile(r"\[\$([0-9A-F]{4})\]")
_OLD = re.compile(r"= \$([0-9A-F]{2})")


@dataclass(frozen=True, slots=True)
class Step:
    pc: int
    text: str            # Mesen's disassembly, labels and all
    a: int
    x: int
    y: int
    s: int
    p: str
    frame: int | None
    cycle: int | None
    code: bytes          # the instruction bytes (BC:), empty if the log has no BC column
    addr: int | None     # Mesen's resolved effective address, when it printed one
    old: int | None      # the "= $XX" value

    @property
    def opcode(self) -> int | None:
        return self.code[0] if self.code else None


def parse_line(line: str) -> Step | None:
    m = _LINE.match(line.rstrip("\n"))
    if not m:
        return None
    pc, text, tail = m.groups()
    regs, _, bc = tail.partition(" BC:")
    c = dict(_COL.findall(regs))
    try:
        a, x, y = int(c["A"], 16), int(c["X"], 16), int(c["Y"], 16)
    except KeyError:
        return None
    am, om = _ADDR.search(text), _OLD.search(text)
    return Step(int(pc, 16), text, a, x, y, int(c.get("S", "0"), 16), c.get("P", ""),
                int(c["Fr"]) if "Fr" in c else None, int(c["Cycle"]) if "Cycle" in c else None,
                bytes.fromhex(bc) if bc else b"", int(am.group(1), 16) if am else None,
                int(om.group(1), 16) if om else None)


def read_tracelog(path: Path) -> Iterator[Step]:
    """Steps from a trace log, in order; a .gz log is read transparently."""
    path = Path(path)
    f = gzip.open(path, "rt", encoding="latin-1") if path.suffix == ".gz" else open(path, encoding="latin-1")
    with f:
        for line in f:
            st = parse_line(line)
            if st is not None:
                yield st
