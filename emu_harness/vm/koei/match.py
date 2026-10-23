"""Cross-title KOEI bytecode matching: "the same routine, in another title".

Many KOEI titles share bytecode routines (a library carried from game to game), so a routine named
in one title names its twin in the next. A signature is a routine's instruction stream, one token
per instruction, built from the DECODED bytes through the one opcode table (vm_opcode_names.toml):

    keep       the opcode (ext ops: $B7 + the ext byte), immediates, frame offsets, and absolute
               operands below `rom_floor` (RAM / SRAM layout is stable across one engine's builds)
    normalize  ROM operands (>= rom_floor, which relocate) and every control-transfer target
               (jumps, branches, switch cases) to 'A'

From ro3k-decompiler tools/bc_match.py (row E of MIGRATION.md), which tokenized listing TEXT: two
listings rendered with different opcode names (ro3k's `PUSH_quick` vs NA1's `PUSH_qimm`, the same
byte) could not match, so it under-counted sharing. Decoding the bytes makes the names irrelevant.

Two routines compare by difflib ratio: 1.0 = relocated-identical, lower = diverged.
"""
from __future__ import annotations

import difflib

from emu_harness.vm.koei.isa import TABLE, TYPE_SIZE, SIGNED

ADDR_ROLES = {"target", "default", "case"}           # always control transfers -> relocate


def _fields(fields, raw, i, vals, rep_counts):
    """Walk `fields` over raw[i:], appending (role, type, value); returns the next index."""
    for f in fields:
        n = 1
        if f.repeat:
            n = rep_counts.get(f.repeat, 0)
        for _ in range(n):
            if f.group:
                i = _fields(f.group, raw, i, vals, rep_counts)
                continue
            sz = TYPE_SIZE[f.type]
            v = int.from_bytes(bytes(raw[i:i + sz]), "little", signed=f.type in SIGNED)
            vals.append((f.role, f.type, v))
            rep_counts[f.role] = v
            i += sz
    return i


def token(raw, rom_floor=0x8000):
    """One instruction's token from its raw bytes (opcode first)."""
    op = raw[0]
    if op < 0x80:
        return "%02X" % op                             # nibble ops: the operand is in the opcode
    ext = op == 0xB7 and len(raw) > 1
    o = TABLE.op(raw[1], ext=True) if ext else TABLE.op(op)
    head = "B7.%02X" % raw[1] if ext else "%02X" % op
    if o is None:
        return head + "?"
    vals = []
    try:
        _fields(o.fields, raw, 2 if ext else 1, vals, {})
    except (IndexError, KeyError):
        return head + "!"
    branch = o.cf in ("jmp", "cjmp", "bra", "cbra", "switch", "call")
    parts = [head]
    for role, typ, v in vals:
        if typ == "r8" or role in ADDR_ROLES or (typ == "a16" and (branch or v >= rom_floor)):
            parts.append("A")
        elif typ == "a16":
            parts.append("$%04X" % v)
        else:
            parts.append(str(v))
    return " ".join(parts)


def signature(instrs, rom_floor=0x8000):
    """A routine's tokens from its decoded instructions (each a raw-bytes sequence)."""
    return [token(r, rom_floor) for r in instrs]


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def best_match(sig, candidates, floor=0.0):
    """-> (key, score) of the most similar candidate; ties keep the first seen. Candidates whose
    upper bound (length / multiset) cannot beat the current best or `floor` are skipped."""
    best_k, best = None, floor
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(sig)
    for k, s in candidates.items():
        sm.set_seq1(s)
        if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
            continue
        sc = sm.ratio()
        if sc > best:
            best, best_k = sc, k
    return best_k, (best if best_k is not None else 0.0)
