"""The KOEI bytecode instruction set: the ONE opcode table (vm_opcode_names.toml, next to this file) and its
machine-neutral model -- per opcode the listing name, control-flow class, operand layout and lift template.

The same engine runs on the NES (6502 host) and the SNES (65C816 host): MIGRATION.md row F2 measured every
live opcode on 9 NES cartridges and the table matched. Each host binds the table to a ROM its own way
(snes/vm_isa.bind derives sizes from the 65C816 handlers; the NES decoder takes its lengths from here).
Moved verbatim out of koei-snes tools/vm_isa.py (row F, SNES half step S3); vm_isa re-exports all of it.
"""
import os
import re
from collections import namedtuple

from emu_harness.vm.koei import minitoml

TABLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vm_opcode_names.toml")

# ---- control-flow classes ------------------------------------------------------------
CF_CLASSES = ("none", "jmp", "cjmp", "bra", "cbra", "switch", "call", "ret", "prefix")
FALLS_THROUGH = {"none", "cjmp", "cbra", "call", "prefix"}   # execution may continue at addr+len
LOCAL_BRANCH = {"jmp", "cjmp", "bra", "cbra", "switch"}      # transfers to in-module targets
CONDITIONAL = {"cjmp", "cbra"}

# ---- operand layout field types (byte counts are cross-checked against the ROM) -------
TYPE_SIZE = {"s8": 1, "f8": 1, "r8": 1, "e8": 1,
             "i16": 2, "s16": 2, "u16": 2, "f16": 2, "a16": 2, "i32": 4}
SIGNED = {"s8", "f8", "r8", "s16", "f16"}
FRAME_TYPES = {"f8", "f16"}

Field = namedtuple("Field", "role type repeat group")      # group: tuple of Fields (or None)


class IsaError(ValueError):
    """The shared table itself is malformed."""


def parse_layout(text):
    """'bias:s16 n:u16 default:a16 case:a16*n' -> tuple of Field. '(a b)*n' repeats a group."""
    toks = re.findall(r"\)\*\w+|[()]|[^\s()]+", text or "")

    def seq(i, closing):
        out = []
        while i < len(toks):
            t = toks[i]
            if t.startswith(")"):
                if not closing:
                    raise IsaError("layout %r: unbalanced ')'" % text)
                return tuple(out), i + 1, (t[2:] if t.startswith(")*") else None)
            if t == "(":
                sub, i, rep = seq(i + 1, True)
                out.append(Field(None, None, rep, sub))
                continue
            role, _, typ = t.rpartition(":")
            typ, _, rep = typ.partition("*")
            if typ not in TYPE_SIZE:
                raise IsaError("layout %r: unknown field type %r" % (text, typ))
            out.append(Field(role or typ, typ, rep or None, None))
            i += 1
        if closing:
            raise IsaError("layout %r: unclosed '('" % text)
        return tuple(out), i, None

    fields, _, _ = seq(0, False)
    return fields


def layout_size(fields):
    """Fixed operand byte count, or None when a field repeats (switch tables)."""
    n = 0
    for f in fields:
        if f.repeat:
            return None
        n += layout_size(f.group) if f.group else TYPE_SIZE[f.type]
    return n


# ---- lift templates (the `c` field): grammar + regA / regC effects --------------------
# Shared by vm_lift (rendering, liveness) and bind() (the ROM check of the register effects).
RA, RC = 1, 2                                     # register bits: regA (L), regC (R)
TEMPLATE_DIRECT_LV = re.compile(r"^(\{o\}|\{slot\}|memb\[&\{o\}\]|mem32\[&\{o\}\])$")   # a store to a variable
TEMPLATE_INDIRECT_LV = re.compile(r"^(mem|memb|mem32)\[(.+)\]$")                       # ... through a pointer
TEMPLATE_TOKEN = re.compile(r"mem32\[&\{o\}\]|memb\[&\{o\}\]|&\{o\}|\{o\}|\{n\}|\{slot\}|\bpop32\b|\bpop\b"
                            r"|\bA\b|\bC\b|\bmem32\[|\bmemb\[|\bmem\[")


def parse_template(c):
    """A `c` template -> steps: (kind, ...). Steps are separated by ';'. Kinds: nop, swap,
    push(size, expr), sp_adj, setA(expr), setC(expr), store(lvalue, expr), stmt(expr)."""
    steps = []
    for part in [p.strip() for p in (c or "").split(";")]:
        if not part:
            continue
        if part in ("nop", "swap", "sp_adj {o}"):
            steps.append((part.split()[0],))
        elif part.startswith("push32 ") or part.startswith("push "):
            steps.append(("push", 4 if part.startswith("push32") else 2, part.split(" ", 1)[1]))
        elif re.match(r"^[AC] = ", part):
            steps.append(("set" + part[0], part[4:]))
        elif " = " in part:
            lv, _, ex = part.partition(" = ")
            steps.append(("store", lv.strip(), ex.strip()))
        else:
            steps.append(("stmt", part))
    return steps


def step_exprs(st):
    """The expressions a template step evaluates (a store: its lvalue and its value)."""
    k = st[0]
    if k == "push":
        return [st[2]]
    if k in ("setA", "setC", "stmt"):
        return [st[1]]
    if k == "store":
        return [st[1], st[2]]
    return []


def template_reads(expr):
    """(reads regA, reads regC, operand-stack bytes popped) of one template expression."""
    toks = TEMPLATE_TOKEN.findall(expr)
    return ("A" in toks), ("C" in toks), sum(4 if t == "pop32" else 2 for t in toks if t in ("pop", "pop32"))


def template_effects(steps):
    """(use, def) bit masks of regA / regC for a parsed template: a register is USED when a
    step reads it before an earlier step of the same template wrote it."""
    use, dfn = 0, 0
    for st in steps:
        k = st[0]
        for e in step_exprs(st):
            ra, rc, _np = template_reads(e)
            use |= (RA if ra and not dfn & RA else 0) | (RC if rc and not dfn & RC else 0)
        if k == "swap":
            use |= (RA & ~dfn) | (RC & ~dfn)
            dfn |= RA | RC
        elif k == "setA":
            dfn |= RA
        elif k == "setC":
            dfn |= RC
    return use, dfn


class Op:
    __slots__ = ("code", "ext", "name", "mnem", "cf", "layout", "fields", "size", "desc", "c")

    def __init__(self, code, ext, name, cf, layout, desc, c=None):
        self.code, self.ext, self.name, self.cf, self.layout, self.desc = code, ext, name, cf, layout, desc
        self.c = c                                  # lift template (see the table header), or None
        self.mnem = name.split()[0]
        self.fields = parse_layout(layout)
        self.size = layout_size(self.fields)

    def field_types(self):
        return [f.type for f in self.fields if f.type]

    def __repr__(self):
        return "Op(%s$%02X %r cf=%s layout=%r)" % ("ext " if self.ext else "", self.code, self.name, self.cf, self.layout)


class Table:
    """The parsed shared opcode table (validated: full coverage, known classes/types, unique names)."""

    def __init__(self, path=TABLE_PATH):
        self.path = path
        d = minitoml.load_file(path)
        meta = d.get("meta", {})
        self.prefix = meta["prefix"]
        self.ext_count = meta["ext_count"]
        self.primary, self.extended = {}, {}
        for rng, e in d.get("primary_ranges", {}).items():
            lo, hi = (int(x, 16) for x in rng.split("-"))
            for op in range(lo, hi + 1):
                name = e["name"].replace("{n}", "%X" % (op & 0x0F))
                # the c template keeps {n}/{slot}: the lifter binds them (low nibble / its frame slot)
                self._add(self.primary, Op(op, False, name, e["cf"], e.get("layout", ""), e.get("desc", ""),
                                           e.get("c")))
        for k, e in d.get("primary", {}).items():
            self._add(self.primary, Op(int(k, 0), False, e["name"], e["cf"], e.get("layout", ""),
                                       e.get("desc", ""), e.get("c")))
        for k, e in d.get("extended", {}).items():
            self._add(self.extended, Op(int(k, 0), True, e["name"], e["cf"], e.get("layout", ""),
                                        e.get("desc", ""), e.get("c")))
        dead = d.get("dead", {})
        self.dead_primary = set(dead.get("primary", []))
        self.dead_ext = set(dead.get("extended", []))
        self._validate()

    def _add(self, tbl, op):
        if op.code in tbl:
            raise IsaError("%s: opcode $%02X defined twice" % (self.path, op.code))
        if op.cf not in CF_CLASSES:
            raise IsaError("%s: $%02X unknown cf %r" % (self.path, op.code, op.cf))
        tbl[op.code] = op

    def _validate(self):
        p = self.path
        for code in range(256):
            live, dead = code in self.primary, code in self.dead_primary
            if live == dead:
                raise IsaError("%s: primary $%02X must be exactly one of live/dead" % (p, code))
        for code in range(self.ext_count):
            live, dead = code in self.extended, code in self.dead_ext
            if live == dead:
                raise IsaError("%s: ext $%02X must be exactly one of live/dead" % (p, code))
        if any(c >= self.ext_count for c in self.extended):
            raise IsaError("%s: ext opcode beyond meta.ext_count" % p)
        for tbl in (self.primary, self.extended):
            seen = {}
            for op in tbl.values():
                if op.name in seen:
                    raise IsaError("%s: name %r used by $%02X and $%02X" % (p, op.name, seen[op.name], op.code))
                seen[op.name] = op.code
                if op.cf in ("jmp", "cjmp") and not any(f.role == "target" and f.type == "a16" for f in op.fields):
                    raise IsaError("%s: $%02X %s needs a target:a16 field" % (p, op.code, op.cf))
                if op.cf in ("bra", "cbra") and op.field_types() != ["r8"]:
                    raise IsaError("%s: $%02X %s needs one r8 field" % (p, op.code, op.cf))
                if op.cf in CONDITIONAL and op.c not in ("A", "!A"):
                    raise IsaError("%s: $%02X %s needs its taken condition c = \"A\" or \"!A\"" % (p, op.code, op.cf))
                if op.cf == "switch" and op.size is not None:
                    raise IsaError("%s: $%02X switch layout must be variable" % (p, op.code))
                if op.cf != "switch" and op.size is None:
                    raise IsaError("%s: $%02X only a switch may have a variable layout" % (p, op.code))
                if op.ext and op.cf != "none":
                    raise IsaError("%s: ext $%02X must be cf none" % (p, op.code))
                if op.cf == "none" and op.c:
                    self._validate_template(op)
        pre = self.primary.get(self.prefix)
        if pre is None or pre.cf != "prefix" or pre.field_types() != ["e8"]:
            raise IsaError("%s: meta.prefix $%02X must be the cf=prefix op with layout e8" % (p, self.prefix))

    def _validate_template(self, op):
        """A `none` op's lift template must use only the forms the lifter knows."""
        where = "%s: %s$%02X template %r" % (self.path, "ext " if op.ext else "", op.code, op.c)
        steps = parse_template(op.c)
        if not steps:
            raise IsaError(where + ": no step")
        for st in steps:
            if st[0] == "store" and not (TEMPLATE_DIRECT_LV.match(st[1]) or TEMPLATE_INDIRECT_LV.match(st[1])):
                raise IsaError(where + ": unknown store target %r" % st[1])
            if st[0] == "sp_adj" and not op.fields:
                raise IsaError(where + ": sp_adj needs an operand")
        if "{o}" in op.c and not op.fields:
            raise IsaError(where + ": {o} on an op without an explicit operand")
        if ("{slot}" in op.c or "{n}" in op.c) and op.fields:
            raise IsaError(where + ": {slot}/{n} belong to the $00-$7F nibble ranges")

    def op(self, code, ext=False):
        return (self.extended if ext else self.primary).get(code)


TABLE = Table()
