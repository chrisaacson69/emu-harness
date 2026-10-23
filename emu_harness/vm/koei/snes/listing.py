#!/usr/bin/env python3
"""
One decoded bytecode instruction -> listing lines, shared by gen_source.py (the .src layer),
vm_walk.py --list and vm_disasm.py, so the three print the same thing.

A line shows the address, the raw bytes, the instruction with every operand VALUE
(vm_isa.BoundIsa.render: 'ldA    #$1C69', 'ldA    local5 [fp-$0B]', 'call.p $1169,6',
'jz     $20FA'; a switch prints one row per table entry), and an annotation:

  call a16            ; callee name (routines of the module, its resident + coresident modules)
  call to a syscall   ; syscall_bridge: sys_name ($1F)            the id every path pushes
                      ; syscall_bridge: sys_a ($05) | sys_b ($1E) (id by path)
                      ; syscall_bridge (id = computed value)
                        The id is the operand-stack entry on top at the call (the last-pushed
                        word), from vm_lift.module_syscall_ids: a dataflow over the routine CFG,
                        so a call reached by a jump gets every path's id -- never the id of the
                        instruction that merely precedes it in address order.
  stub
  call.ind / ind2     ; *regA
  jmp / jz / bz ...   ; target name, when the target is named
  a16 / i16 operand   ; symbol name and/or "string preview" (see preview())
  $00-$3F frame slot  ; local6 [fp-$0D]  (the variable the opcode's nibble selects)

    L = listing.Lister(isa, S, space, syscall_ids=..., code=...)   # space: vm_walk.Space
    for line in L.lines(ins): print(line)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.vm.koei.snes import vm_isa as ISA  # noqa: E402

STR_MIN = 2          # printable characters before the NUL for a string preview
STR_SHOW = 40        # characters shown
TABLE_RUN = 6        # a printable run this long whose odd bytes are all equal is a word table
LEAD_TEXT = 2        # printable bytes of preceding binary data a string start may follow


def is_text(b):
    return 32 <= b < 127 or b == 0x0A


def raw_string(read, addr, limit=240):
    """The bytes of the NUL-terminated ASCII string at addr (printable + \\n, NUL excluded), or None
    (a non-text byte, a byte off the image, or no NUL within `limit`)."""
    if read is None:
        return None
    out = bytearray()
    for k in range(limit):
        b = read((addr + k) & 0xFFFF)
        if b is None or (b != 0 and not is_text(b)):
            return None
        if b == 0:
            return bytes(out)
        out.append(b)
    return None


def show(bs):
    """Preview text of string bytes ('\\n' escaped), cut at STR_SHOW characters with '...'."""
    s = "".join("\\n" if b == 0x0A else chr(b) for b in bs)
    return s if len(s) <= STR_SHOW else s[:STR_SHOW] + "..."


def string_at(read, addr, limit=240):
    """The NUL-terminated ASCII string at addr (printable + \\n) as preview text, or None."""
    bs = raw_string(read, addr, limit)
    if bs is None or len(bs) < STR_MIN:
        return None
    return show(bs)


def quoted(s):
    return '"%s"' % s.replace('"', '\\"')


def word_table(bs):
    """A printable run that is really a table of words (e.g. pointers $595F $5965 ... read as
    '_YeYkYqY'): at least TABLE_RUN bytes whose odd-offset bytes are all the same."""
    return len(bs) >= TABLE_RUN and len(set(bs[1::2])) == 1


def starts_string(read, v):
    """v can start a string: the byte before it is NUL or not text -- or at most LEAD_TEXT text bytes
    precede it and they follow binary data (a table's last bytes that happen to be printable, e.g.
    `1E 32` before "%s, "). Text back to a NUL, or a longer text run, means v is mid-string."""
    k = 0
    while k <= LEAD_TEXT:
        b = read((v - k - 1) & 0xFFFF)
        if b is None or b == 0 or not is_text(b):
            break
        k += 1
    if k == 0:
        return True
    if k > LEAD_TEXT:
        return False
    b = read((v - k - 1) & 0xFFFF)
    return b is not None and b != 0                    # binary before the short run (not NUL / the start)


def preview(S, v, read_own, read_any=None, code=None):
    """Preview text of the string an a16 / i16 operand value points at, or None:
      - an address strings.toml names (S.kind == "string"): the string, from any module the code
        can see (read_any) -- the symbol table is the authority;
      - otherwise only a NUL-terminated ASCII run in the module's OWN image (read_own) that
        STARTS a string (starts_string), is not decoded code (`code`: bytecode, trampolines,
        native stubs) and is not a word table. A value that merely lands mid-string, in another
        module's bytes or in a switch table gets no preview."""
    v &= 0xFFFF
    if S is not None and S.kind(v) == "string":
        return string_at(read_any or read_own, v)
    if read_own is None or read_own(v) is None:
        return None
    if code is not None and v in code:
        return None
    if not starts_string(read_own, v):
        return None
    bs = raw_string(read_own, v)
    if bs is None or len(bs) < STR_MIN or word_table(bs):
        return None
    if code is not None and any(x in code for x in range(v, v + len(bs) + 1)):
        return None
    return show(bs)


def syscall_text(S, ids):
    """The id part of a syscall annotation / name: ids = a frozenset of pushed constants, or None."""
    if not ids:
        return None
    return " | ".join("%s ($%02X)" % ((S.syscall(i) if S is not None else None) or "syscall", i) for i in sorted(ids))


class Lister:
    """syscall_ids: {call address: frozenset of ids | None} (vm_lift.module_syscall_ids); code: the
    module's decoded bytes (bytecode + trampolines + native), excluded from string previews."""

    def __init__(self, isa, S, space=None, read_any=None, syscall_ids=None, code=None):
        self.isa, self.S, self.space = isa, S, space
        self.read_own = space.read if space is not None else read_any
        self.read_any = read_any if read_any is not None else (space.read_any if space is not None else None)
        self.syscall_ids, self.code = syscall_ids, code

    # -- annotations ---------------------------------------------------------------------
    def name(self, addr):
        return self.S.string(addr) if self.S is not None else None

    def value_note(self, v):
        """Symbol and/or string preview for an address-like operand value."""
        nm = self.name(v & 0xFFFF)
        st = preview(self.S, v, self.read_own, self.read_any, self.code)
        return " ".join(x for x in (nm, quoted(st) if st else None) if x)

    def syscall_note(self, target, ins):
        nm = self.name(target) or "syscall stub $%04X" % target
        if self.syscall_ids is None or ins.addr not in self.syscall_ids:
            return nm + " (id not analysed)"
        ids = self.syscall_ids[ins.addr]
        if ids is None:
            return nm + " (id = computed value)"
        return "%s: %s%s" % (nm, syscall_text(self.S, ids), " (id by path)" if len(ids) > 1 else "")

    def annotation(self, ins):
        if ins.error:
            return ""
        cf = ins.cf
        if cf == "call":
            t = ins.call_target
            if t is None:
                return "*regA"
            if self.space is not None and self.space.is_syscall_stub(t):
                return self.syscall_note(t, ins)
            return self.name(t) or ""
        if cf in ("jmp", "cjmp", "bra", "cbra"):
            return self.name(ins.targets[0]) or ""
        if cf == "switch":
            return ""
        if ins.ext is None and ins.op < 0x40 and not ins.fields:      # frame-matrix slot ops
            return ISA.frame_text(ISA.slot_offset(ins.op & 0x0F))
        notes = []
        for _r, t, v in ins.fields:
            if t in ("a16", "i16"):
                n = self.value_note(v)
                if n:
                    notes.append(n)
        return "  ".join(notes)

    # -- lines -----------------------------------------------------------------------------
    def lines(self, ins, read=None):
        """Listing lines for one instruction (a switch adds one line per table row)."""
        read = read or (self.space.read if self.space is not None else None)
        text, rows = self.isa.render(ins)
        n = max(1, ins.length)
        raw = [read(ins.addr + k) for k in range(n)] if read else []
        head_n = rows[0][0] if rows else n
        ann = self.annotation(ins)
        out = [("  $%04X  %-18s %-30s%s" % (ins.addr, hexbytes(raw[:head_n]), text, ("; " + ann) if ann else "")).rstrip()]
        for off, cnt, rtext in rows:
            tgt = None
            if rtext.split(":")[-1].strip().startswith("$"):
                tgt = int(rtext.split("$")[-1], 16)
            nm = self.name(tgt) if tgt is not None else None
            out.append(("  $%04X  %-18s   %-28s%s" % (ins.addr + off, hexbytes(raw[off:off + cnt]), rtext,
                                                     ("; " + nm) if nm else "")).rstrip())
        return out


def hexbytes(bs):
    return " ".join("%02X" % b for b in bs if b is not None)
