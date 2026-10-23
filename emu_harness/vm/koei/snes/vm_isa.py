#!/usr/bin/env python3
"""
The KOEI SNES bytecode ISA -- the ONE loader for the shared opcode table.

data/vm_opcode_names.toml holds, per opcode, the listing name, the control-flow class and the
operand layout. This module is its only reader: vm_opcodes, vm_disasm, vm_walk, vm_callgraph,
gen_source, vm_lift and vm_struct import it, and none of them keeps its own opcode names,
control-flow sets or operand sizes.

    from emu_harness.vm.koei.snes import vm_isa as ISA
    ISA.TABLE.primary[0xD8].name        -> "jz"
    isa = ISA.bind(rom, P)              # derive sizes from THIS ROM's handlers + cross-check
    ins = isa.decode(read, 0x9008)      # one instruction: length, fields, targets, cf (or .error)
    isa.text(ins)                       # listing name ("jz", "ldA    f8", "mul32", "op_FC", ...)
    isa.render(ins)                     # with operand VALUES ("jz     $20FA", "ldA    local5 [fp-$0B]",
                                        #  "call.p $1169,6"; a switch also returns its case rows)

Sizes come from the ROM: vm_opcodes.derive() walks every path of every handler, and bind()
RAISES IsaMismatch (naming the opcode and both values) when a derived operand size or
control-flow class disagrees with the table (also a switch layout's field order, a conditional
branch's polarity, a lift template's register effects -- see check()). Nothing falls back silently: bind() also needs
the title profile explicitly and checks it against the ROM header (profile.check_rom).

`read(addr)` is any callable returning the byte at a VM address, or None outside the image
(module bounds): a decode that runs off the image is an error, never a wrap into other data.
"""
import os
import re
import sys
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.vm.koei.snes import profile as _prof  # noqa: E402

from emu_harness.vm.koei.isa import (  # noqa: E402  (the shared table model)
    CF_CLASSES, CONDITIONAL, FALLS_THROUGH, FRAME_TYPES, Field, IsaError, LOCAL_BRANCH, Op, RA, RC, SIGNED,
    TABLE, TEMPLATE_DIRECT_LV, TEMPLATE_INDIRECT_LV, TEMPLATE_TOKEN, TYPE_SIZE, Table, layout_size,
    parse_layout, parse_template, step_exprs, template_effects, template_reads, TABLE_PATH)

class IsaMismatch(_prof.ToolError):
    """The ROM's handlers disagree with the shared table (raised by bind())."""


# ---- frame addressing (shared by the lifters) -----------------------------------------
def frame_offset(n):
    """f8/f16 operand n -> byte offset from FP: n+1 if n<0, n+4 if n>=0 (the frame-relative
    fetch helpers of every title, e.g. Aerobiz $A8EA/$A91D)."""
    return n + 1 if n < 0 else n + 4


def slot_offset(k):
    """Frame-matrix slot k ($00-$3F low nibble) -> byte offset from FP."""
    return -23 + 2 * k if k < 12 else 15 + 2 * (k - 12)


def frame_var(off):
    """Canonical name for the frame word at FP+off. Every odd negative offset is a local:
    localK = FP-(2K+1) (local0 = FP-1 .. local11 = FP-23 are the matrix slots; deeper ones
    continue, e.g. local129 = FP-259). Every odd offset >= $0F is an argument: argK =
    FP+$0F+2(K-1) (arg1..arg4 = the matrix slots FP+$0F..$15; arg6 = FP+$19). The remaining
    (even, or FP+0..$0E = the saved $00-$09 + return) offsets are fp_mN / fp_pN."""
    if off < 0 and off % 2:
        return "local%d" % ((-off - 1) // 2)
    if off >= 15 and (off - 15) % 2 == 0:
        return "arg%d" % ((off - 15) // 2 + 1)
    return ("fp_m%d" % -off) if off < 0 else ("fp_p%d" % off)


def frame_text(off):
    """Listing form of a frame operand: the variable and its FP offset, e.g. 'local5 [fp-$0B]'."""
    return "%s [fp%s$%02X]" % (frame_var(off), "-" if off < 0 else "+", abs(off))


def hexs(v):
    """Signed hex: -$1, $1C."""
    return ("-$%X" % -v) if v < 0 else ("$%X" % v)




# ---- decoded instruction -------------------------------------------------------------
class Insn:
    """One decoded instruction. `error` is None, or 'oob' (ran off the image), 'dead'
    (a dead primary opcode) or 'badext' (null / out-of-table extended byte)."""
    __slots__ = ("addr", "op", "ext", "info", "length", "fields", "error")

    def __init__(self, addr, op, ext, info, length, fields, error=None):
        self.addr, self.op, self.ext, self.info = addr, op, ext, info
        self.length, self.fields, self.error = length, fields, error

    @property
    def cf(self):
        return self.info.cf if (self.info and not self.error) else None

    @property
    def mnem(self):
        return self.info.mnem if self.info else None

    @property
    def next(self):
        return self.addr + self.length

    def value(self, role):
        for r, _t, v in self.fields:
            if r == role:
                return v
        return None

    def values_of(self, role):
        """Every value of a (repeated) field, in operand order -- e.g. a switch's cases."""
        return [v for r, _t, v in self.fields if r == role]

    def typed(self, typ):
        """First operand of a field type (e.g. 'a16', 'f16', 's8'), or None."""
        for _r, t, v in self.fields:
            if t == typ:
                return v
        return None

    @property
    def falls_through(self):
        return self.cf in FALLS_THROUGH

    @property
    def targets(self):
        """In-module control targets: [a16] for jmp/cjmp, [PC+r8] for bra/cbra, every a16 of a switch."""
        cf = self.cf
        if cf in ("jmp", "cjmp"):
            return [self.value("target")]
        if cf in ("bra", "cbra"):
            return [(self.next + self.typed("r8")) & 0xFFFF]
        if cf == "switch":
            return [v for _r, t, v in self.fields if t == "a16"]
        return []

    @property
    def call_target(self):
        """Direct call target (a16) of call.n / call.p; None for register-indirect calls."""
        return self.value("target") if self.cf == "call" else None

    @property
    def pop(self):
        """Argument bytes a call pops afterwards (call.p / call.ind2), else 0."""
        v = self.value("pop") if self.cf == "call" else None
        return v or 0

    def switch_default(self):
        return self.value("default") if self.cf == "switch" else None

    def spans(self):
        """(byte offset from the instruction start, role, type, value) for every operand field."""
        o = 2 if self.ext is not None else 1
        out = []
        for r, t, v in self.fields:
            out.append((o, r, t, v))
            o += TYPE_SIZE[t]
        return out


def _read_value(read, a, typ):
    n = TYPE_SIZE[typ]
    v = 0
    for k in range(n):
        b = read(a + k)
        if b is None:
            return None
        v |= b << (8 * k)
    if typ in SIGNED and v & (1 << (8 * n - 1)):
        v -= 1 << (8 * n)
    return v


def _read_fields(fields, read, a, env, out):
    """Decode `fields` at a; returns the next address, or None when a byte is off the image."""
    for f in fields:
        count = 1
        if f.repeat:
            count = env.get(f.repeat)
            if count is None or count < 0:
                raise IsaError("repeat count %r not decoded before its use" % f.repeat)
        for _ in range(count):
            if f.group:
                a = _read_fields(f.group, read, a, env, out)
                if a is None:
                    return None
                continue
            v = _read_value(read, a, f.type)
            if v is None:
                return None
            out.append((f.role, f.type, v))
            env.setdefault(f.role, v)
            a += TYPE_SIZE[f.type]
    return a


class BoundIsa:
    """The shared table bound to one ROM: sizes derived from that ROM's handlers agree with it."""

    def __init__(self, table, derived, P):
        self.table, self.derived, self.P = table, derived, P
        self.prefix = table.prefix
        self.ext_count = table.ext_count

    def is_dead(self, op):
        return op in self.table.dead_primary

    def regs(self, ins):
        """(rd, wr): the VM register bytes ($0A-$11: regA, L-high, regC, R-high) the instruction's
        handler reads before writing / writes, on any path (vm_opcodes, from THIS ROM). Empty for a
        failed decode. The lifter uses them for value widths: a write of $0C-$0D (L-high) makes
        regA's value 32-bit; a read of $0A-$0B alone is a 16-bit read."""
        if ins.error or ins.info is None:
            return frozenset(), frozenset()
        d = self.derived["extended"][ins.ext] if ins.ext is not None else self.derived["primary"][ins.op]
        return frozenset(d.get("rd", [])), frozenset(d.get("wr", []))

    def decode(self, read, v):
        op = read(v)
        if op is None:
            return Insn(v, None, None, None, 1, [], "oob")
        info = self.table.primary.get(op)
        if info is None:
            return Insn(v, op, None, None, 1, [], "dead")
        ext = None
        start = v + 1
        if op == self.prefix:
            ext = read(v + 1)
            if ext is None:
                return Insn(v, op, None, None, 1, [], "oob")
            einfo = self.table.extended.get(ext) if ext < self.ext_count else None
            if einfo is None:
                return Insn(v, op, ext, None, 2, [], "badext")
            info, start = einfo, v + 2
        fields = []
        end = _read_fields(info.fields, read, start, {}, fields)
        if end is None:
            return Insn(v, op, ext, info, max(1, start - v), fields, "oob")
        return Insn(v, op, ext, info, end - v, fields)

    def text(self, ins):
        """The listing name of a decoded instruction (ext ops by their own name). A failed
        decode never shows a live name: dead / off-image -> op_XX, bad ext byte -> ext_XX."""
        if ins.error == "badext":
            return "ext_%02X" % ins.ext
        if ins.error:
            return "op_%02X" % ins.op if ins.op is not None else "??"
        return ins.info.name

    def render(self, ins):
        """(text, rows): the listing text with every operand VALUE (frame operands as
        'local5 [fp-$0B]', immediates '#$1C69' / '#-$1', absolute addresses and jump / branch /
        call targets '$20FA', a call's pop count 'call.p $1169,6'), and for a switch one row per
        table entry after the header fields: (byte offset in the instruction, byte count, text).
        Nothing is elided: every operand byte is shown by some field. Ops whose operand is
        implicit in the opcode (the $00-$7F ranges) and failed decodes return their name."""
        if ins.error or not ins.info.fields:
            return self.text(ins), []
        info, sp = ins.info, ins.spans()
        if info.cf == "switch":
            head, rows, idx, pend = [], [], 0, None
            for o, r, t, v in sp:
                if r == "case":
                    rows.append((o, 2, "case %d: $%04X" % (idx, v)))
                    idx += 1
                elif r == "value":
                    pend = (o, v)
                elif r == "target":
                    rows.append((pend[0], 4, "case $%X: $%04X" % (pend[1] & 0xFFFF, v)))
                elif r == "default" and rows:              # a case list's default follows it
                    rows.append((o, 2, "default: $%04X" % v))
                else:
                    head.append("%s=%s" % (r, ("$%04X" % v) if t == "a16" else
                                           (str(v) if t == "u16" else hexs(v))))
            return "%-6s %s" % (info.mnem, " ".join(head)), rows
        parts = []
        for o, r, t, v in sp:
            if t in FRAME_TYPES:
                parts.append(frame_text(frame_offset(v)))
            elif t == "r8":
                parts.append("$%04X" % ((ins.next + v) & 0xFFFF))
            elif t == "a16":
                parts.append("$%04X" % v)
            elif r == "pop":
                parts.append("%d" % v)
            elif t == "i32":
                parts.append("#$%08X" % v)
            elif t in ("i16", "u16"):
                parts.append("#$%X" % v)
            else:                                            # s8 / s16 immediates
                parts.append("#" + hexs(v))
        return "%-6s %s" % (info.mnem, ",".join(parts)), []


# What a switch layout field's operand word must be used for by the handler (vm_opcodes roles)
SWITCH_ROLE = {"bias": {"add-regA"}, "n": {"compare", "count"}, "value": {"compare"},
               "default": {"pc"}, "target": {"pc"}, "case": {"pc"}}


def layout_offsets(fields, n):
    """(operand offset, role, type, in a repeated part) of every field when each repeat count = n."""
    out, pos = [], [0]

    def walk(fs, rep):
        for f in fs:
            for _ in range(n if f.repeat else 1):
                if f.group:
                    walk(f.group, True)
                else:
                    out.append((pos[0], f.role, f.type, rep or bool(f.repeat)))
                    pos[0] += TYPE_SIZE[f.type]
    walk(fields, False)
    return out


def check_switch_layout(t, d):
    """Disagreements between a switch op's layout and the operand roles its handler shows: every
    fixed field (and each field of a scanned table's first entry) must be used as its role says --
    bias added to regA, n compared / counted, value compared, default / target stored to the PC.
    An indexed table entry (read after the handler moved the PC, e.g. $D5's cases) is not
    observable and is skipped."""
    seen = {}
    for off, role in d.get("roles", []):
        seen.setdefault(off, set()).add(role)
    bad = []
    for n in (0, 1):
        for off, role, typ, rep in layout_offsets(t.fields, n):
            want = SWITCH_ROLE.get(role)
            if want is None:
                continue
            got = seen.get(off)
            if not got:
                if not rep:
                    bad.append("primary $%02X %r: layout field %s:%s at operand +%d is never read by the handler "
                               "at that offset" % (t.code, t.name, role, typ, off))
                continue
            if not got & want:
                bad.append("primary $%02X %r: layout field %s:%s at operand +%d is used as %s by handler $%04X "
                           "(the role needs %s)" % (t.code, t.name, role, typ, off, "/".join(sorted(got)),
                                                   d["handler"], "/".join(sorted(want))))
    return sorted(set(bad))


# The register bytes a template's "A" / "C" stand for: regA / regC for a primary op, the 32-bit
# L / R for an extended op (their low words are regA / regC).
REG_BYTES = {False: {RA: frozenset((0x0A, 0x0B)), RC: frozenset((0x0E, 0x0F))},
             True: {RA: frozenset(range(0x0A, 0x0E)), RC: frozenset(range(0x0E, 0x12))}}


def check_effects(t, d):
    """Disagreements between an op's lift template (the lifter's register model) and the
    register bytes its handler reads / writes on any path (vm_opcodes rd / wr): the template must
    WRITE every register the handler writes (else the lifter keeps a stale value), must READ every
    register the handler reads before writing it (else a live value looks dead), and must not
    claim a write the handler never does. A template may read MORE than the handler (only
    keeps values alive longer). Ops without a template, and calls / branches, are not checked."""
    if t.cf != "none" or not t.c:
        return []
    rd, wr = set(d.get("rd", [])), set(d.get("wr", []))
    where = ("ext $%02X" % t.code) if t.ext else ("primary $%02X" % t.code)
    if -1 in rd or -1 in wr:
        return ["%s %r: handler $%04X touches the direct page at an unknown index -- its register "
                "effects cannot be checked" % (where, t.name, d["handler"])]
    use, dfn = template_effects(parse_template(t.c))
    bad = []
    for bit in (RA, RC):
        nm = ("L" if bit == RA else "R") if t.ext else ("regA" if bit == RA else "regC")
        b = REG_BYTES[t.ext][bit]
        if rd & b and not use & bit:
            bad.append("%s %r: handler $%04X reads %s before writing it; the template %r does not read it"
                       % (where, t.name, d["handler"], nm, t.c))
        if wr & b and not dfn & bit:
            bad.append("%s %r: handler $%04X writes %s; the template %r does not" % (where, t.name, d["handler"], nm, t.c))
        if dfn & bit and not wr & b:
            bad.append("%s %r: the template %r writes %s; handler $%04X never does" % (where, t.name, t.c, nm, d["handler"]))
    return bad


def check_polarity(t, d):
    """Disagreements between a conditional branch's taken condition (the table's c: "A" = taken when
    regA != 0, "!A" = taken when regA == 0) and the handler: every path that writes the PC must have
    branched on regA the way c says, every path that does not, the other way (vm_opcodes records the
    regA condition of each path through a Z branch after a 16-bit LDA $0A)."""
    from emu_harness.vm.koei.snes import vm_opcodes
    want = vm_opcodes.A_NONZERO if t.c == "A" else vm_opcodes.A_ZERO
    other = vm_opcodes.A_ZERO if want == vm_opcodes.A_NONZERO else vm_opcodes.A_NONZERO
    live = [e for e in d.get("exits", []) if e.kind != "BRK"]
    taken = {e.acond for e in live if e.pcw}
    fall = {e.acond for e in live if not e.pcw}
    if taken == {want} and fall == {other}:
        return []
    show = lambda conds: "/".join(sorted("regA %s 0" % c[1:3] if c else "not derived" for c in conds)) or "none"  # noqa: E731
    return ["primary $%02X %r: taken when %s (c = %r), but handler $%04X writes the PC when %s and falls "
            "through when %s" % (t.code, t.name, "regA %s 0" % want[1:3], t.c, d["handler"], show(taken), show(fall))]


def check(table, derived, P):
    """All disagreements between the shared table and the ROM-derived facts, as text lines."""
    bad = []
    prim, ext = derived["primary"], derived["extended"]
    if P.get("extended_prefix", table.prefix) != table.prefix:
        bad.append("prefix: table $%02X, profile extended_prefix $%02X" % (table.prefix, P["extended_prefix"]))
    inv = P.get("invalid_handler")
    if inv is not None:                     # dead = handler is invalid_handler or invalid_handler+2
        for op in range(256):
            at_inv = prim[op]["handler"] in (inv, inv + 2)
            if at_inv != (op in table.dead_primary):
                bad.append("primary $%02X: handler $%04X is %sinvalid_handler(+2) but the table lists it %s"
                           % (op, prim[op]["handler"], "" if at_inv else "not ",
                              "dead" if op in table.dead_primary else "live"))
    for op in range(256):
        d = prim[op]
        if op in table.dead_primary:
            if d["cls"] != "dead":
                bad.append("primary $%02X: table says dead, ROM handler $%04X derives %s %s"
                           % (op, d["handler"], d["cls"], _sz(d["sizes"])))
            continue
        t = table.primary[op]
        if d["cls"] == "dead":
            bad.append("primary $%02X %r: table says live (%s), ROM handler $%04X is dead"
                       % (op, t.name, t.cf, d["handler"]))
            continue
        if t.cf == "prefix":
            if d["cls"] != "prefix" or d["sizes"] != [1]:
                bad.append("primary $%02X prefix: ROM handler $%04X derives %s %s (want prefix size 1)"
                           % (op, d["handler"], d["cls"], _sz(d["sizes"])))
            continue
        want = "var" if t.size is None else [t.size]
        got = "var" if d["cls"] == "switch" else d["sizes"]
        if got != want or d["cls"] != t.cf:
            bad.append("primary $%02X %r: table size %s cf %s -- ROM handler $%04X derives size %s cf %s"
                       % (op, t.name, _sz(want), t.cf, d["handler"], _sz(got), d["cls"]))
        elif t.cf == "switch":
            bad.extend(check_switch_layout(t, d))          # field order / roles of the inline table
        elif t.cf in CONDITIONAL:
            bad.extend(check_polarity(t, d))               # which regA value takes the branch
        else:
            bad.extend(check_effects(t, d))                # the template's regA / regC effects
    for h, n in sorted(P.get("fetch_helpers", {}).items()):      # the profile's own size claims
        d = derived.get("fetch_helpers", {}).get(h)
        if d is not None and (d["cls"] != "none" or d["sizes"] != [n]):
            bad.append("profile fetch_helpers $%04X = %d: ROM helper derives size %s (%s)"
                       % (h, n, _sz(d["sizes"]), d["cls"]))
    for e in range(table.ext_count):
        d = ext[e]
        if e in table.dead_ext:
            if d["cls"] != "dead":
                bad.append("ext $%02X: table says dead, ROM entry $%04X derives %s" % (e, d["handler"], d["cls"]))
            continue
        t = table.extended[e]
        if d["cls"] != t.cf or d["sizes"] != [t.size]:
            bad.append("ext $%02X %r: table size %s cf %s -- ROM handler $%04X derives size %s cf %s"
                       % (e, t.name, t.size, t.cf, d["handler"], _sz(d["sizes"]), d["cls"]))
        else:
            bad.extend(check_effects(t, d))
    return bad


def _sz(s):
    if s == "var":
        return "var"
    return "/".join(str(x) for x in s) if s else "-"


def bind(rom, P, table=None):
    """Derive every handler's operand size + control-flow class from `rom` (vm_opcodes) and
    cross-check them against the shared table. Raises IsaMismatch listing every disagreement.
    P (the title profile) is required and must be this ROM's title (profile.check_rom)."""
    from emu_harness.vm.koei.snes import profile as _profile
    from emu_harness.vm.koei.snes import vm_opcodes
    if P is None:
        raise _profile.ProfileError("vm_isa.bind needs the title profile (pass P; no default)")
    _profile.check_rom(rom, P)
    table = table or TABLE
    derived = vm_opcodes.derive(rom, P, ext_count=table.ext_count)
    bad = check(table, derived, P)
    if bad:
        raise IsaMismatch("shared opcode table vs ROM handlers (%s) -- %d mismatch(es):\n  %s"
                          % (P.get("title", "?"), len(bad), "\n  ".join(bad)))
    return BoundIsa(table, derived, P)


def main(argv):
    """Print the shared table; with a ROM, also bind (derive + cross-check) and report."""
    import argparse
    ap = argparse.ArgumentParser(description="The shared KOEI SNES opcode table (+ optional ROM cross-check).")
    ap.add_argument("rom", nargs="?")
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ROM header)")
    a = ap.parse_args(argv[1:])
    T = TABLE
    print("== %s: %d live primary (+%d dead), %d live extended (+%d dead), prefix $%02X ==" % (
        os.path.relpath(T.path), len(T.primary), len(T.dead_primary), len(T.extended), len(T.dead_ext), T.prefix))
    for code in range(256):
        op = T.primary.get(code)
        if op:
            print("%02X    %-13s %-6s %-5s %-40s %s" % (code, op.name, op.cf, "var" if op.size is None else op.size,
                                                      op.layout or "-", op.c or ""))
    for code in sorted(T.extended):
        op = T.extended[code]
        print("B7 %02X %-13s %-6s %-5s %-40s %s" % (code, op.name, op.cf, op.size, op.layout or "-", op.c or ""))
    if a.rom:
        from emu_harness.vm.koei.snes import profile as _profile
        from emu_harness.machines.snes.mapper import strip_copier_header
        rom, _ = strip_copier_header(open(a.rom, "rb").read())
        P = _profile.select(a.game, rom)
        bind(rom, P)                           # raises IsaMismatch on any disagreement
        print("-- %s: every live opcode's ROM-derived size and class agree with the table" % P["game"])
    return 0


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
