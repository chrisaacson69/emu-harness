"""Relocation-invariant 6502 code signatures: "the same routine, reassembled somewhere else".

From ro3k-decompiler tools/handler_match.py (row E of MIGRATION.md), where it named KOEI handlers
across eight titles. A signature is the instruction stream from an entry to the first rts/rti,
one token per instruction:

    keep       opcode, addressing mode, immediates, zero-page and low (<$8000) absolute operands
               (RAM/MMIO layout tends to be stable across builds of one engine)
    normalize  ROM operands ($8000-$FFFF, which relocate) and branch targets
    '.db'      a byte that is not a documented opcode

Two routines compare by difflib ratio: ~1.0 = relocated-identical, lower = diverged.
"""
from __future__ import annotations

import difflib

from .disasm import ABS, ABX, ABY, IMM, IND, IZX, IZY, MODE_LEN, OPS, REL, ZP, ZPX, ZPY


def signature(buf: bytes, entry: int, cap: int = 64, base: int = 0xC000) -> list[str]:
    """Tokens from `entry` (a CPU address; `buf` is mapped at `base`) to the first rts/rti or `cap`."""
    toks, i, n = [], entry - base, 0
    while 0 <= i < len(buf) and n < cap:
        op = buf[i]
        if op not in OPS:
            toks.append(".db"); i += 1; n += 1
            continue
        mn, mode = OPS[op]
        ln = 1 + MODE_LEN[mode]
        if i + ln > len(buf):               # truncated at the end of the buffer
            break
        lo = buf[i + 1] if ln >= 2 else 0
        hi = buf[i + 2] if ln >= 3 else 0
        if mode == IMM:
            toks.append(f"{mn}#{lo:02x}")
        elif mode in (ZP, ZPX, ZPY, IZX, IZY):
            toks.append(f"{mn}/{mode}/{lo:02x}")
        elif mode in (ABS, ABX, ABY, IND):
            a = lo | hi << 8
            toks.append(f"{mn}/{mode}/{'ROM' if a >= 0x8000 else f'{a:04x}'}")
        else:                               # implied, accumulator, branch (target normalized)
            toks.append(mn)
        n += 1
        if op in (0x60, 0x40):              # rts / rti -> end of routine
            break
        i += ln
    return toks


def best_match(sig: list[str], candidates: dict[int, list[str]]) -> tuple[int | None, float]:
    """-> (address of the most similar candidate signature, its ratio). Ties keep the first seen."""
    best_a, best = None, 0.0
    for a, s in candidates.items():
        sc = difflib.SequenceMatcher(None, sig, s).ratio()
        if sc > best:
            best, best_a = sc, a
    return best_a, best
