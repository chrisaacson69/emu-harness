"""Recursive descent: follow the 6502's control flow from known entries, so code separates from data.

One walker for the copies that grew in six title repos (MIGRATION.md row B): ro3k `subs.reach`,
GemFire `inbank_native`, UW `disasm_native`, Mappy `static_expand`, koei-nes `native_floor.reach`,
and the cold contender in `tools/disasm_bench.py`. The disasm_bench scores show why it is worth
having: a linear sweep calls about two thirds of all data "code", while descent never does. It
only finds what its seeds lead to, though, so a title supplies good ones (vectors, dispatch tables,
the CPU log, a profile).

A routine's flow (`flow`) follows the fall-through and both sides of every branch, follows `JMP abs`
inside the window, and ends at rts / rti / `JMP (ind)`, an undocumented opcode (we ran into data),
a known data byte, or optionally `BRK`. A `JSR` falls through: past its operand, or past N more
inline bytes when the target is an inline-operand bridge (KOEI's `$E299` / `$E338` read 3 bytes
after the call and return beyond them). `descend` treats every followed JSR target as a new entry
and walks each entry to a fixpoint.

Addresses are CPU addresses. A `Window` maps one ROM slice at a base, optionally mirrored (NROM-128
shows its 16K at both $8000 and $C000); results use the canonical address, `base + offset`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from .disasm import MODE_LEN, OPS, REL

JSR, JMP, JMP_IND, RTS, RTI, BRK = 0x20, 0x4C, 0x6C, 0x60, 0x40, 0x00


class Window:
    """`buf` as the CPU sees it at `base`, and again at each of `mirrors`."""

    def __init__(self, buf: bytes, base: int, mirrors: Iterable[int] = ()):
        self.buf, self.base, self.bases = buf, base, (base, *mirrors)

    def offset(self, addr: int) -> int | None:
        for b in self.bases:
            if b <= addr < b + len(self.buf):
                return addr - b
        return None

    def __contains__(self, addr: int) -> bool:
        return self.offset(addr) is not None

    def canon(self, addr: int) -> int:
        return self.base + self.offset(addr)


@dataclass
class Flow:
    """One routine's control flow, without descending into the routines it calls."""
    entry: int
    starts: set[int] = field(default_factory=set)       # instruction starts
    operands: set[int] = field(default_factory=set)     # operand bytes of those instructions
    inline: set[int] = field(default_factory=set)       # inline-bridge operand bytes (data)
    calls: dict[int, int] = field(default_factory=dict)  # JSR site -> target (any address)
    jumps: dict[int, int] = field(default_factory=dict)  # JMP abs site -> target (any address)


def flow(win: Window, entry: int, *, inline: dict[int, int] | None = None,
         follow: Callable[[int], bool] | None = None, data: frozenset[int] = frozenset(),
         stop_brk: bool = False, end: int | None = None, into_jmp: bool = True) -> Flow:
    """Walk one routine from `entry`. `follow(addr)` vetoes entering a JMP target (e.g. a KOEI
    bytecode stub); `end` stops at an address (the next routine); `into_jmp=False` ends a path at
    any JMP instead of continuing at its target."""
    inline = inline or {}
    buf, f, work = win.buf, Flow(entry), [entry]
    while work:
        pc = work.pop()
        while (o := win.offset(pc)) is not None:
            a = win.base + o
            if a in f.starts or a in data or (end is not None and a >= end):
                break
            op = buf[o]
            if op not in OPS:
                break
            ln = 1 + MODE_LEN[OPS[op][1]]
            if o + ln > len(buf):                     # truncated at the end of the window
                break
            f.starts.add(a)
            f.operands.update(range(a + 1, a + ln))
            nxt = (pc + ln) & 0xFFFF
            tgt = buf[o + 1] | buf[o + 2] << 8 if ln == 3 else None
            if OPS[op][1] == REL:
                work.append((nxt + ((buf[o + 1] ^ 0x80) - 0x80)) & 0xFFFF)
            elif op == JSR:
                f.calls[a] = tgt
                if tgt in inline:                     # the bridge returns past its inline bytes
                    f.inline.update(range(a + ln, a + ln + inline[tgt]))
                    nxt = (nxt + inline[tgt]) & 0xFFFF
            elif op == JMP:
                f.jumps[a] = tgt
                if into_jmp and tgt in win and (follow is None or follow(tgt)):
                    work.append(tgt)
                break
            if op in (RTS, RTI, JMP_IND) or (stop_brk and op == BRK):
                break
            pc = nxt
    return f


@dataclass
class Descent:
    """Every routine reached from the seeds, each with its own flow."""
    entries: set[int]
    flows: dict[int, Flow]

    def union(self, attr: str) -> set[int]:
        return set().union(*(getattr(f, attr) for f in self.flows.values()))

    @property
    def starts(self) -> set[int]:
        return self.union("starts")

    @property
    def operands(self) -> set[int]:
        return self.union("operands")

    @property
    def inline(self) -> set[int]:
        return self.union("inline")

    def overlaps(self) -> set[int]:
        """Bytes one flow decodes as an instruction start and another as an operand: a path ran
        into the middle of an instruction. Usually one path is wrong (data reached by a bad branch);
        sometimes it is a real trick (the `BIT abs` opcode that hides a 2-byte instruction). Reported,
        not resolved: which flow got there first says nothing about which is right."""
        return self.starts & self.operands

    def callers(self) -> dict[int, set[int]]:
        """callee -> entries whose own flow calls it (a site shared by two flows counts for both)."""
        out: dict[int, set[int]] = {}
        for e, f in self.flows.items():
            for t in f.calls.values():
                out.setdefault(t, set()).add(e)
        return out


def descend(win: Window, seeds: Iterable[int], *, follow: Callable[[int], bool] | None = None,
            **kw) -> Descent:
    """Walk every seed in the window, then every JSR target in the window that `follow` allows,
    to a fixpoint. Keywords pass to `flow`."""
    entries = {win.canon(s) for s in seeds if s in win}
    flows: dict[int, Flow] = {}
    work = sorted(entries)
    while work:
        e = work.pop()
        if e in flows:
            continue
        flows[e] = f = flow(win, e, follow=follow, **kw)
        for t in f.calls.values():
            if t in win and (follow is None or follow(t)) and win.canon(t) not in entries:
                entries.add(win.canon(t))
                work.append(win.canon(t))
    return Descent(entries, flows)


def gaps(win: Window, covered: set[int], *, min_len: int = 8, min_ops: int = 6,
         pad: int = 0xFF) -> list[int]:
    """Starts of uncovered runs that look like a routine nobody reached by a followed call (one
    reached through a jump table or from bytecode): at least `min_len` bytes, not beginning with
    the `pad` fill byte, and decoding as `min_ops` documented instructions. A guess, so a caller
    seeds these only where bytecode cannot masquerade as code."""
    buf, out, run = win.buf, [], None
    for o in range(len(buf) + 1):
        if o < len(buf) and win.base + o not in covered:
            run = o if run is None else run
            continue
        if run is not None and o - run >= min_len and buf[run] != pad:
            i, n = run, 0
            while i < o and n < min_ops and buf[i] in OPS:
                i += 1 + MODE_LEN[OPS[buf[i]][1]]
                n += 1
            if n >= min_ops:
                out.append(win.base + run)
        run = None
    return out


def verdicts(win: Window, starts: set[int], operands: set[int] = frozenset()) -> list[str]:
    """One verdict per window byte: "I" instruction start, "O" operand, "" not reached.
    A byte that is a start in one flow and an operand in another stays "I"."""
    return ["I" if win.base + o in starts else "O" if win.base + o in operands else ""
            for o in range(len(win.buf))]
