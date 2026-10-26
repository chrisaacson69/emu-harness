"""Static RE of a native 6502 program: finding what neither the trace nor the descent sees (row R).

Lifted from Mappy's pipeline (game-annotation mappy/: static_expand, jump_tables, stored_pointers,
pointer_tables, label_leaves, obj_record), which took a 16K NROM game from a trace's 31% code
coverage to 64% and a connected call graph. Each pass is an idiom that hides code from descent:

  dispatch_tables          LDA tbl,X / STA ptr / LDA tbl+1,X / STA ptr+1 / JMP (ptr):
                           back-scan each JMP (ind) to its indexed load, read the word table,
                           seed every handler, mark the table as data, repeat to a fixpoint
  stored_pointer_installs  LDA #<h / STA v / LDA #>h / STA v+1: a handler installed by data flow
  pointer_runs             3+ consecutive words in undecoded bytes that each point at decoded code
  explain                  names the deliberate idioms behind a walker conflict (`conflicts`)
  partition_edges          a call graph by the contiguous partition (an instruction belongs to the
                           greatest entry at or below it): JSR / JMP abs edges
  field_census             a record's fields from (ptr),Y with a constant Y and from table,X
  routine_listing          a routine's listing plus a signature (hardware touched, zero page,
                           RAM, loop shape, size), for naming from evidence

`NativeMap` holds the program window and two bitmaps over it: `code` (decoded) and `is_data`
(proven data; descent stops there). Addresses are CPU addresses; offsets wrap inside the window as
the original `& $3FFF` did, so a window should be a power of two aligned at its base.
"""
from __future__ import annotations

import bisect
from collections import deque

from ..cpu.m6502.descent import Window, descend
from ..cpu.m6502.disasm import ABS, ABX, ABY, IMM, IZY, MODE_LEN, OPS, ZP, ZPX, ZPY, fmt_operand

JSR, JMP, JMP_IND = 0x20, 0x4C, 0x6C
BRANCHES = {0x10, 0x30, 0x50, 0x70, 0x90, 0xB0, 0xD0, 0xF0}


class NativeMap:
    def __init__(self, win: Window, code: bytearray | None = None, is_data: bytearray | None = None,
                 stop_brk: bool = True, noreturn=()):
        self.win, self.buf = win, win.buf
        self.code = code if code is not None else bytearray(len(win.buf))
        self.is_data = is_data if is_data is not None else bytearray(len(win.buf))
        self.stop_brk = stop_brk
        self.noreturn = frozenset(noreturn)   # JSR targets that never return (see descent.flow)
        self.descent = None                  # the last reach's Descent, for `conflicts`

    def off(self, a: int) -> int:
        return (a - self.win.base) % len(self.buf)

    def canon(self, a: int) -> int:
        return self.win.base + self.off(a)

    def byte(self, a: int) -> int:
        return self.buf[self.off(a)]

    def word(self, a: int) -> int:
        return self.byte(a) | self.byte(a + 1) << 8

    def in_code(self, a: int) -> bool:
        return a in self.win

    def legal(self, a: int) -> bool:
        return self.byte(a) in OPS

    def mark_data(self, base: int, nbytes: int) -> None:
        for k in range(nbytes):
            self.is_data[self.off(base + k)] = 1

    def reach(self, seeds):
        """-> (routines, instruction starts), canonical addresses. Routines are the seeds plus every
        JSR / JMP target reached; every reached byte is marked in `code`."""
        seeds = set(seeds)
        data = {self.win.base + n for n in range(len(self.buf)) if self.is_data[n]}
        d = self.descent = descend(self.win, seeds, data=data, stop_brk=self.stop_brk,
                                   noreturn=self.noreturn)
        for a in d.starts | d.operands:
            self.code[a - self.win.base] = 1
        jumped = {t for f in d.flows.values() for t in f.jumps.values() if t in self.win}
        return {self.win.canon(a) for a in seeds | jumped if a in self.win} | d.entries, d.starts

    def conflicts(self) -> dict[str, list[int]]:
        """The last reach's walker conflicts, by kind: "overlap" (a byte decoded both as an
        instruction start and as an operand) and each halt kind of `descent.flow`. The discover
        gate wants none unexplained."""
        out: dict[str, list[int]] = {}
        if self.descent is not None:
            if ov := self.descent.overlaps():
                out["overlap"] = sorted(ov)
            for a, k in sorted(self.descent.halts().items()):
                out.setdefault(k, []).append(a)
        return out


def explain(m: NativeMap, kind: str, a: int) -> str | None:
    """Name the idiom behind one walker conflict (`NativeMap.conflicts`), or None. Two are
    deliberate code, not a misread:
      "bit-skip"     an overlap inside `BIT abs` ($2C): a second entry hides in its operand
                     (ldx #1 / bit $02A2 = ldx #2 shares a tail; Mappy $F743/$F746)
      "dead-path"    a halt where a path falls past a branch that is always taken (`dead_path`)
    Anything else stays unexplained, and the discover gate counts it."""
    starts = m.descent.starts if m.descent is not None else set()
    if kind == "overlap":
        for back in (1, 2):
            if m.canon(a - back) in starts and m.byte(a - back) == 0x2C:
                return "bit-skip"
    elif dead_path(m.byte, starts, m.canon(a)):
        return "dead-path"
    return None


LOAD_IMM = {0xA9, 0xA2, 0xA0}                   # lda / ldx / ldy #imm: set Z and N from the operand
# leave Z and N alone: stores (every mode), clc sec cli sei cld sed clv, pha php, nop
KEEPS_ZN = {0x85, 0x95, 0x8D, 0x9D, 0x99, 0x81, 0x91, 0x86, 0x96, 0x8E, 0x84, 0x94, 0x8C,
            0x18, 0x38, 0x58, 0x78, 0xD8, 0xF8, 0xB8, 0x48, 0x08, 0xEA}


def dead_path(byte, starts, a, *, back: int = 4) -> bool:
    """True if the byte at `a` is reached only by falling past a branch that is always taken, so
    the fall-through never runs. `byte(addr)` reads the program, `starts` holds instruction starts.
      a branch and its complement     bne x / beq y                 (Mappy $CC3D/$CC3F)
      a flag fixed by a load-imm      ldx #$00 / beq x              (Mappy $F73F: Z=1, beq always taken)
                                      lda #$00 / sta v,x / beq x    (Mappy $F753: up to `back` stores
                                      or flag-neutral ops between, straight-line only)
    Never when something branches or jumps to `a`: then it is reached that way (Mappy $F5B8 follows
    an always-taken bpl and is the target of beq $F5B8 at $F5A0)."""
    b2 = a - 2
    if b2 not in starts or byte(b2) not in BRANCHES:
        return False
    for s in starts:
        op_s = byte(s)
        if op_s in BRANCHES and ((s + 2 + ((byte(s + 1) ^ 0x80) - 0x80)) & 0xFFFF) == a:
            return False
        if op_s in (0x4C, 0x20) and (byte(s + 1) | byte(s + 2) << 8) == a:
            return False
    op = byte(b2)
    if (a - 4) in starts and byte(a - 4) == op ^ 0x20:
        return True
    pos = b2
    for _ in range(back + 1):
        prev = next((s for s in (pos - 1, pos - 2, pos - 3)
                     if s in starts and byte(s) in OPS and s + 1 + MODE_LEN[OPS[byte(s)][1]] == pos), None)
        if prev is None:
            return False
        if byte(prev) in LOAD_IMM:
            imm = byte(prev + 1)
            return {0xF0: imm == 0, 0xD0: imm != 0, 0x30: imm >= 0x80, 0x10: imm < 0x80}.get(op, False)
        if byte(prev) not in KEEPS_ZN:
            return False
        pos = prev
    return False


def dispatch_tables(m: NativeMap, seeds: set, *, back: int = 30, max_entries: int = 64,
                    inline=(), caller_back: int = 64):
    """The indexed-dispatch idiom, to a fixpoint. `seeds` grows in place. `inline` names
    dispatchers that read a word table placed after the JSR that calls them (they join
    `m.noreturn`); each such call site is a table site too. A table ends before a word that is
    known code or a handler it already holds: Mappy's $C356 read the first instruction of its own
    last handler as a 20th entry. A JMP (ind) with no load of its pointer before it, reached by
    JSR, is a shared stub: each JSR site within `caller_back` bytes after the full pair
    (LDA tbl,r / STA ptr / LDA tbl+1,r / STA ptr+1) is a table site (Mappy's $EA05 -> $EA46).
    -> (tables {site: (base, [handlers])}, every JMP (ind) and inline-dispatch site seen,
    routines, instruction starts {offset: opcode})."""
    starts, ptr_of = {}, {}
    inline = {m.canon(a) for a in inline}
    m.noreturn |= inline

    def fixpoint(seedset):
        routines, st = m.reach(seedset)
        for a in st:
            n = m.off(a)
            starts[n] = m.buf[n]
            if m.buf[n] == JMP_IND:
                ptr_of[n] = m.word(a + 1)
            elif m.buf[n] == JSR and m.canon(m.word(a + 1)) in inline:
                ptr_of[n] = None
        return routines

    def resolve(site_n):
        ptr, site = ptr_of.get(site_n), m.canon(site_n)
        loads = []
        for a in sorted((m.canon(x) for x in starts if 0 < site - m.canon(x) <= back), reverse=True):
            if m.byte(a) in (0xBD, 0xB9):                               # LDA abs,X / abs,Y
                sta_t = m.byte(a + 4) if m.byte(a + 3) == 0x85 else None  # STA zp after it
                loads.append((m.word(a + 1), sta_t))
        if ptr is not None:                                             # the low-byte load
            for base, sta_t in loads:
                if sta_t == (ptr & 0xFF):
                    return base
            if stub_callers(site_n):
                return None
        return loads[0][0] if loads else None

    def stub_callers(site_n):
        site = m.canon(site_n)
        return [c for c, op in starts.items() if op == JSR and m.canon(m.word(m.canon(c) + 1)) == site]

    def paired_load(call, ptr):
        """The nearest full pointer-pair load of `ptr` before the JSR at `call`."""
        for a in sorted((m.canon(x) for x in starts if 0 < call - m.canon(x) <= caller_back),
                        reverse=True):
            op = m.byte(a)
            if (op in (0xBD, 0xB9) and m.byte(a + 3) == 0x85 and m.byte(a + 4) == ptr & 0xFF
                    and m.byte(a + 5) == op and m.word(a + 6) == m.word(a + 1) + 1
                    and m.byte(a + 8) == 0x85 and m.byte(a + 9) == (ptr + 1) & 0xFF):
                return m.word(a + 1)
        return None

    def read(base):
        ents, a = [], base
        for _ in range(max_entries):
            if m.is_data[m.off(a)] and a != base:
                break
            here = {m.canon(a), m.canon(a + 1)}
            if any(m.code[m.off(x)] for x in here) or here & {m.canon(e) for e in ents}:
                break
            w = m.word(a)
            if m.in_code(w) and m.legal(w):
                ents.append(w)
                a += 2
            else:
                break
        return ents

    tables, known, known_calls = {}, set(), set()

    def take(site, base):
        ents = read(base)
        if not ents:
            return False
        tables[site] = (base, ents)
        m.mark_data(base, 2 * len(ents))
        before = len(seeds)
        seeds.update(e for e in ents if m.in_code(e))
        return len(seeds) > before

    while True:
        fixpoint(seeds)
        new = False
        for n, op in sorted(starts.items()):
            if n in ptr_of and n not in known:
                base = m.canon(n) + 3 if op == JSR else resolve(n)
                if base is not None or op == JSR or not stub_callers(n):
                    known.add(n)                                        # a stub stays open: callers grow
                    if base is not None:
                        new = take(m.canon(n), base) or new
                    continue
                for c in stub_callers(n):
                    if c not in known_calls:
                        known_calls.add(c)
                        cb = paired_load(m.canon(c), ptr_of[n])
                        if cb is not None:
                            new = take(m.canon(c), cb) or new
        if not new:
            break
    return tables, known, fixpoint(seeds), starts


def stored_pointer_installs(m: NativeMap, starts: dict, *, window: int = 16, look: int = 6):
    """LDA #imm then STA (zp|abs) pairs whose immediates, on adjacent targets within `window`
    bytes, form a code pointer (or pointer - 1, the RTS-trick convention), stored into a variable
    some decoded `JMP (ind)` jumps through. Without that consumer a pair is a data pointer or a
    16-bit counter: all four of Mappy's former "installs" were. `starts` maps window offsets to
    (opcode, length). -> [(install site, handler)]."""
    jumped = {m.word(m.win.base + n + 1) for n, (op, _) in starts.items() if op == JMP_IND}
    events = []
    for n in sorted(starts):
        op, ilen = starts[n]
        if op != 0xA9:
            continue
        s2 = n + ilen
        if m.off(s2) not in starts:
            continue
        op2, _ = starts[m.off(s2)]
        if op2 == 0x85:
            tgt = m.byte(s2 + 1)
        elif op2 == 0x8D:
            tgt = m.word(s2 + 1)
        else:
            continue
        events.append((m.canon(n), tgt, m.byte(n + 1)))
    installs = []
    ev = sorted(events)
    for j, (a, t, imm) in enumerate(ev):
        for k in range(j + 1, min(j + look, len(ev))):
            a2, t2, imm2 = ev[k]
            if a2 - a > window:
                break
            if t2 == t + 1:
                lo, hi, var = imm, imm2, t
            elif t2 == t - 1:
                lo, hi, var = imm2, imm, t2
            else:
                continue
            if var not in jumped:
                break
            p = lo | (hi << 8)
            for cand in (p, (p + 1) & 0xFFFF):
                if m.in_code(cand) and m.legal(cand):
                    installs.append((a, cand))
                    break
            break
    return installs


def pointer_runs(m: NativeMap, *, min_len: int = 3):
    """Runs of >= min_len words in bytes that are neither code nor data, each pointing at decoded
    code; marked as data. -> [(base, [targets])]."""
    found, i, n = [], 0, len(m.buf)
    while i < n - 2:
        if not m.code[i] and not m.is_data[i]:
            run, a = [], i
            while a + 1 < n:
                w = m.buf[a] | (m.buf[a + 1] << 8)
                if m.in_code(w) and m.legal(w) and m.code[m.off(w)]:
                    run.append(w)
                    a += 2
                else:
                    break
            if len(run) >= min_len:
                found.append((m.win.base + i, run))
                m.mark_data(m.win.base + i, 2 * len(run))
                i = a
                continue
        i += 1
    return found


def owner(entries: list, a: int, m: NativeMap) -> int:
    """The routine an address belongs to: the greatest entry at or below it."""
    return entries[bisect.bisect_right(entries, m.canon(a)) - 1]


def partition_edges(m: NativeMap, entries: list, *, skip_data: bool = False) -> dict:
    """JSR / JMP abs edges between entries, each instruction owned by the greatest entry at or
    below it. skip_data steps over bytes proven data."""
    ent, edges = set(entries), {e: set() for e in entries}
    for idx, e in enumerate(entries):
        end = entries[idx + 1] if idx + 1 < len(entries) else 0x10000
        pc = e
        while pc < end:
            if skip_data and m.is_data[m.off(pc)]:
                pc += 1
                continue
            op = m.byte(pc)
            if op not in OPS:
                pc += 1
                continue
            if op in (JSR, JMP):
                t = m.word(pc + 1)
                if t in ent and t != e:
                    edges[e].add(t)
            pc += 1 + MODE_LEN.get(OPS[op][1], 0)
    return edges


def reachable(edges: dict, roots) -> set:
    seen = set(roots)
    dq = deque(seen)
    while dq:
        for w in edges[dq.popleft()]:
            if w not in seen:
                seen.add(w)
                dq.append(w)
    return seen


READ = {"lda", "ldx", "ldy", "cmp", "cpx", "cpy", "adc", "sbc", "and", "ora", "eor", "bit"}
WRITE = {"sta", "stx", "sty"}
RMW = {"inc", "dec", "asl", "lsr", "rol", "ror"}


def access_kind(mn: str) -> str:
    return "w" if mn in WRITE else ("m" if mn in RMW else "r")


def field_census(m: NativeMap, entries: list, *, pointers: set, table: tuple, size: int = 16,
                 keep: int = 4) -> dict:
    """A record's fields from every routine's accesses: (ptr),Y for ptr in `pointers` with a
    constant Y below `size`, and absolute / indexed accesses inside `table` = (lo, hi). ->
    {offset: {routine: {"kinds": {r,w,m}, "lines": [first `keep` instructions]}}}."""
    fields = {i: {} for i in range(size)}

    def rec(off, e, k, line):
        d = fields[off].setdefault(e, {"kinds": set(), "lines": []})
        d["kinds"].add(k)
        if len(d["lines"]) < keep:
            d["lines"].append(line)

    lo_t, hi_t = table
    for idx, e in enumerate(entries):
        end = m.canon(entries[idx + 1]) if idx + 1 < len(entries) else 0x10000
        pc, cur_y = m.canon(e), None
        while pc < end:
            if m.is_data[m.off(pc)]:
                pc += 1
                continue
            op = m.byte(pc)
            if op not in OPS:
                pc += 1
                cur_y = None
                continue
            mn, mode = OPS[op]
            il = 1 + MODE_LEN.get(mode, 0)
            lo = m.byte(pc + 1) if il > 1 else 0
            hi = m.byte(pc + 2) if il > 2 else 0
            line = f"${m.canon(pc):04X}: {mn} {fmt_operand(mode, lo, hi, m.canon(pc), il)}".strip()
            if mn == "ldy" and mode == IMM:
                cur_y = lo
            elif mn in ("tay", "iny", "dey") or (mn == "ldy" and mode != IMM):
                cur_y = None
            if mode == IZY and lo in pointers and cur_y is not None and cur_y < size:
                rec(cur_y, e, access_kind(mn), line)
            if mode in (ABS, ABX, ABY):
                a = (hi << 8) | lo
                if lo_t <= a <= hi_t:
                    rec((a - lo_t) % size, e, access_kind(mn), line)
            pc += il
    return fields


def routine_listing(m: NativeMap, entries: list, e: int, *, hwname=lambda a: None):
    """Decode [entry, next entry) (the whole span: routines have early returns), data bytes as
    .db. -> ([(addr, text)], signature {hw, zp, ram, loop, ninstr, nbytes}). `hwname(addr)` names
    a hardware register or returns None; RAM is $0000-$07FF."""
    i = entries.index(e)
    end = entries[i + 1] if i + 1 < len(entries) else 0x10000
    out, pc = [], m.canon(e)
    hw, zp, ram, loop, ninstr = set(), set(), set(), False, 0
    while pc < min(end, 0x10000):
        n = m.off(pc)
        if m.is_data[n]:
            out.append((m.canon(pc), f".db ${m.buf[n]:02X}"))
            pc += 1
            continue
        op = m.buf[n]
        if op not in OPS:
            out.append((m.canon(pc), f".db ${op:02X}"))
            pc += 1
            continue
        mn, mode = OPS[op]
        il = 1 + MODE_LEN.get(mode, 0)
        lo = m.byte(pc + 1) if il > 1 else 0
        hi = m.byte(pc + 2) if il > 2 else 0
        out.append((m.canon(pc), f"{mn} {fmt_operand(mode, lo, hi, m.canon(pc), il)}".strip()))
        ninstr += 1
        if mode in (ABS, ABX, ABY):
            a = (hi << 8) | lo
            if hwname(a):
                hw.add(hwname(a))
            elif a < 0x800:
                ram.add(a)
        if mode in (ZP, ZPX, ZPY):
            zp.add(lo)
        if op in BRANCHES and lo >= 0x80:
            loop = True
        pc += il
    return out, dict(hw=sorted(hw), zp=sorted(f"${z:02X}" for z in sorted(zp)),
                     ram=sorted(f"${r:04X}" for r in sorted(ram)),
                     loop=loop, ninstr=ninstr, nbytes=m.canon(pc) - m.canon(e))
