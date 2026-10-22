"""Symbols: named addresses, in a vocabulary every layer shares.

A Symbol is a name at an offset inside a memory *region*. Regions are console memories
("nes.prg_rom", "nes.internal_ram", ...); a machine decides which region an address lives in,
a backend decides what its emulator calls that region. Neither needs the other.

Also the reader for the bank-sectioned label registry the decompiler repos keep
(`mesen-labels.toml`):

    [prg.bank15]
    "0xC577" = { name = "set_prg_bank", comment = "..." }
    [ram]
    "0x0073" = { name = "prg_bank_shadow" }

Most of those files are not valid TOML as a whole (duplicate keys, repeated sections, unescaped
quotes in comments), so each entry is parsed on its own with tomllib, and every way a file
departs from TOML is reported as a Defect instead of being silently tolerated.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REGIONS = (
    "nes.prg_rom",        # offset into the PRG image (iNES header excluded)
    "nes.internal_ram",   # $0000-$07FF
    "nes.save_ram",       # cartridge RAM at $6000-$7FFF, battery-backed
    "nes.work_ram",       # cartridge RAM at $6000-$7FFF, no battery
    "nes.cpu_bus",        # anything else, by CPU address (registers, mirrors, unmapped)
    "snes.prg_rom",       # offset into the ROM file (copier header excluded)
    "snes.work_ram",      # $7E:0000-$7F:FFFF as one 128 KiB space
)


@dataclass(frozen=True)
class Symbol:
    region: str
    offset: int
    name: str
    comment: str = ""

    def __post_init__(self):
        if self.region not in REGIONS:
            raise ValueError(f"unknown region {self.region!r}")


@dataclass(frozen=True)
class Entry:
    """One registry line: a name at a CPU address, under a section such as 'prg.bank15'."""
    section: str
    addr: int
    name: str
    comment: str
    line: int


@dataclass(frozen=True)
class Defect:
    line: int
    kind: str      # "invalid-toml" | "unparsed" | "duplicate" | "conflict" | "repeated-section"
    detail: str


@dataclass
class Registry:
    path: Path
    entries: list[Entry] = field(default_factory=list)
    defects: list[Defect] = field(default_factory=list)


SECTION_RE = re.compile(r'^\[([^\]]+)\]\s*(?:#.*)?$')
KEY_RE = re.compile(r'^"0x([0-9A-Fa-f]+)"\s*=\s*(.*)$')
# Fallback for values tomllib rejects (in practice: unescaped quotes inside a comment).
# The name never contains a quote; the comment runs to the last quote before the closing brace.
_LOOSE = re.compile(r'^\{\s*name\s*=\s*"([^"]*)"(?:\s*,\s*comment\s*=\s*"(.*)")?\s*\}\s*(?:#.*)?$')


def _loose_unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


def toml_string(s: str) -> str:
    """A TOML basic string, escaped per the spec (the escaping the hand-written writers skipped)."""
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"' + "".join(c if ord(c) >= 0x20 and c != "\x7f" else f"\\u{ord(c):04X}" for c in out) + '"'


def format_entry(key: str, name: str, comment: str = "") -> str:
    """One registry line, valid TOML. `key` is the address text as written, e.g. '0xC577'."""
    body = f"name = {toml_string(name)}" + (f", comment = {toml_string(comment)}" if comment else "")
    return f'"{key}" = {{ {body} }}'


def read_registry(path: str | Path) -> Registry:
    """Read a bank-sectioned label registry. Entries keep file order; the first entry for a
    (section, address) wins and every later one is reported."""
    reg = Registry(Path(path))
    section, sections, seen = None, set(), {}
    for n, raw in enumerate(reg.path.read_text(encoding="utf-8").splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = SECTION_RE.match(s)
        if m:
            section = m.group(1).strip()
            if section in sections:
                reg.defects.append(Defect(n, "repeated-section", section))
            sections.add(section)
            continue
        k = KEY_RE.match(s)
        if not k:
            if s.startswith('"0x'):
                reg.defects.append(Defect(n, "unparsed", s[:60]))
            continue
        if section is None:
            reg.defects.append(Defect(n, "unparsed", "entry before any [section]"))
            continue
        try:
            value = tomllib.loads("v = " + k.group(2))["v"]
            name, comment = value["name"], value.get("comment", "")
        except (tomllib.TOMLDecodeError, KeyError, TypeError) as e:
            loose = _LOOSE.match(k.group(2))
            if not loose:
                reg.defects.append(Defect(n, "unparsed", s[:60]))
                continue
            name, comment = loose.group(1), _loose_unescape(loose.group(2) or "")
            reg.defects.append(Defect(n, "invalid-toml", str(e).split(" (at")[0]))
        addr = int(k.group(1), 16)
        first = seen.get((section, addr))
        if first is not None:
            kind = "duplicate" if first.name == name else "conflict"
            reg.defects.append(Defect(n, kind, f"[{section}] ${addr:04X} {name!r}; kept {first.name!r} "
                                               f"(line {first.line})"))
            continue
        e = Entry(section, addr, name, comment, n)
        seen[(section, addr)] = e
        reg.entries.append(e)
    return reg
