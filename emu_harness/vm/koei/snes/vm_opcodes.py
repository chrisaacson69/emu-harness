#!/usr/bin/env python3
"""
Derive every KOEI SNES bytecode opcode's operand size and control-flow class from the target
ROM's own handler code -- the ground truth the shared table (data/vm_opcode_names.toml) is
cross-checked against: vm_isa.bind() calls derive() and RAISES on any disagreement.

Sizes stay ROM-derived (hand-listed sizes under-counted branchy fetch helpers), but a
fall-through-only reading is not enough either: $D8 is LDA $0A / BEQ / BNE, and the
unreachable fall-through after that pair is $E3's code, so a primary-path walk reads "r8"
where the handler really takes a16. So each handler is abstractly executed along ALL paths
from the dispatch entry state (Y=0, m16/x16, X=opcode):

  - both arms of every data-dependent branch; a branch on a KNOWN flag takes one arm only
    (the BNE after a failed BEQ; the BCC after a fetch helper's CLC; BNE after CPY #n on a
    known Y);
  - JSR callees are entered (fetch helpers, shared tails) and RTS returns to the caller; a
    callee that leaves through an indirect JML is a call-out of the VM (the callee's ret
    resumes after the JSR), e.g. $AC/$E9 via the launcher (Aerobiz $A5F0/$A5F9);
  - jumps into other handlers' shared code are followed;
  - PC arithmetic is tracked: LDA $06 / ADC #n / STA $06 with a known carry advances the PC by
    n + C (ext $18/$19: ADC #3 with C=1 left by CPY #4 -> +4), while STA $06 of a loaded value
    ('abs') or of A + PC ('rel') is a control transfer.

Per path it also records which VM register bytes ($0A-$11: regA, L-high, regC, R-high) the
handler reads before writing and which it writes -- vm_isa.check() compares them with each op's
lift template (the lifter's register model) -- and, when the path went through a branch on the
Z flag of a 16-bit `LDA $0A`, whether regA was zero or non-zero on it (acond). vm_isa.check()
uses that to verify each conditional branch's polarity: the paths that write the PC must be the
ones the table's taken condition ("A" = regA != 0, "!A" = regA == 0) names.

Per path it records adv (bytes pulled from the bytecode PC before control leaves), pcw (first
PC overwrite: None / 'abs' / 'rel'), maxread (highest operand byte read via [$06]), call-outs
and the exit kind. It also records the ROLE of each operand word read at a known offset --
added to regA (ADC $0A), compared (CMP [$06],Y), a count (TAX), stored to the PC -- which
vm_isa.check() uses to verify the switch layouts' field order (e.g. $D5 bias/n/default). The class follows from the paths: none / jmp / cjmp / bra / cbra / switch
(PC always overwritten and the operand read past the advance, or a data-dependent advance) /
call / ret / prefix / dead (BRK).

    py -3 tools/vm_opcodes.py <rom> [--game g]     # derived table vs the shared table (profile: --game,
                                                    # $KOEI_SNES_GAME, or the ROM header -- tools/profile.py)
    py -3 tools/vm_opcodes.py <rom> --op D8        # every path of one primary handler
    py -3 tools/vm_opcodes.py <rom> --ext 18       # ... of one extended handler
Exit status 1 when the ROM disagrees with the shared table.
"""
import argparse
import os
import sys
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import cpu_to_file, strip_copier_header  # noqa: E402
from emu_harness.cpu.w65c816 import isa as w65c816  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402

MAX_STATES = 60000        # abstract states per handler before the analysis gives up (raises)
MAX_ADV = 64              # a path that has advanced the PC this far is a data-dependent loop
MAX_DEPTH = 8             # JSR nesting
UNKNOWN_READ = 1 << 16    # maxread when an operand read's offset is not known

St = namedtuple("St", "pc m8 x8 A asrc X Y C Z N adv pcw pcok maxread callout rstack rd wr zsrc acond")
Exit = namedtuple("Exit", "kind adv pcw maxread callout at rd wr acond")
# zsrc: what the Z flag reflects ("A16" right after a 16-bit LDA $0A, else None); acond: the regA
# condition a path took at a Z branch on it ("A==0" / "A!=0", else None).
A_ZERO, A_NONZERO = "A==0", "A!=0"

# Register bytes of the VM tracked per path: regA $0A-$0B, L-high $0C-$0D, regC $0E-$0F, R-high
# $10-$11. rd = read before written on the path, wr = written. -1 = a DP access at an unknown index.
TRACK = frozenset(range(0x0A, 0x12))
DP_READ = {"LDA", "LDX", "LDY", "ADC", "SBC", "AND", "ORA", "EOR", "CMP", "CPX", "CPY", "BIT"}
DP_WRITE = {"STA", "STX", "STY", "STZ"}
DP_RMW = {"INC", "DEC", "ASL", "LSR", "ROL", "ROR", "TSB", "TRB"}
IDX_OPS = {"LDX", "LDY", "STX", "STY", "CPX", "CPY"}

COND = {"BPL": ("N", 0), "BMI": ("N", 1), "BNE": ("Z", 0), "BEQ": ("Z", 1),
        "BCC": ("C", 0), "BCS": ("C", 1), "BVC": ("V", 0), "BVS": ("V", 1)}
NOFLAGS = {"STA", "STX", "STY", "STZ", "JMP", "JML", "JSR", "JSL", "BRA", "BRL", "PHA", "PHX",
           "PHY", "PHP", "PHK", "PHB", "PHD", "PEA", "PEI", "PER", "TCS", "TXS", "NOP", "WDM",
           "MVN", "MVP", "CLD", "SED", "CLI", "SEI", "CLV", "WAI", "RTS", "RTL"}
READS_OPND = {"LDA", "CMP", "ADC", "SBC", "AND", "ORA", "EOR", "BIT"}


class AnalysisError(RuntimeError):
    pass


def _nz(v, w8):
    if v is None:
        return None, None
    return (1 if v == 0 else 0), (v >> (7 if w8 else 15)) & 1


def _mask(v, w8):
    return None if v is None else v & (0xFF if w8 else 0xFFFF)


def loop_addrs(P):
    """Addresses that ARE the interpreter loop for handler analysis: interpreter_loop and the
    byte after it. rot3k2's profile value is the PLB one byte before its loop head (Gemfire and
    Aerobiz point at the head); both spellings are accepted here, in this one place (vm_semantics
    uses it too). See CONTEXT.md, re-grade backlog."""
    il = P["interpreter_loop"]
    return {il, il + 1}


class Deriver:
    def __init__(self, rom, P):
        self.rom, self.P = rom, P
        self.m = _profile.mapping(P)
        self.loop = loop_addrs(P)
        self.roles = set()           # (operand offset, role) seen while analysing one handler

    def ro(self, a):
        if 0x8000 <= a <= 0xFFFF:
            f = cpu_to_file(self.m, 0, a)
            return f if f < len(self.rom) else None
        return None

    def word(self, a):
        f = cpu_to_file(self.m, 0, a)
        return self.rom[f] | (self.rom[f + 1] << 8)

    # -- the all-paths abstract executor ----------------------------------------------
    def explore(self, entry):
        exits, seen, work = [], set(), [entry]
        while work:
            s = work.pop()
            if s in seen:
                continue
            seen.add(s)
            if len(seen) > MAX_STATES:
                raise AnalysisError("handler $%04X: more than %d abstract states" % (entry.pc, MAX_STATES))
            if s.adv > MAX_ADV:
                exits.append(Exit("VAR", s.adv, s.pcw, s.maxread, s.callout, s.pc, s.rd, s.wr, s.acond))
                continue
            if s.pc in self.loop and not s.rstack:
                exits.append(Exit("LOOP", s.adv, s.pcw, s.maxread, s.callout, s.pc, s.rd, s.wr, s.acond))
                continue
            f = self.ro(s.pc)
            if f is None:
                exits.append(Exit("FAR", s.adv, s.pcw, s.maxread, s.callout, s.pc, s.rd, s.wr, s.acond))
                continue
            ins, nm8, nx8 = w65c816.decode(self.rom, f, 0, s.pc, s.m8, s.x8)
            for r in self.step(s, ins, nm8, nx8):
                if isinstance(r, Exit):
                    exits.append(r)
                else:
                    work.append(r)
        return exits

    def dp_effects(self, s, ins):
        """(rd, wr) after this instruction: the register bytes it reads (before any write on the
        path) and writes. Indirect modes read their pointer bytes; abs $00xx is the direct page."""
        mn, md, raw = ins["mnem"], ins["mode"], ins["raw"]
        reads, writes = set(), set()
        width = 1 if (s.x8 if mn in IDX_OPS else s.m8) else 2
        base = None
        if md in ("dp", "dpx", "dpy"):
            base = raw[1]
        elif md in ("abs", "abx", "aby") and len(raw) > 2 and raw[2] == 0:
            base = raw[1]
        if base is not None:
            idx = 0 if md in ("dp", "abs") else (s.X if md in ("dpx", "abx") else s.Y)
            addrs = {-1} if idx is None else {(base + idx + k) & 0xFFFF for k in range(width)}
            if mn in DP_READ or mn in DP_RMW:
                reads |= addrs
            if mn in DP_WRITE or mn in DP_RMW:
                writes |= addrs
        elif md in ("idp", "idx", "idy", "idl", "idly", "pei"):
            idx = s.X if md == "idx" else 0
            n = 3 if md in ("idl", "idly") else 2
            reads |= {-1} if idx is None else {(raw[1] + idx + k) & 0xFFFF for k in range(n)}
        reads = {a for a in reads if a in TRACK or a == -1}
        writes = {a for a in writes if a in TRACK or a == -1}
        return s.rd | frozenset(reads - s.wr), s.wr | frozenset(writes)

    def step(self, s, ins, nm8, nx8):
        mn, md, raw, tgt = ins["mnem"], ins["mode"], ins["raw"], ins["target"]
        rd, wr = self.dp_effects(s, ins)
        s = s._replace(rd=rd, wr=wr)
        nxt = (s.pc + ins["len"]) & 0xFFFF
        dp = raw[1] if len(raw) > 1 else None
        m8, x8 = s.m8, s.x8
        A, asrc, X, Y, C, Z, N = s.A, s.asrc, s.X, s.Y, s.C, s.Z, s.N
        adv, pcw, pcok, maxread = s.adv, s.pcw, s.pcok, s.maxread
        imm = None
        if md in ("imm_m", "imm_x", "imm8"):
            imm = raw[1] | ((raw[2] << 8) if len(raw) > 2 else 0)

        def ex(kind, **kw):
            return Exit(kind, kw.get("adv", adv), kw.get("pcw", pcw), kw.get("maxread", maxread),
                        kw.get("callout", s.callout), s.pc, s.rd, s.wr, s.acond)

        # ---- control transfers ---------------------------------------------------------
        if mn in ("BRK", "COP", "STP", "WAI"):
            return [ex("BRK")]
        if mn in ("RTS", "RTL", "RTI"):
            if s.rstack:
                return [s._replace(pc=s.rstack[-1], rstack=s.rstack[:-1], m8=nm8, x8=nx8)]
            return [ex("RTS")]
        if mn in ("JMP", "JML"):
            if md in ("abs", "abl") and tgt and (md == "abs" or tgt[0] == 0):
                if tgt[1] in self.loop:
                    return [ex("LOOP")]
                return [s._replace(pc=tgt[1], m8=nm8, x8=nx8)]
            return [self.indirect(s, ex, nm8, nx8)]
        if mn in ("JSR", "JSL"):
            ret = nxt
            if md in ("abs", "abl") and tgt and (md == "abs" or tgt[0] == 0):
                if len(s.rstack) >= MAX_DEPTH:
                    raise AnalysisError("JSR nesting deeper than %d at $%04X" % (MAX_DEPTH, s.pc))
                return [s._replace(pc=tgt[1], rstack=s.rstack + (ret,), m8=nm8, x8=nx8)]
            # JSR (abs,X) / JSL far: an out-call that returns here, registers unknown
            return [s._replace(pc=ret, A=None, asrc=None, X=None, Y=None, C=None, Z=None, N=None,
                               zsrc=None, callout=True, m8=nm8, x8=nx8)]
        if mn in ("BRA", "BRL"):
            return [s._replace(pc=tgt[1], m8=nm8, x8=nx8)]
        if mn in COND:
            flag, want = COND[mn]
            cur = {"N": N, "Z": Z, "C": C}.get(flag)       # V is never tracked
            taken = s._replace(pc=tgt[1], m8=nm8, x8=nx8)
            fall = s._replace(pc=nxt, m8=nm8, x8=nx8)
            if cur is not None:
                return [taken if cur == want else fall]
            if flag == "V":
                return [taken, fall]
            taken, fall = taken._replace(**{flag: want}), fall._replace(**{flag: 1 - want})
            if flag == "Z" and s.zsrc == "A16":         # a branch on regA == 0: record each arm's condition
                taken = taken._replace(acond=A_ZERO if want else A_NONZERO)
                fall = fall._replace(acond=A_NONZERO if want else A_ZERO)
            return [taken, fall]

        # ---- the bytecode PC ($06-$08) ------------------------------------------------
        opk = None                                  # operand offset of a [$06] read, when known
        if md in ("idl", "idly") and dp == 0x06 and mn in READS_OPND:
            if pcok:
                if md == "idl" or Y is not None:
                    off = adv + (Y if md == "idly" else 0)
                    maxread = max(maxread, off + (1 if m8 else 2) - 1)
                    opk = off
                    if mn == "CMP":
                        self.roles.add((off, "compare"))
                else:
                    maxread = UNKNOWN_READ
        if md == "dp" and dp in (0x06, 0x07, 0x08):
            if mn == "INC" and dp == 0x06:
                if pcok:
                    adv += 1
                return [s._replace(pc=nxt, adv=adv, maxread=maxread, Z=None, N=None, zsrc=None, m8=nm8, x8=nx8)]
            if mn in ("STA", "STX", "STY", "STZ"):
                if dp != 0x06 or mn != "STA" or m8:
                    raise AnalysisError("unexpected write to the PC byte $%02X at $%04X (%s)" % (dp, s.pc, mn))
                if asrc and asrc[0] == "pc":
                    if pcok:
                        adv += asrc[1]                      # PC skip = operand advance
                elif asrc and asrc[0] == "pcu":
                    raise AnalysisError("PC advance at $%04X depends on an unknown carry" % s.pc)
                else:
                    if asrc and asrc[0] == "opnd" and asrc[1] is not None:
                        self.roles.add((asrc[1], "pc"))        # an operand word becomes the PC
                    pcw = pcw or ("rel" if asrc == ("rel",) else "abs")
                    pcok = False
                return [s._replace(pc=nxt, adv=adv, pcw=pcw, pcok=pcok, maxread=maxread, m8=nm8, x8=nx8)]
            if mn in ("DEC", "ASL", "LSR", "ROL", "ROR", "TSB", "TRB", "INC"):
                raise AnalysisError("unexpected RMW of the PC byte $%02X at $%04X (%s)" % (dp, s.pc, mn))

        # ---- register / flag effects ---------------------------------------------------
        nA, nasrc, nX, nY, nC, nZ, nN = A, asrc, X, Y, C, Z, N
        wa = m8           # accumulator width for this instruction
        wi = x8           # index width
        res, rw = None, wa  # the flag-setting result (value, width8)
        setflags = mn not in NOFLAGS and mn not in COND
        if mn == "LDA":
            if imm is not None:
                nA = (imm if not wa else (((A & 0xFF00) | imm) if A is not None else None))
                res = imm
            else:
                nA = None
            if md == "dp" and dp == 0x06 and not wa:
                nasrc = ("pc", 0)
            elif md in ("idl", "idly") and dp == 0x06:
                nasrc = ("opnd", opk)
            else:
                nasrc = None
        elif mn in ("LDX", "LDY"):
            v = imm if imm is not None else None
            if mn == "LDX":
                nX = v
            else:
                nY = v
            res, rw = v, wi
        elif mn in ("TAX", "TAY"):
            if mn == "TAX" and asrc and asrc[0] == "opnd" and asrc[1] is not None:
                self.roles.add((asrc[1], "count"))
            v = _mask(A, wi)                        # x16 takes all of C (incl. B), x8 the low byte
            if mn == "TAX":
                nX = v
            else:
                nY = v
            res, rw = v, wi
        elif mn in ("TXA", "TYA"):
            v = X if mn == "TXA" else Y
            nA = (v if not wa else (((A & 0xFF00) | (v & 0xFF)) if (A is not None and v is not None) else None))
            nasrc = None
            res, rw = (_mask(v, wa)), wa
        elif mn in ("TXY", "TYX"):
            v = X if mn == "TXY" else Y
            if mn == "TXY":
                nY = v
            else:
                nX = v
            res, rw = v, wi
        elif mn in ("INX", "INY", "DEX", "DEY"):
            cur = X if mn in ("INX", "DEX") else Y
            v = None if cur is None else _mask(cur + (1 if mn.startswith("IN") else -1), wi)
            if mn in ("INX", "DEX"):
                nX = v
            else:
                nY = v
            res, rw = v, wi
        elif mn in ("INC", "DEC") and md == "acc":
            nA = None if A is None else ((A & 0xFF00) | ((A + (1 if mn == "INC" else -1)) & 0xFF) if wa
                                         else (A + (1 if mn == "INC" else -1)) & 0xFFFF)
            nasrc = None
            res = _mask(nA, wa)
        elif mn in ("CMP", "CPX", "CPY"):
            if mn == "CMP" and md == "dp" and dp == 0x0A and asrc and asrc[0] == "opnd" and asrc[1] is not None:
                self.roles.add((asrc[1], "compare"))       # operand word compared with regA
            reg = {"CMP": A, "CPX": X, "CPY": Y}[mn]
            w = wa if mn == "CMP" else wi
            if imm is not None and reg is not None:
                r = _mask(reg, w)
                nC = 1 if r >= imm else 0
                res, rw = _mask(r - imm, w), w
                nZ, nN = _nz(res, w)
                setflags = False
            else:
                nC = None
        elif mn in ("ADC", "SBC"):
            if mn == "ADC" and md == "dp" and dp == 0x0A and asrc and asrc[0] == "opnd" and asrc[1] is not None:
                self.roles.add((asrc[1], "add-regA"))      # operand word + regA
            if md == "dp" and dp == 0x06:
                nasrc = ("rel",)                   # A + PC: a relative branch target
                nA, nC = None, None
            elif imm is not None and asrc and asrc[0] == "pc" and not wa:
                if C is None:
                    nasrc = ("pcu",)
                else:
                    nasrc = ("pc", asrc[1] + (imm + C if mn == "ADC" else -(imm + 1 - C)))
                nA, nC = None, None
            elif imm is not None and A is not None and C is not None:
                full = 0xFF if wa else 0xFFFF
                a = A & full
                t = a + imm + C if mn == "ADC" else a - imm - (1 - C)
                nC = (1 if t > full else 0) if mn == "ADC" else (1 if t >= 0 else 0)
                t &= full
                nA = ((A & 0xFF00) | t) if wa else t
                res = t
                nasrc = None
            else:
                nA, nC, nasrc = None, None, None
        elif mn in ("AND", "ORA", "EOR"):
            if imm is not None and A is not None:
                full = 0xFF if wa else 0xFFFF
                a = A & full
                t = (a & imm) if mn == "AND" else ((a | imm) if mn == "ORA" else (a ^ imm))
                nA = ((A & 0xFF00) | t) if wa else t
                res = t
            else:
                nA = None
            nasrc = None
        elif mn in ("ASL", "LSR", "ROL", "ROR"):
            if md == "acc":
                nA, nasrc = None, None
            nC = None
        elif mn == "XBA":
            nA = None if A is None else ((A >> 8) | ((A & 0xFF) << 8))
            nasrc = None
            res, rw = (None if nA is None else nA & 0xFF), True
        elif mn in ("PLA", "TSC", "TDC"):
            nA, nasrc = None, None
        elif mn in ("PLX", "TSX"):
            nX = None
        elif mn == "PLY":
            nY = None
        elif mn in ("CLC", "SEC"):
            nC = 0 if mn == "CLC" else 1
            setflags = False
        elif mn in ("REP", "SEP"):
            if imm & 0x01:
                nC = 0 if mn == "REP" else 1
            if imm & 0x02:
                nZ = 0 if mn == "REP" else 1
            if imm & 0x80:
                nN = 0 if mn == "REP" else 1
            if mn == "SEP" and imm & 0x10:      # x8: index high bytes clear
                nX, nY = _mask(X, True), _mask(Y, True)
            setflags = False
        elif mn in ("PLP", "XCE"):
            nC = nZ = nN = None
            setflags = False
        elif mn in ("MVN", "MVP"):
            nA = nX = nY = None
            nasrc = None
        elif mn in ("STA", "STX", "STY", "STZ", "PHA", "PHX", "PHY", "PHP", "PHK", "PHB", "PHD",
                    "PEA", "PEI", "PER", "TCS", "TXS", "NOP", "WDM", "CLD", "SED", "CLI", "SEI",
                    "CLV", "TCD", "PLB", "PLD", "BIT", "TSB", "TRB", "INC", "DEC"):
            pass                # no tracked register changes (flags per NOFLAGS: unknown or kept)
        else:
            raise AnalysisError("no transfer rule for %s (%s) at $%04X" % (mn, md, s.pc))
        if setflags:
            nZ, nN = _nz(res, rw)
        zsrc = s.zsrc
        if nZ != Z or setflags or mn in ("CMP", "CPX", "CPY", "REP", "SEP", "PLP", "XCE"):
            zsrc = None                                 # Z now reflects something else
        if mn == "LDA" and md == "dp" and dp == 0x0A and not wa:
            zsrc = "A16"                                # Z = (regA == 0), all 16 bits
        return [s._replace(pc=nxt, m8=nm8, x8=nx8, A=nA, asrc=nasrc, X=nX, Y=nY, C=nC, Z=nZ, N=nN,
                           adv=adv, pcw=pcw, pcok=pcok, maxread=maxread, zsrc=zsrc)]

    def indirect(self, s, ex, nm8, nx8):
        """An indirect JMP/JML. Inside a JSR'd sub it is a call-out of the VM (the callee's ret
        comes back through the pushed return address): resume at the innermost return."""
        if s.rstack:
            return s._replace(pc=s.rstack[-1], rstack=s.rstack[:-1], A=None, asrc=None, X=None,
                              Y=None, C=None, Z=None, N=None, zsrc=None, callout=True, m8=nm8, x8=nx8)
        return ex("IND")

    # -- per-handler result -----------------------------------------------------------
    def analyse(self, handler, x, ext=False, is_prefix=False):
        self.roles = set()
        if handler == 0:
            return dict(handler=0, sizes=[], cls="dead", exits=[], note="null entry")
        if self.ro(handler) is None:
            return dict(handler=handler, sizes=[], cls="unknown", exits=[], note="handler outside ROM")
        z, n = _nz(x, False)
        entry = St(handler, False, False, None, None, x, 0, None, None if ext else z,
                   None if ext else n, 0, None, True, -1, False, (), frozenset(), frozenset(), None, None)
        try:
            exits = self.explore(entry)
        except AnalysisError as e:
            return dict(handler=handler, sizes=[], cls="unknown", exits=[], note=str(e))
        cls, sizes, note = classify(exits, is_prefix, ext)
        live = [e for e in exits if e.kind != "BRK"]
        rd = sorted(set().union(*[e.rd for e in live])) if live else []
        wr = sorted(set().union(*[e.wr for e in live])) if live else []
        return dict(handler=handler, sizes=sizes, cls=cls, exits=exits, note=note, roles=sorted(self.roles),
                    rd=rd, wr=wr)


def classify(exits, is_prefix=False, ext=False):
    """(class, sorted sizes, note) from one handler's path exits."""
    kinds = {e.kind for e in exits}
    advs = sorted({e.adv for e in exits if e.kind != "VAR"})
    if not exits:
        return "unknown", [], "no exits"
    if kinds == {"BRK"}:
        return "dead", [], "BRK"
    if kinds & {"FAR", "BRK"}:
        return "unknown", advs, "paths leave the handler: %s" % ",".join(sorted(kinds))
    var = "VAR" in kinds
    overread = any(e.maxread >= e.adv for e in exits if e.kind != "VAR")
    if kinds == {"IND"}:                 # control leaves the loop through an indirect jump
        if len(advs) != 1 or any(e.callout or e.pcw for e in exits):
            return "unknown", advs, "indirect exit with variable advance / call-out / PC write"
        return ("prefix" if is_prefix else "ret"), advs, ""
    if "IND" in kinds:
        return "unknown", advs, "indirect exit on some paths only"
    if all(e.callout for e in exits):
        if var or len(advs) != 1 or overread or any(e.pcw for e in exits):
            return "unknown", advs, "call with variable advance / PC write"
        return "call", advs, ""
    if any(e.callout for e in exits):
        return "unknown", advs, "call-out on some paths only"
    written = [e.pcw for e in exits if e.kind != "VAR"]    # VAR = a loop still running, not an exit
    wset = {w for w in written if w}
    if not wset:
        if var or len(advs) != 1 or overread:
            return "unknown", advs, "no PC write but variable advance / operand over-read"
        return "none", advs, ""
    if all(written):
        if var or len(advs) != 1 or overread:
            return "switch", advs, "inline table" + (" (data-dependent advance)" if var else "")
        if wset == {"abs"}:
            return "jmp", advs, ""
        if wset == {"rel"}:
            return "bra", advs, ""
        return "unknown", advs, "mixed abs/rel PC writes"
    if var or len(advs) != 1 or overread:
        return "unknown", advs, "conditional PC write with variable advance"
    if wset == {"abs"}:
        return "cjmp", advs, ""
    if wset == {"rel"}:
        return "cbra", advs, ""
    return "unknown", advs, "mixed abs/rel PC writes"


def tables(rom, P, ext_count=48):
    d = Deriver(rom, P)
    prim = [d.word(P["primary_table"] + 2 * i) for i in range(256)]
    ext = [d.word(P["extended_table"] + 2 * i) for i in range(ext_count)]
    return prim, ext


def derive(rom, P, ext_count=48):
    """{'primary': {op: result}, 'extended': {e: result}} for every primary opcode and every
    extended entry below ext_count. result = {handler, sizes, cls, exits, note}."""
    d = Deriver(rom, P)
    prim, ext = tables(rom, P, ext_count)
    inv = P["invalid_handler"]
    out_p, out_e = {}, {}
    for op, h in enumerate(prim):
        r = d.analyse(h, op, is_prefix=(h == P["ext_interpreter"]))
        if h in (inv, inv + 2) and r["cls"] != "dead":
            r["note"] = ("invalid_handler slot but not BRK; " + r["note"]).strip("; ")
        out_p[op] = r
    for e, h in enumerate(ext):
        out_e[e] = d.analyse(h, e * 2, ext=True)
    # the profile's fetch_helpers claim a byte count each; derive it the same way (JSR'd with Y=0)
    out_f = {h: d.analyse(h, 0, ext=True) for h in P.get("fetch_helpers", {})}
    return {"primary": out_p, "extended": out_e, "fetch_helpers": out_f}


def _fmt_sizes(r):
    if r["cls"] == "switch":
        return "var"
    return "/".join(str(x) for x in r["sizes"]) or "-"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("rom")
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ONE profile "
                                                 "whose header_title matches the ROM; no default)")
    ap.add_argument("--op", default=None, help="hex primary opcode: dump every path of its handler")
    ap.add_argument("--ext", default=None, help="hex extended opcode: dump every path of its handler")
    a = ap.parse_args(argv[1:])
    from emu_harness.vm.koei.snes import vm_isa as ISA
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    game = P["game"]
    T = ISA.TABLE
    if a.op or a.ext:
        ext = a.ext is not None
        code = int(a.ext if ext else a.op, 16)
        prim, etab = tables(rom, P, T.ext_count)
        h = etab[code] if ext else prim[code]
        r = Deriver(rom, P).analyse(h, code * 2 if ext else code, ext=ext, is_prefix=(not ext and h == P["ext_interpreter"]))
        print("%s $%02X handler $%04X -> class %s size %s %s" % ("ext" if ext else "op", code, h, r["cls"], _fmt_sizes(r), r["note"]))
        if r.get("rd") or r.get("wr"):
            print("  register bytes read before written: %s; written: %s" % (
                " ".join("$%02X" % b if b >= 0 else "?" for b in r["rd"]) or "-",
                " ".join("$%02X" % b if b >= 0 else "?" for b in r["wr"]) or "-"))
        if r.get("roles"):
            ro = r["roles"]
            print("  operand roles: " + ", ".join("+%d %s" % x for x in ro[:12]) + (" ..." if len(ro) > 12 else ""))
        for e in sorted(set(r["exits"]), key=lambda e: (e.kind, e.adv, e.pcw or "", e.acond or "")):
            print("  exit %-5s at $%04X adv=%-3d pcw=%-4s maxread=%-3s%s%s" % (
                e.kind, e.at, e.adv, e.pcw or "-", e.maxread if e.maxread < UNKNOWN_READ else "?",
                "  call-out" if e.callout else "", ("  when regA %s 0" % e.acond[1:3]) if e.acond else ""))
        return 0
    derived = derive(rom, P, T.ext_count)
    bad = ISA.check(T, derived, P)
    print("== %s (%s): handlers derived from the ROM vs data/vm_opcode_names.toml ==" % (game, P.get("title", "")))
    print("op    handler  derived        shared table")
    live = 0
    for op in range(256):
        r = derived["primary"][op]
        t = T.primary.get(op)
        tsz = "dead" if t is None else ("var" if t.size is None else str(t.size))
        tcf = "" if t is None else t.cf
        live += r["cls"] != "dead"
        ok = "" if not any(l.startswith("primary $%02X" % op) for l in bad) else "   <-- MISMATCH"
        print("%02X    $%04X    %-6s %-6s  %-5s %-6s %s%s" % (
            op, r["handler"], r["cls"], _fmt_sizes(r), tsz, tcf, (t.name if t else "op_%02X" % op), ok))
    elive = 0
    for e in range(T.ext_count):
        r = derived["extended"][e]
        t = T.extended.get(e)
        elive += r["cls"] != "dead"
        ok = "" if not any(l.startswith("ext $%02X" % e) for l in bad) else "   <-- MISMATCH"
        print("B7 %02X $%04X    %-6s %-6s  %-5s %-6s %s%s" % (
            e, r["handler"], r["cls"], _fmt_sizes(r), "dead" if t is None else str(t.size),
            "" if t is None else t.cf, t.name if t else "(null)", ok))
    print("-- live primary %d (dead %d), live extended %d of %d; mismatches: %d" % (
        live, 256 - live, elive, T.ext_count, len(bad)))
    for l in bad:
        print("MISMATCH " + l)
    return 1 if bad else 0


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
