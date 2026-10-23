#!/usr/bin/env python3
"""
Lift KOEI SNES bytecode routines to C-like pseudocode -- the C layer of the layered source.

The VM is an accumulator/stack hybrid: regA / regC (+ the 32-bit L / R whose low words they
are), an operand stack (call arguments, temporaries) and frame slots. Each routine is lifted
over its control-flow graph (the vm_walk decode: routine_sig trampoline -> body, module-bounded):

  1. basic blocks from the walked instructions;
  2. liveness of regA / regC per instruction, and the operand-stack shape per block, both from
     each op's effects (the shared table's `c` template + control-flow class);
  3. two forward dataflows over the same CFG: the CONSTANTS on the operand stack (which syscall
     id reaches a call -- every path's, see stack_consts) and the WIDTH of regA / regC (whether
     the value was last written as 32 bits, from the ROM-derived register bytes each handler
     writes -- see width_flow);
  4. symbolic execution in reverse post-order. A block with one predecessor continues its
     predecessor's symbolic state; a JOIN (two or more predecessors, a loop head, the entry)
     starts from named variables -- `ret` for regA, `regC`, `s0..sN` for operand-stack
     entries -- and every edge into it assigns them (only what is live there). So a value
     never leaks along address order into a label reached by a jump;
  5. a symbolic value that a later store / call would change is copied to a temporary
     (tN = ...) first; a word store of regA to the variable its value names rebinds regA to
     that variable (x = x + 2; *(x - 2)... reads the OLD x correctly);
  6. a call is a statement: `ret = f(arg1, arg2, ...)` when its result (regA) is live on SOME
     path afterwards (liveness over the CFG, switch targets included), else `f(...)`.
     Arguments are in C order: arg1 = the LAST-pushed word (the callee reads it at FP+$0F),
     so the list is built in pop order. A call to a native stub that JSLs to the profile's
     abi_syscall (read from the ROM) is a syscall: its last-pushed argument is the id, and it
     renders as the named syscall (data/syscall_names.toml) with the remaining arguments when
     every path pushes the same id; else `syscall_bridge[id](...)`.

Everything per opcode comes from the ONE shared table (vm_isa): the control-flow class drives
branches / calls / switches / return; each op's `c` template ("A = (A + C)", "{o} = A",
"C = pop; mem[C] = A", "push {o}", ...) drives the rest -- no opcode tables here. Operands:
f8/f16 -> frame variables (local0 = FP-1 .., arg1 = FP+$0F ..); a16 -> the global's symbol
(else g_XXXX); an immediate that names something -> the routine / string name, &global for a
global's address. Widths and signedness: memb[] / mem32[] accesses; (u16) / (u32) / (s32) casts
on an operation's operands give its signedness (16-bit ops are signed unless cast); (u32)(u16)x
/ (s32)(s16)x are the zero / sign extensions (zext32 / sign32); (word)(...) marks a 16-bit
consumer (a 16-bit op, a 2-byte push, a 16-bit test) reading only the low word of a 32-bit value.
String previews (listing.preview: strings.toml names, else a string that starts at that address
in the module's own data) are hoisted to one trailing comment per statement:
`f(msg_BE0A, x);  /* msg_BE0A = "%3d0K" */`, so argument lists never contain comment text.

    py -3 tools/vm_lift.py <rom> [--game g] --module COMMON --syms <title>/data/COMMON   # every routine
    py -3 tools/vm_lift.py <rom> [--game g] --module COMMON --syms ... --at 1169         # one routine
    (legacy --start/--base select the module too; --scan is checked against the archive length)
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import strip_copier_header  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402
from emu_harness.vm.koei.snes import vm_isa as ISA  # noqa: E402
from emu_harness.vm.koei.snes import vm_walk as VW  # noqa: E402
from emu_harness.vm.koei.snes import listing  # noqa: E402
from emu_harness.vm.koei.snes.symbols import Symbols  # noqa: E402

RA, RC = ISA.RA, ISA.RC                         # liveness bits: regA, regC
STR_IN_C = 32                                   # string-preview length in the C
MAX_IDS = 8                                     # a set of more constants than this is "computed"
LOW = {RA: frozenset((0x0A, 0x0B)), RC: frozenset((0x0E, 0x0F))}      # regA / regC bytes
HIGH = {RA: frozenset((0x0C, 0x0D)), RC: frozenset((0x10, 0x11))}     # L-high / R-high bytes


# ---- symbolic values -----------------------------------------------------------------
class Val:
    """A C expression and what its value depends on: ('F', byte) frame bytes, ('G', addr)
    absolute (global) bytes, ('M',) memory read through a computed pointer, ('V', name) a
    lifter variable (ret / regC / sN / tN). `const` = the integer when it is an immediate."""
    __slots__ = ("text", "deps", "const")

    def __init__(self, text, deps=(), const=None):
        self.text, self.deps, self.const = text, frozenset(deps), const

    def __repr__(self):
        return "Val(%r)" % self.text


def var(name):
    return Val(name, [("V", name)])


MEM = ("M",)


def fbytes(off, w):
    return [("F", off + k) for k in range(w)]


def gbytes(a, w):
    return [("G", (a + k) & 0xFFFF) for k in range(w)]


def paren(t):
    return t if re.fullmatch(r"[\w$&.\[\]*-]+|\(.*\)", t) else "(%s)" % t


_CAST_DUP = re.compile(r"\((u16|s16|u32|s32)\)\(\1\)")


def tidy_casts(t):
    """(u32)(u32)x -> (u32)x: a cast repeated to the same type is the same conversion."""
    while True:
        n = _CAST_DUP.sub(r"(\1)", t)
        if n == t:
            return t
        t = n


def word(t):
    """A 16-bit consumer of a 32-bit value: it reads the low word only."""
    return "(word)" + paren(t)


# ---- the templates of the shared table (grammar + register effects live in vm_isa) ------
_TOK = ISA.TEMPLATE_TOKEN
_DIRECT_LV, _INDIRECT_LV = ISA.TEMPLATE_DIRECT_LV, ISA.TEMPLATE_INDIRECT_LV
_WIDTH = {"memb": 1, "mem": 2, "mem32": 4}
parse_template, step_exprs, template_effects = ISA.parse_template, ISA.step_exprs, ISA.template_effects
_reads = ISA.template_reads


class OpModel:
    """Per-op effects for the lifter, from the shared table (cached per Op)."""
    _cache = {}

    @classmethod
    def of(cls, info):
        m = cls._cache.get(id(info))
        if m is None:
            m = cls._cache[id(info)] = cls(info)
        return m

    def __init__(self, info):
        self.info = info
        self.steps = parse_template(info.c) if info.cf in ("none",) and info.c else None
        if self.steps is not None:
            self.use, self.dfn = template_effects(self.steps)
            self.later = []                            # per step: regA/regC read by the steps after it
            for i in range(len(self.steps)):
                rest = 0
                for st in self.steps[i + 1:]:
                    for e in step_exprs(st):
                        ra, rc, _np = _reads(e)
                        rest |= (RA if ra else 0) | (RC if rc else 0)
                    if st[0] == "swap":
                        rest |= RA | RC
                self.later.append(rest)
        elif info.cf == "none":                        # an op without a template: opaque, conservative
            self.use, self.dfn = RA | RC, 0
        elif info.cf in ("cjmp", "cbra"):
            self.use, self.dfn = RA, 0
        elif info.cf == "switch":
            biased = any(f.role == "bias" for f in info.fields)
            self.use, self.dfn = RA, (RA if biased else 0)
        elif info.cf == "ret":
            self.use, self.dfn = RA, 0
        elif info.cf == "call":
            ind = not any(f.role == "target" for f in info.fields)
            self.use, self.dfn = (RA if ind else 0), RA | RC
        else:                                          # jmp / bra
            self.use, self.dfn = 0, 0


def ins_effects(ins):
    """(use, def) of regA/regC for one decoded instruction (errors: reads everything)."""
    if ins.error:
        return RA | RC, 0
    m = OpModel.of(ins.info)
    return m.use, m.dfn


def live_before(ins, use_def, out):
    """Registers live before `ins` given those live after it. A pure swap (swapAC / swap32)
    MOVES: regC is live before it iff regA is live after, and vice versa."""
    if not ins.error and OpModel.of(ins.info).steps == [("swap",)]:
        return (RC if out & RA else 0) | (RA if out & RC else 0)
    use, dfn = use_def
    return use | (out & ~dfn)


def stack_delta(ins):
    """[('pop', bytes) | ('push', bytes)] in execution order, for the stack-shape pass."""
    if ins.error:
        return []
    info = ins.info
    if info.cf == "call":
        return [("pop", ins.pop)] if ins.pop else []
    m = OpModel.of(info)
    out = []
    for st in (m.steps or []):
        k = st[0]
        for e in step_exprs(st):
            for t in _TOK.findall(e):
                if t in ("pop", "pop32"):
                    out.append(("pop", 4 if t == "pop32" else 2))
        if k == "push":
            out.append(("push", st[1]))
        elif k == "sp_adj":
            n = sp_adj_bytes(ins)
            out.append(("pop", n) if n > 0 else ("push", -n))
    return out


def sp_adj_bytes(ins):
    """SP += operand: the signed byte count (the i16 form wraps like a signed 16-bit add)."""
    _r, t, n = ins.fields[0]
    if ISA.TYPE_SIZE[t] == 2 and n >= 0x8000:
        n -= 0x10000
    return n


# ---- the routine CFG -------------------------------------------------------------------
def succ_addrs(ins):
    if ins.error:
        return []
    cf = ins.cf
    if cf in ("jmp", "bra"):
        return [ins.targets[0]]
    if cf in ("cjmp", "cbra"):
        return [ins.next, ins.targets[0]]
    if cf == "switch":
        out = []
        for t in ins.targets:
            if t not in out:
                out.append(t)
        return out
    if cf == "ret":
        return []
    return [ins.next]


def cfg(insns):
    """{addr: successor addresses that were decoded} for one routine walk."""
    return {v: [t for t in succ_addrs(i) if t in insns] for v, i in insns.items()}


def forward(insns, succ, body, start, step, join):
    """Generic forward fixpoint: {addr: state at entry} (start at the body; join merges)."""
    IN = {body: start}
    work = [body]
    while work:
        v = work.pop()
        out = step(insns[v], IN[v])
        for t in succ[v]:
            old = IN.get(t)
            new = out if old is None else join(old, out)
            if old is None or new != old:
                IN[t] = new
                work.append(t)
    return IN


# ---- dataflow 1: constants on the operand stack (the syscall id a call receives) ------------
# A value is a frozenset of the constants it can be (one per path), or None = computed / unknown.
# State = (regA, regC, stack, deep): stack = ((bytes, value), ...) bottom..top; deep = entries
# below it are unknown (paths of different stack depth met: aligned from the TOP, the end that
# matters for the id).
def _vjoin(a, b):
    if a is None or b is None:
        return None
    u = a | b
    return u if len(u) <= MAX_IDS else None


def _cjoin(x, y):
    xs, ys = x[2], y[2]
    n = min(len(xs), len(ys))
    deep = x[3] or y[3] or len(xs) != len(ys)
    xs, ys = xs[len(xs) - n:], ys[len(ys) - n:]
    st = tuple((a[0], _vjoin(a[1], b[1])) if a[0] == b[0] else (max(a[0], b[0]), None) for a, b in zip(xs, ys))
    return (_vjoin(x[0], y[0]), _vjoin(x[1], y[1]), st, deep)


def _const_of(expr, ins, A, C, popped):
    if expr == "{o}" and ins.fields:
        _r, t, v = ins.fields[0]
        if t in ("s8", "s16", "i16", "u16"):
            return frozenset([v & 0xFFFF])
        if t == "i32":
            return frozenset([v & 0xFFFFFFFF])
        return None
    if expr == "{n}":
        return frozenset([ins.op & 0x0F])
    if expr == "A":
        return A
    if expr == "C":
        return C
    if expr in ("pop", "pop32"):
        return popped[0] if popped else None
    return None


def _cstep(ins, s):
    A, C, st, deep = s[0], s[1], list(s[2]), s[3]

    def pop_bytes(n):
        while n > 0 and st:
            n -= st.pop()[0]

    if ins.error:
        return (None, None, tuple(st), deep)
    cf = ins.cf
    if cf == "call":
        pop_bytes(ins.pop)
        return (None, None, tuple(st), deep)
    if cf == "switch":
        return (None if ins.value("bias") is not None else A, C, tuple(st), deep)
    if cf != "none":
        return s
    m = OpModel.of(ins.info)
    if m.steps is None:
        return (None, None, tuple(st), deep)
    for step in m.steps:
        k = step[0]
        if k == "swap":
            A, C = C, A
            continue
        if k == "nop":
            continue
        if k == "sp_adj":
            n = sp_adj_bytes(ins)
            if n > 0:
                pop_bytes(n)
            elif n < 0:
                st.append((-n, None))
            continue
        popped = []
        for e in step_exprs(step):
            for t in _TOK.findall(e):
                if t in ("pop", "pop32"):
                    popped.append(st.pop()[1] if st else None)
        if k == "push":
            v = _const_of(step[2], ins, A, C, popped)
            if v is not None and step[1] == 2:
                v = frozenset(x & 0xFFFF for x in v)
            st.append((step[1], v))
        elif k == "setA":
            A = _const_of(step[1], ins, A, C, popped)
        elif k == "setC":
            C = _const_of(step[1], ins, A, C, popped)
    return (A, C, tuple(st), deep)


def stack_consts(insns, succ, body):
    """{addr: (regA, regC, stack, deep) at entry}: which constants each value can be, over EVERY
    path of the routine CFG (a forward fixpoint; joins take the union)."""
    return forward(insns, succ, body, (None, None, (), False), _cstep, _cjoin)


def syscall_ids(space, walk, succ=None):
    """{call address: frozenset of ids, or None (computed)} for each call of one routine walk to a
    syscall stub: the operand-stack entry on top at the call (= the last-pushed word, arg1)."""
    insns = walk.insns
    if walk.body not in insns:
        return {}
    succ = succ if succ is not None else cfg(insns)
    calls = [v for v, i in insns.items() if not i.error and i.cf == "call" and i.call_target is not None
             and space.is_syscall_stub(i.call_target)]
    if not calls:
        return {}
    IN = stack_consts(insns, succ, walk.body)
    out = {}
    for v in calls:
        s = IN.get(v)
        out[v] = s[2][-1][1] if (s is not None and s[2]) else None
    return out


def module_syscall_ids(space, W):
    """syscall_ids over every routine of a module walk (a call two routines share: the union)."""
    out = {}
    for e in W.routines:
        for a, ids in syscall_ids(space, W.walks[e]).items():
            out[a] = ids if a not in out else _vjoin(out[a], ids)
    return out


# ---- dataflow 2: value widths (which regA / regC values are 32-bit) -------------------------
def _wstep(isa, ins, s):
    wa, wc = s
    if ins.error or ins.cf == "call":
        return (False, False)                      # a call's result: width unknown (not marked)
    if ins.cf == "switch":
        return ((False if ins.value("bias") is not None else wa), wc)
    if ins.cf != "none":
        return s
    m = OpModel.of(ins.info)
    if m.steps is None:
        return (False, False)
    if m.steps == [("swap",)]:
        return (wc, wa)
    _rd, wr = isa.regs(ins)
    ext = ins.ext is not None                      # primary ops are 16-bit: a primary handler's
    if m.dfn & RA:                                 # $0C-$0D / $10-$11 writes are scratch (sdiv,
        wa = ext and bool(wr & HIGH[RA])           # smod, the bitfield ops), not a result's high word
    if m.dfn & RC:
        wc = ext and bool(wr & HIGH[RC])
    return (wa, wc)


def width_flow(isa, insns, succ, body):
    """{addr: (wideA, wideC) at entry}: True when the value in regA / regC was last written, on
    some path into this instruction, by an EXTENDED (32-bit) op whose handler also wrote the
    register's high word (L $0C-$0D / R $10-$11) -- a 32-bit value. The 32-bit compares / test32
    write regA only (a 16-bit 0/1); the primary ops are 16-bit. From the ROM's handlers
    (BoundIsa.regs)."""
    return forward(insns, succ, body, (False, False), lambda i, s: _wstep(isa, i, s),
                   lambda a, b: (a[0] or b[0], a[1] or b[1]))


def reads_low(isa, ins, bit):
    """The instruction's handler reads regA (bit RA) / regC (RC) as 16 bits: the low word only."""
    rd, _wr = isa.regs(ins)
    return bool(rd & LOW[bit]) and not (rd & HIGH[bit])


# ---- C rendering helpers ---------------------------------------------------------------------
_PREVIEW = re.compile(r' /\*("(?:[^"\\]|\\.)*")\*/')


def hoist_previews(line):
    """Move the inline string previews of a statement (`name /*"text"*/`) to ONE trailing comment
    (`stmt;  /* name = "text" */`), so expressions and argument lists carry no comment text."""
    notes = []

    def take(m):
        k = j = m.start()
        while k > 0 and (line[k - 1].isalnum() or line[k - 1] in "_$&"):
            k -= 1
        note = ("%s = %s" % (line[k:j], m.group(1))) if k < j else m.group(1)
        if note not in notes:
            notes.append(note)
        return ""
    body = _PREVIEW.sub(take, line)
    return body if not notes else "%s  /* %s */" % (body, "; ".join(notes))


def c_quoted(s):
    """A preview as a C comment payload: quoted, and never closing the comment early."""
    return listing.quoted(s).replace("*/", "*\\/")


class Block:
    __slots__ = ("start", "addrs", "succs", "preds", "stmts", "term", "cond", "taken", "fall",
                 "switch", "retval", "canon")

    def __init__(self, start):
        self.start, self.addrs, self.succs, self.preds = start, [], [], []
        self.stmts, self.term = [], "fall"
        self.cond = self.taken = self.fall = self.switch = self.retval = None
        self.canon = True


class RoutineLift:
    """One routine lifted: .blocks (address order) with statements + terminator, .notes."""

    def __init__(self, entry, body):
        self.entry, self.body = entry, body
        self.blocks, self.notes, self.temps = [], [], 0
        self.args = 0
        self.ids, self.wide = {}, {}                  # syscall ids per call; (wideA, wideC) per insn


class Lifter:
    """Lifts routines of one module (shared state: ISA, symbols, the module's address space).
    W = the module walk (vm_walk.walk_module); walked lazily when not given -- its decoded bytes
    are excluded from string previews."""

    def __init__(self, isa, space, S, W=None):
        self.isa, self.space, self.S = isa, space, S
        self._W, self._code = W, None

    @property
    def code(self):
        if self._code is None:
            if self._W is None:
                self._W = VW.walk_module(self.isa, self.space)
            self._code = VW.covered(self._W, self.space)
        return self._code

    # -- operands -------------------------------------------------------------------------
    def const_text(self, v, typ):
        """An immediate: routine / string / table name, &global, or hex; + a string preview."""
        if typ in ("s8", "s16"):
            base = ISA.hexs(v)
            u = v & 0xFFFF
        elif typ == "i32":
            return "$%08X" % v                         # 8 digits: a 32-bit constant
        else:
            base, u = "$%X" % v, v
        nm = self.S.string(u) if typ in ("i16",) else None
        if nm:
            base = ("&" + nm) if self.S.kind(u) == "global" else nm
        if typ == "i16":
            st = listing.preview(self.S, u, self.space.read, self.space.read_any, self.code)
            if st:
                if len(st) > STR_IN_C:
                    st = st[:STR_IN_C] + "..."
                base = "%s /*%s*/" % (base, c_quoted(st))
        return base

    def operand(self, ins):
        """(kind, text, key) of the explicit operand: 'F' frame variable (key = FP offset),
        'G' global (key = address), 'I' immediate (key = value)."""
        _r, t, v = ins.fields[0]
        if t in ISA.FRAME_TYPES:
            off = ISA.frame_offset(v)
            return "F", ISA.frame_var(off), off
        if t == "a16":
            return "G", (self.S.string(v) or "g_%04X" % v), v
        return "I", self.const_text(v, t), v

    # -- lifting one routine -----------------------------------------------------------------
    def lift(self, entry, walk=None):
        sp = self.space
        walk = walk or VW.walk_routine(self.isa, sp, entry)
        R = RoutineLift(entry, walk.body)
        insns = walk.insns
        for kind, v in walk.errors:
            R.notes.append("decode error at $%04X: %s" % (v, kind))
        if walk.body not in insns:
            R.notes.append("no body decoded")
            return R
        succ = cfg(insns)
        for v, i in insns.items():
            for t in succ_addrs(i):
                if t not in insns:
                    R.notes.append("$%04X: successor $%04X was not decoded" % (v, t))
        R.ids = syscall_ids(sp, walk, succ)
        R.wide = width_flow(self.isa, insns, succ, walk.body)
        # leaders + blocks
        leaders = {walk.body}
        for v, i in insns.items():
            if succ_addrs(i) != [i.next]:
                leaders.update(succ[v])
                if i.next in insns:
                    leaders.add(i.next)
        blocks = {}
        for L in sorted(leaders):
            B = blocks[L] = Block(L)
            v = L
            while True:
                B.addrs.append(v)
                i = insns[v]
                if succ_addrs(i) != [i.next] or i.next not in insns or i.next in leaders:
                    break
                v = i.next
            B.succs = list(succ[B.addrs[-1]])
        for L, B in blocks.items():
            for s in B.succs:
                if L not in blocks[s].preds:
                    blocks[s].preds.append(L)
        # liveness of regA / regC, per instruction (backward fixpoint)
        live_in, live_out = {v: 0 for v in insns}, {v: 0 for v in insns}
        eff = {v: ins_effects(i) for v, i in insns.items()}
        order = sorted(insns, reverse=True)
        changed = True
        while changed:
            changed = False
            for v in order:
                out = 0
                for s in succ[v]:
                    out |= live_in[s]
                inn = live_before(insns[v], eff[v], out)
                if out != live_out[v] or inn != live_in[v]:
                    live_out[v], live_in[v], changed = out, inn, True
        # reverse post-order over blocks
        rpo, seen = [], set()

        def dfs(L):
            stack = [(L, iter(blocks[L].succs))]
            seen.add(L)
            while stack:
                n, it = stack[-1]
                for s in it:
                    if s not in seen:
                        seen.add(s)
                        stack.append((s, iter(blocks[s].succs)))
                        break
                else:
                    rpo.append(n)
                    stack.pop()
        dfs(walk.body)
        rpo.reverse()
        ri = {L: k for k, L in enumerate(rpo)}
        for L, B in blocks.items():
            B.canon = (L == walk.body or len(B.preds) != 1 or B.preds[0] == L
                       or ri.get(B.preds[0], 1 << 30) >= ri.get(L, 0))
        # operand-stack shape at block entry (forward)
        shape = {walk.body: ()}
        work = [walk.body]
        while work:
            L = work.pop(0)
            sh = list(shape[L])
            for v in blocks[L].addrs:
                for kind, n in stack_delta(insns[v]):
                    if kind == "push":
                        sh.append(n)
                        continue
                    while n > 0 and sh:
                        n -= sh.pop()
                    if n != 0:
                        R.notes.append("$%04X: pops %s bytes %s" % (v, "more" if n > 0 else "fewer",
                                                                 "than the stack holds" if n > 0 else "than an entry"))
            for s in blocks[L].succs:
                if s not in shape:
                    shape[s] = tuple(sh)
                    work.append(s)
                elif shape[s] != tuple(sh):
                    R.notes.append("stack shape mismatch at $%04X: %s vs %s (from $%04X)" % (s, shape[s], tuple(sh), L))
        has_lea = any(not i.error and i.info.c and "&{o}" in i.info.c and "[&{o}]" not in i.info.c
                      for i in insns.values())
        # symbolic execution in RPO
        exit_state = {}
        for L in rpo:
            B = blocks[L]
            if B.canon:
                st = State(var("ret"), var("regC"), [[var("s%d" % k), n] for k, n in enumerate(shape.get(L, ()))])
            else:
                st = exit_state[B.preds[0]].copy()
            M = Machine(self, R, st, has_lea, B.stmts)
            last = None
            for v in B.addrs:
                last = insns[v]
                M.need = live_out[v]                      # registers still read afterwards: kept valid
                if last.error:
                    B.stmts.append("/* decode error at $%04X: %s */" % (v, self.isa.text(last)))
                    B.term = "error"
                    break
                cf = last.cf
                if cf == "call":
                    M.call(last, live_out[v] & RA)
                    if live_out[v] & RC:
                        R.notes.append("$%04X: regC is read after this call (the callee clobbers it)" % v)
                elif cf == "none":
                    M.op(last, live_out[v])
            if B.term != "error":
                self.terminate(B, M, last, blocks, live_in, insns, R)
            exit_state[L] = M.st
        R.blocks = [blocks[L] for L in sorted(blocks)]
        text = " ".join(" ".join(B.stmts) + " " + (B.cond or "") + " " + (B.retval or "") + " " + (B.switch or "")
                        for B in R.blocks)
        R.args = max([int(x) for x in re.findall(r"\barg(\d+)\b", text)] or [0])
        return R

    def reg_text(self, R, ins, bit, val):
        """The text of regA / regC as `ins` reads it: (word)(...) when the handler reads only the low
        word of a value that is 32-bit on some path into it."""
        wa, wc = R.wide.get(ins.addr, (False, False))
        if (wa if bit == RA else wc) and reads_low(self.isa, ins, bit):
            return word(val.text)
        return val.text

    def terminate(self, B, M, ins, blocks, live_in, insns, R):
        """Materialise the state for JOIN successors, then record the block's terminator."""
        canon_succ = [s for s in B.succs if blocks[s].canon]
        need = 0
        for s in canon_succ:
            need |= live_in[s]
        M.need = 0                                     # what any successor still reads (copies)
        for s in B.succs:
            M.need |= live_in[s]
        cf = ins.cf
        if cf == "switch" and ins.value("bias"):
            M.st.A = Val("(%s + %s)" % (self.reg_text(R, ins, RA, M.st.A), ISA.hexs(ins.value("bias"))), M.st.A.deps)
        if canon_succ:
            M.materialize(need)
        if cf in ("jmp", "bra"):
            B.term, B.taken = "goto", ins.targets[0]
        elif cf in ("cjmp", "cbra"):
            a = self.reg_text(R, ins, RA, M.st.A)
            B.term, B.taken, B.fall = "if", ins.targets[0], ins.next
            B.cond = a if ins.info.c == "A" else "!%s" % paren(a)
        elif cf == "switch":
            sel = M.st.A.text if ins.value("bias") else self.reg_text(R, ins, RA, M.st.A)
            cases = []
            if ins.value("bias") is not None:
                for k, t in enumerate(ins.values_of("case")):
                    cases.append("case %d: goto L%04X;" % (k, t))
            else:
                for val, t in zip(ins.values_of("value"), ins.values_of("target")):
                    cases.append("case $%X: goto L%04X;" % (val & 0xFFFF, t))
            B.term = "switch"
            B.switch = "switch (%s) { %s default: goto L%04X; }" % (sel, " ".join(cases), ins.switch_default())
        elif cf == "ret":
            B.term, B.retval = "return", M.st.A.text
        else:
            B.term = "fall"


class State:
    __slots__ = ("A", "C", "st")

    def __init__(self, A, C, st):
        self.A, self.C, self.st = A, C, st

    def copy(self):
        return State(self.A, self.C, [list(e) for e in self.st])


class Machine:
    """Symbolic regA / regC / operand stack over one block; appends statements to `out`."""

    def __init__(self, L, R, st, has_lea, out):
        self.L, self.R, self.st, self.has_lea, self.out = L, R, st, has_lea, out
        self.need = RA | RC          # regA / regC still read after (or by) the current instruction
        self.trunc = 0               # registers the current op reads as 16 bits of a 32-bit value

    # -- temporaries -----------------------------------------------------------------------
    def temp(self):
        self.R.temps += 1
        return "t%d" % self.R.temps

    def slots(self):
        yield "A", None
        yield "C", None
        for k in range(len(self.st.st)):
            yield "S", k

    def get(self, kind, k):
        return self.st.A if kind == "A" else (self.st.C if kind == "C" else self.st.st[k][0])

    def put(self, kind, k, val):
        if kind == "A":
            self.st.A = val
        elif kind == "C":
            self.st.C = val
        else:
            self.st.st[k][0] = val

    def flush(self, hit, skip=()):
        """Copy every state value whose deps satisfy hit(deps) to a temporary (one per value)."""
        done = {}
        for kind, k in list(self.slots()):
            if (kind, k) in skip or (kind == "A" and not self.need & RA) or (kind == "C" and not self.need & RC):
                continue                               # a dead register needs no copy
            v = self.get(kind, k)
            if not v.deps or not hit(v.deps):
                continue
            if id(v) not in done:
                t = self.temp()
                self.out.append("%s = %s;" % (t, v.text))
                done[id(v)] = var(t)
            self.put(kind, k, done[id(v)])

    def mem_hit(self, deps):
        """Would a store through an unknown pointer / a call change this value?"""
        for d in deps:
            if d[0] in ("M", "G") or (d[0] == "F" and self.has_lea):
                return True
        return False

    # -- expressions ----------------------------------------------------------------------
    def pop(self, nbytes):
        if not self.st.st:
            self.R.notes.append("operand stack underflow")
            return Val("/*stack underflow*/")
        v, n = self.st.st.pop()
        if n != nbytes:
            self.R.notes.append("pop of %d bytes takes a %d-byte entry" % (nbytes, n))
        return v

    def eval(self, ins, expr):
        """Evaluate a template expression against the state -> Val (pops happen here)."""
        deps, parts, pos = set(), [], 0
        kind = text = key = None
        if "{o}" in expr:
            kind, text, key = self.L.operand(ins)
        for m in _TOK.finditer(expr):
            lit = expr[pos:m.start()]
            parts.append(lit)
            t, pos = m.group(0), m.end()
            if t in ("A", "C"):
                bit = RA if t == "A" else RC
                v = self.st.A if t == "A" else self.st.C
                cast16 = lit.endswith("(u16)") or lit.endswith("(s16)")   # the cast already truncates
                parts.append(word(v.text) if (self.trunc & bit and not cast16) else v.text)
                deps |= v.deps
            elif t in ("pop", "pop32"):
                v = self.pop(4 if t == "pop32" else 2)
                parts.append(v.text)
                deps |= v.deps
            elif t == "{n}":
                parts.append("$%X" % (ins.op & 0x0F))
            elif t == "{slot}":
                off = ISA.slot_offset(ins.op & 0x0F)
                parts.append(ISA.frame_var(off))
                deps |= set(fbytes(off, 2))
            elif t == "&{o}":
                parts.append("&" + text)
            elif t in ("{o}", "memb[&{o}]", "mem32[&{o}]"):
                w = {"{o}": 2, "memb[&{o}]": 1, "mem32[&{o}]": 4}[t]
                if kind == "F":
                    deps |= set(fbytes(key, w))
                elif kind == "G":
                    deps |= set(gbytes(key, w))
                parts.append(text if t == "{o}" else t.replace("{o}", text))
            else:                                       # mem[ / memb[ / mem32[ through a pointer
                parts.append(t)
                deps.add(MEM)
        parts.append(expr[pos:])
        txt = tidy_casts("".join(parts))
        const = None
        if expr == "{o}" and kind == "I":
            const = ins.fields[0][2] & 0xFFFF
        elif expr == "{n}":
            const = ins.op & 0x0F
        return Val(txt, deps, const)

    # -- one non-control op -----------------------------------------------------------------
    def op(self, ins, live_after):
        m = OpModel.of(ins.info)
        if m.steps is None:                            # no template: shown, state made unknown
            self.need = RA | RC
            self.flush(self.mem_hit)
            self.out.append("/* %s */" % self.L.isa.render(ins)[0])
            self.st.A, self.st.C = Val("regA_after_%04X" % ins.addr), Val("regC_after_%04X" % ins.addr)
            return
        wa, wc = self.R.wide.get(ins.addr, (False, False))
        self.trunc = ((RA if wa and reads_low(self.L.isa, ins, RA) else 0) |
                      (RC if wc and reads_low(self.L.isa, ins, RC) else 0))
        for si, step in enumerate(m.steps):
            # a flush in this step keeps a register valid only if it is read after this
            # instruction or by a later step of its template (this step consumes its own reads)
            self.need = live_after | m.later[si]
            k = step[0]
            if k == "nop":
                continue
            if k == "swap":
                self.st.A, self.st.C = self.st.C, self.st.A
                self.trunc = 0
            elif k == "push":
                self.st.st.append([self.eval(ins, step[2]), step[1]])
            elif k == "sp_adj":
                n = sp_adj_bytes(ins)
                self.out.append("/* sp_adj #%s */" % ISA.hexs(n))
                while n > 0 and self.st.st:
                    n -= self.st.st.pop()[1]
                if n < 0:
                    self.st.st.append([Val("/*reserved*/"), -n])
            elif k == "setA":
                self.st.A = self.eval(ins, step[1])
                self.trunc &= ~RA                      # later steps read the value just written
            elif k == "setC":
                self.st.C = self.eval(ins, step[1])
                self.trunc &= ~RC
            elif k == "store":
                self.store(ins, step[1], step[2])
            elif k == "stmt":
                val = self.eval(ins, step[1])
                self.flush(self.mem_hit)
                self.out.append(val.text + ";")
        self.trunc = 0

    def store(self, ins, lv, ex):
        A = self.st.A
        if ex == "A":                                  # the value, before any pop in the lvalue
            val = Val(word(A.text), A.deps) if self.trunc & RA else A
        else:
            val = self.eval(ins, ex)
        direct = _DIRECT_LV.match(lv)
        if direct:
            kind, text, key = self.L.operand(ins) if "{o}" in lv else ("F", None, ISA.slot_offset(ins.op & 0x0F))
            w = 1 if lv.startswith("memb") else (4 if lv.startswith("mem32") else 2)
            if lv == "{slot}":
                text = ISA.frame_var(key)
            lhs = text if w == 2 else lv.replace("{o}", text)
            hitset = set(fbytes(key, w) if kind == "F" else gbytes(key, w))

            def hit(d, hs=hitset):
                return bool(d & hs) or (MEM in d and (kind == "G" or self.has_lea))
        else:
            mi = _INDIRECT_LV.match(lv)
            ptr = self.eval(ins, mi.group(2))
            lhs = "%s[%s]" % (mi.group(1), ptr.text)
            hit = self.mem_hit
            w = _WIDTH[mi.group(1)]
        # values the store changes are copied first; a word / 32-bit store of regA to the
        # variable its own value names rebinds regA to that variable instead
        rebind = (direct and w in (2, 4) and val is A and hit(A.deps)
                  and not any(self.get(k_, i_) is A for k_, i_ in self.slots() if k_ != "A"))
        self.flush(hit, skip=({("A", None)} if rebind else set()))
        if not rebind and ex == "A":
            A2 = self.st.A                             # regA may have just been copied to a temporary
            val = Val(word(A2.text), A2.deps) if self.trunc & RA else A2
        self.out.append("%s = %s;" % (lhs, val.text))
        if rebind:
            deps = (fbytes(key, w) if kind == "F" else gbytes(key, w))
            self.st.A = Val(lhs, deps)

    # -- calls ------------------------------------------------------------------------------
    def call(self, ins, result_live):
        L, S = self.L, self.L.S
        need, args = ins.pop, []
        while need > 0:
            if not self.st.st:
                self.R.notes.append("$%04X: call pops %d bytes more than the stack holds" % (ins.addr, need))
                args.append(Val("/*stack underflow*/"))
                break
            v, n = self.st.st.pop()
            args.append(v)                             # pop order = C order: arg1 was pushed last
            need -= n
        if need < 0:
            self.R.notes.append("$%04X: call pop splits a stack entry" % ins.addr)
        t = ins.call_target
        if t is None:
            fn = "(*%s)" % paren(L.reg_text(self.R, ins, RA, self.st.A))
        elif L.space.is_syscall_stub(t):
            idv = args.pop(0) if args else Val("?")
            ids = self.R.ids.get(ins.addr)             # every path's id (dataflow), not just this block's
            if idv.const is not None:
                ids = frozenset([idv.const])
            if ids is not None and len(ids) == 1:
                i = next(iter(ids))
                fn = S.syscall(i) or "syscall_%02X" % i
            else:
                fn = "%s[%s]" % (S.string(t) or "syscall", idv.text)
        else:
            fn = S.string(t) or "sub_%04X" % t
        # a call may change memory, any global and the frame (when its address was taken), and
        # it changes regA: a pending value naming `ret` is copied first, so after ANY call the
        # C never reads `ret` unless it is assigned again (the VM's reading of regA)
        self.flush(lambda d: self.mem_hit(d) or ("V", "ret") in d, skip={("A", None)})
        text = "%s(%s)" % (fn, ", ".join(a.text for a in args))
        self.out.append(("ret = %s;" % text) if result_live else (text + ";"))
        self.st.A, self.st.C = var("ret"), var("regC")

    # -- joins ---------------------------------------------------------------------------------
    def materialize(self, need):
        """Assign the join variables: ret (regA, when live), regC (when live), s0..sN."""
        pairs = []
        if need & RA and self.st.A.text != "ret":
            pairs.append(("A", None, "ret"))
        if need & RC and self.st.C.text != "regC":
            pairs.append(("C", None, "regC"))
        for k in range(len(self.st.st)):
            if self.st.st[k][0].text != "s%d" % k:
                pairs.append(("S", k, "s%d" % k))
        if not pairs:
            return
        dsts = {d for _k, _i, d in pairs}
        # a value that reads one of the variables being assigned (other than its own) goes
        # through a temporary first; so does any other state value reading them (a register
        # only when some successor still reads it)
        assigned = {(k, i) for k, i, _d in pairs}
        for kind, k in list(self.slots()):
            if (kind, k) not in assigned and ((kind == "A" and not self.need & RA) or
                                              (kind == "C" and not self.need & RC)):
                continue
            v = self.get(kind, k)
            own = next((d for k_, i_, d in pairs if (k_, i_) == (kind, k)), None)
            if any(("V", d) in v.deps for d in dsts if d != own) or ((kind, k) not in assigned and
                                                                    any(("V", d) in v.deps for d in dsts)):
                t = self.temp()
                self.out.append("%s = %s;" % (t, v.text))
                self.put(kind, k, var(t))
        for kind, k, d in pairs:
            v = self.get(kind, k)
            if v.text != d:
                self.out.append("%s = %s;" % (d, v.text))
            self.put(kind, k, var(d))


# ---- rendering -----------------------------------------------------------------------------
def render(R, name):
    """C text for one lifted routine."""
    out = []
    params = ", ".join("word arg%d" % k for k in range(1, R.args + 1))
    out.append("/* $%04X%s */" % (R.entry, (" (body $%04X)" % R.body) if R.body != R.entry else ""))
    for n in R.notes:
        out.append("/* NOTE: %s */" % n)
    out.append("word %s(%s) {" % (name, params))
    parts = []                                        # (block start, lines)
    blocks = R.blocks
    for k, B in enumerate(blocks):
        lines = list(B.stmts)
        nxt = blocks[k + 1].start if k + 1 < len(blocks) else None
        if B.term == "goto":
            if B.taken != nxt:
                lines.append("goto L%04X;" % B.taken)
        elif B.term == "if":
            lines.append("if (%s) goto L%04X;" % (B.cond, B.taken))
            if B.fall != nxt:
                lines.append("goto L%04X;" % B.fall)
        elif B.term == "switch":
            lines.append(B.switch)
        elif B.term == "return":
            lines.append("return %s;" % B.retval)
        elif B.term == "fall" and B.succs and B.succs[0] != nxt:
            lines.append("goto L%04X;" % B.succs[0])
        parts.append((B.start, lines))
    used = set()                                      # a label only where some goto / case lands
    for _s, lines in parts:
        for ln in lines:
            used.update(int(x, 16) for x in re.findall(r"goto L([0-9A-F]{4});", ln))
    body = []
    for start, lines in parts:
        if start in used:
            body.append("L%04X:" % start)
        body.extend(lines)
    # peephole: `ret = f(..); return ret;` -> `return f(..);` (adjacent, same block)
    peep, i = [], 0
    while i < len(body):
        cur = body[i]
        nx = body[i + 1] if i + 1 < len(body) else None
        m = re.match(r"^ret = (.+);$", cur)
        if m and nx == "return ret;":
            peep.append("return %s;" % m.group(1))
            i += 2
            continue
        peep.append(cur)
        i += 1
    for s in peep:
        out.append(("  " if s.endswith(":") and re.match(r"^L[0-9A-F]{4}:$", s) else "    ") + hoist_previews(s))
    out.append("}")
    return "\n".join(out)


def main(argv):
    ap = argparse.ArgumentParser(description="Lift KOEI SNES bytecode routines to C-like pseudocode.")
    ap.add_argument("rom")
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ROM header)")
    ap.add_argument("--module", default=None, help="archive name (instead of --start/--base)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--syms", required=True)
    ap.add_argument("--at", default=None, help="hex: lift this one routine (trampoline or body address)")
    ap.add_argument("--scan", default=None, help="legacy module end (checked against the archive length)")
    a = ap.parse_args(argv[1:])
    if not (a.module or a.start):
        ap.error("give --module NAME or --start/--base")
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    isa = ISA.bind(rom, P)
    mod = VW.module_from_args(rom, P, a)
    if a.scan and int(a.scan, 16) != mod.end:
        print("note: --scan %s ignored; module %s ends at $%04X (archive length)" % (a.scan, mod.name, mod.end),
              file=sys.stderr)
    space = VW.Space(rom, P, mod)
    S = Symbols(a.syms, profile=P)
    W = VW.walk_module(isa, space)
    L = Lifter(isa, space, S, W)
    if a.at:
        e = int(a.at, 16)
        R = L.lift(e, W.walks.get(e))
        print(render(R, S.string(e) or "sub_%04X" % e))
        return 0
    print("/* ==== %s: lifted C (derived by koei-snes vm_lift) -- %d routines, $%04X-$%04X ==== */"
          % (mod.name, len(W.routines), mod.base, mod.end - 1))
    for e in W.routines:
        R = L.lift(e, W.walks[e])
        print()
        print(render(R, S.string(e) or "sub_%04X" % e))
    return 0


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
