"""Apply a label registry to a 6502 disassembly listing: addresses become names (row A3 of MIGRATION.md).

Two listing dialects, one module:

  disasm6   nes-disasm6 output (NA1's `source/1-asm-6502/bank_NN.asm`): `b{N}_{addr}` label tokens,
            `$XX` / `$XXXX` operands, and a trailing `; PRGOFFSET: rawbytes` comment per line.
            From na1-decompiler tools/mesen-labels.py `--asm` (the mature copy of game-annotation's
            asm-relabel): code tokens and RAM operands renamed, the trailing comment protected, and a
            `name:` anchor inserted where a fixed-bank label's PRG offset starts an unanchored line.
  sweep     our linear-sweep listings (`cpu/m6502/disasm`; Elite's `source/asm/bankN.asm`): `bNN_xxxx:`
            definitions and `$..` operands, resolved by where the address lives -- RAM, the fixed
            bank, or (a heuristic) the listing's own bank. From elite-decompiler tools/apply_labels.py.

The fixed bank is a parameter here (NA1 15, Elite 7); the copies hardcoded it. Names come from the
shared registry reader (core/symbols).
"""
from __future__ import annotations

import collections
import re

from emu_harness.core.symbols import read_registry

_BANK_SECTION = re.compile(r"prg\.bank(\d+)$")


def registry_maps(path):
    """-> (ram {addr: name} for [ram], banks {bank: {addr: name}} for [prg.bankN]); first entry wins."""
    ram, banks = {}, collections.defaultdict(dict)
    for e in read_registry(path).entries:
        if e.section == "ram":
            ram.setdefault(e.addr, e.name)
        else:
            m = _BANK_SECTION.match(e.section)
            if m:
                banks[int(m.group(1))].setdefault(e.addr, e.name)
    return ram, banks


# ---- disasm6 ---------------------------------------------------------------------------------
CODE_TOKEN_RE = re.compile(r"\bb(\d+)_([0-9a-fA-F]{4})\b")
# (?<!#) so an IMMEDIATE constant `#$73` is never mistaken for a reference to
# address $0073 -- only true memory operands ($73 / $0073 / $0073,x) get labeled.
RAM_OPERAND_RE = re.compile(r"(?<!#)\$([0-9a-fA-F]{2,4})\b")
# A line already carries an anchor iff it begins with `ident:` (labels are the
# only line-start token followed by a colon; mnemonics/`hex` never are).
EXISTING_ANCHOR_RE = re.compile(r"^\s*[A-Za-z_]\w*:")
# The final per-line comment encodes the PRG file offset: `; 3C22C:  ad 73 00`.
TRAIL_OFFSET_RE = re.compile(r";\s*([0-9A-Fa-f]{4,6}):")
# Column the disassembler indents instruction bodies to (matches existing anchors
# like `b15_c54f:   hex` = 9 + 3 = 12). Anchors longer than this just shift right.
ANCHOR_COL = 12


def disasm6_maps(path, fixed_bank, fixed_lo=0xC000):
    """-> (code {(bank, addr): name}, ram {addr: name} for $0000-$7FFF).

    A label at or above `fixed_lo` is the FIXED bank's, whichever [prg.bankN] section it was filed
    under: the address is authoritative there (NA1 files ~150 kernel labels under [prg.bank0]).
    $8000 up to `fixed_lo` is switchable, so the section decides."""
    code, ram = {}, {}
    sec_ram, banks = registry_maps(path)
    for bank, entries in banks.items():
        for addr, name in entries.items():
            code[(fixed_bank if addr >= fixed_lo else bank, addr)] = name
    for addr, name in sec_ram.items():
        if addr <= 0x7FFF:
            ram[addr] = name
    return code, ram


def fixed_anchors(code, fixed_bank, bank_size=0x4000, fixed_lo=0xC000):
    """PRG file offset -> name, for the fixed bank's labels only. Those are address-authoritative,
    so anchoring them carries no section risk; a switchable-window label filed under the wrong
    section would anchor the wrong bank's code, so those are left out."""
    return {fixed_bank * bank_size + (addr & (bank_size - 1)): name
            for (bank, addr), name in code.items() if bank == fixed_bank and addr >= fixed_lo}


def relabel_disasm6(text, code, ram, anchors=None):
    """Rewrite a raw disasm6 listing. `anchors`, if given, maps PRG file offset -> name and INSERTS
    a `name:` anchor on any line whose trailing `; OFFSET:` matches and that has no anchor yet --
    recovering jump-table-reached entries (e.g. syscall handlers) the recursive-descent
    disassembler never anchored. -> (text, code names applied, RAM names applied, [(offset, name)])"""
    n_code = [0]
    n_ram = [0]
    inserted = []

    def code_sub(m):
        bank, addr = int(m.group(1)), int(m.group(2), 16)
        name = code.get((bank, addr))
        if name:
            n_code[0] += 1
            return name
        return m.group(0)

    text = CODE_TOKEN_RE.sub(code_sub, text)

    def ram_sub(m):
        addr = int(m.group(1), 16)
        name = ram.get(addr)
        if name:
            n_ram[0] += 1
            return name
        return m.group(0)

    out_lines = []
    for line in text.splitlines():
        # Protect ONLY the final `; PRGOFFSET: rawbytes` comment (it encodes addresses as bare hex
        # that must NOT be rewritten). Everything before it is fair game -- crucially the
        # decoded-instruction comment on `hex 8d 73 00 ; sta $0073` lines (absolute zero-page
        # access forced as raw bytes), so those labeled variables get substituted too.
        idx = line.rfind(";")
        if idx == -1:
            line = RAM_OPERAND_RE.sub(ram_sub, line)
        else:
            line = RAM_OPERAND_RE.sub(ram_sub, line[:idx]) + line[idx:]

        # Anchor insertion: only on a still-unanchored line whose trailing offset exactly matches a
        # requested address. The exact line-start match is the safety -- native routines start a
        # line, while fixed-bank VM-bytecode subs sit mid-`hex`-line so they never match here.
        if anchors and not EXISTING_ANCHOR_RE.match(line):
            mo = TRAIL_OFFSET_RE.search(line)
            if mo:
                off = int(mo.group(1), 16)
                name = anchors.get(off)
                if name:
                    ind = re.match(r"^( +)", line)
                    indent = ind.group(1) if ind else ""
                    body = line[len(indent):]
                    anchor = name + ":"
                    pad = max(1, ANCHOR_COL - len(anchor))
                    line = anchor + (" " * pad) + body
                    inserted.append((off, name))

        out_lines.append(line)
    return "\n".join(out_lines), n_code[0], n_ram[0], inserted


# ---- sweep -----------------------------------------------------------------------------------
LABELDEF = re.compile(r'^b(\d\d)_([0-9a-f]{4}):')
# an operand address, but never an immediate (#$..) -- keep any ( prefix
OPERAND = re.compile(r'(?<!#)(?<![0-9a-zA-Z_])\$([0-9a-f]{2,4})\b')


def relabel_sweep(text, bank, ram, banks, fixed_bank, fixed_lo=0xC000, window=(0x8000, 0xC000)):
    """Rewrite one bank's linear-sweep listing. -> (text, Counter(renamed_defs, resolved, numeric)).

    An operand below $8000 resolves through [ram]; at or above `fixed_lo` through the fixed bank;
    inside the switchable `window`, through the listing's OWN bank -- a heuristic that holds when
    cross-bank calls go through trampolines in the fixed bank (Elite's do), so in the fixed bank's
    own listing such references are left numeric. The trailing `; ...` comment is never touched."""
    fixed = banks.get(fixed_bank, {})
    own = banks.get(bank, {})
    out, stats = [], collections.Counter()

    def resolve(addr):
        if addr < window[0]:
            return ram.get(addr)
        if addr >= fixed_lo:
            return fixed.get(addr)
        return own.get(addr) if bank != fixed_bank else None

    def sub(m):
        nm = resolve(int(m.group(1), 16))
        if nm:
            stats["resolved"] += 1
            return nm
        stats["numeric"] += 1
        return m.group(0)

    for line in text.splitlines():
        body, sep, tail = line.partition(";")
        if body.lstrip().startswith("."):          # directive: leave alone
            out.append(line)
            continue
        body = OPERAND.sub(sub, body)
        d = LABELDEF.match(body)
        if d:
            nm = own.get(int(d.group(2), 16))
            if nm:
                body = nm + ":" + body[d.end():]
                stats["renamed_defs"] += 1
        out.append(body + sep + tail)
    return "\n".join(out) + "\n", stats
