#!/usr/bin/env python3
"""
Recursive-descent walker for KOEI SNES bytecode modules (the bytecode analog of
snes-decompiler/tools/walk65816.py), decoding through the shared ISA (vm_isa).

A module is one archive blob loaded at its WRAM base (`JMP base+3` header). Its routines are
the per-routine trampolines -- every occurrence of the profile's `routine_sig` (e.g. JSR $1C10)
-- and each body starts `routine_body_offset` bytes in. From each body the walker follows
control flow by the opcode's class (vm_isa): fall-through, jmp/jz/jnz a16 targets, bra/bz/bnz
r8 targets, every switch case + default; ret stops. Reads are bounded to the module image.

Calls are recorded, never walked into: the target (a16) is classified in the address space the
module runs in -- itself + the profile's resident_modules + its coresident modules:
  intra / resident / coresident   the target starts with routine_sig (a bytecode routine)
  syscall                         a native stub that JSLs to the profile's abi_syscall
                                  trampoline (read from the ROM: Aerobiz COMMON $1C14,
                                  Gemfire root $328B, rot3k2 root $23EA)
  native                          any other non-routine target (a native 65C816 helper)
  unmapped                        no visible module holds it

Code-vs-data checks, all reported per module:
  dead-op hits   a dead primary opcode, a null / out-of-table ext byte (decode errors)
  conflicts      a byte claimed by two different instruction decodes
  rejected       a signature match whose trampoline bytes are code of another routine (a
                 spurious match inside real code) -- listed, never silently dropped. Only a
                 routine whose own walk is error-free can reject another.

    py -3 tools/vm_walk.py <rom> [--game g] --all                  # every code module + totals
    py -3 tools/vm_walk.py <rom> [--game g] --module COMMON [--list] [--calls] [--syms data/COMMON]
    py -3 tools/vm_walk.py <rom> --start 01:8003 --base 7E:1003    # legacy window -> its module
(profile: --game, else $KOEI_SNES_GAME, else the ROM header -- tools/profile.py)
"""
import argparse
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import cpu_to_file, strip_copier_header, LOROM  # noqa: E402
from emu_harness.vm.koei.snes import archive  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402
from emu_harness.vm.koei.snes import vm_isa as ISA  # noqa: E402


class Space:
    """The WRAM a module runs in: the module itself, the resident modules, its coresident ones."""

    def __init__(self, rom, P, mod):
        self.P, self.mod = P, mod
        self.sig = bytes(P.get("routine_sig", []))
        self.body_off = P.get("routine_body_offset", 0)
        sc = P.get("abi_syscall")                  # a syscall stub is `JSL $00:abi_syscall`
        self.syscall_jsl = bytes([0x22, sc & 0xFF, (sc >> 8) & 0xFF, 0x00]) if sc is not None else None
        self.resident = set(_profile.resident_modules(P))
        self.coresident = set(_profile.coresident(P, mod.name))
        self.mods = [mod]
        for name in _profile.visible_modules(P, mod.name)[1:]:
            try:
                self.mods.append(archive.module(rom, P, name))
            except KeyError as e:
                raise KeyError("profile names module %r (resident/coresident of %s): %s" % (name, mod.name, e))

    def read(self, v):
        """Byte at VM address v inside THIS module's image, else None (decode bound)."""
        m = self.mod
        return m.image[v - m.base] if m.base <= v < m.end else None

    def read_any(self, v):
        """Byte at VM address v in any visible module (strings / data it can address), else None."""
        m = self.owner(v)
        return m.image[v - m.base] if m is not None else None

    def owner(self, v):
        for m in self.mods:
            if m.base <= v < m.end:
                return m
        return None

    def is_routine(self, m, v):
        o = v - m.base
        return bool(self.sig) and 0 <= o and m.image[o:o + len(self.sig)] == self.sig

    def is_entry(self, v):
        """v is a routine trampoline (routine_sig) in THIS module."""
        return self.is_routine(self.mod, v)

    def body(self, entry):
        """Where a routine's bytecode starts: entry + routine_body_offset for a trampoline."""
        return entry + self.body_off if self.is_entry(entry) else entry

    def is_syscall_stub(self, target):
        """target is a native stub whose first instruction is JSL to the profile's abi_syscall."""
        m = self.owner(target)
        if m is None or self.syscall_jsl is None:
            return False
        o = target - m.base
        return m.image[o:o + 4] == self.syscall_jsl

    def classify_call(self, target):
        """(class, module name) of a direct call target."""
        m = self.owner(target)
        if m is None:
            return "unmapped", None
        if not self.is_routine(m, target):
            return ("syscall" if self.is_syscall_stub(target) else "native"), m.name
        if m is self.mod:
            return "intra", m.name
        return ("resident" if m.name in self.resident else "coresident"), m.name


def signature_scan(mod, sig):
    """Every routine_sig occurrence in the module image (the trampoline candidates)."""
    out, i = [], mod.image.find(sig) if sig else -1
    while i >= 0:
        out.append(mod.base + i)
        i = mod.image.find(sig, i + 1)
    return out


class RoutineWalk:
    __slots__ = ("entry", "body", "insns", "errors", "calls")

    def __init__(self, entry, body):
        self.entry, self.body = entry, body
        self.insns, self.errors, self.calls = {}, [], []


def walk_routine(isa, space, entry):
    """Decode one routine body by control flow; bounded to the module image. `entry` is a
    trampoline (the body starts routine_body_offset in) or, if it is not one, a body address."""
    mod = space.mod
    r = RoutineWalk(entry, space.body(entry))
    work = [r.body]
    while work:
        v = work.pop()
        while True:
            if v in r.insns:
                break
            if not (mod.base <= v < mod.end):
                r.errors.append(("out-of-module", v))
                break
            ins = isa.decode(space.read, v)
            r.insns[v] = ins
            if ins.error:
                r.errors.append((ins.error, v))
                break
            cf = ins.cf
            if cf == "call":
                r.calls.append(ins)
            if cf in ISA.LOCAL_BRANCH:
                work.extend(ins.targets)
            if not ins.falls_through:
                break
            v = ins.next
    return r


class ModuleWalk:
    def __init__(self, mod):
        self.mod = mod
        self.candidates, self.walks = [], {}
        self.routines, self.rejected = [], {}
        self.insns, self.conflicts = {}, []
        self.dead_hits, self.decode_errors = [], []
        self.calls = []            # (site, routine, cls, module, target, op)

    def stats(self):
        cc = Counter(c[2] for c in self.calls)
        return dict(module=self.mod.name, base=self.mod.base, end=self.mod.end,
                    candidates=len(self.candidates), routines=len(self.routines),
                    rejected=len(self.rejected), instructions=len(self.insns),
                    code_bytes=sum(i.length for i in self.insns.values()),
                    conflicts=len(self.conflicts), dead_hits=len(self.dead_hits),
                    decode_errors=len(self.decode_errors), calls=dict(cc))


def walk_module(isa, space):
    """Walk every trampoline candidate of space.mod; reject spurious ones; count conflicts."""
    mod = space.mod
    W = ModuleWalk(mod)
    W.candidates = signature_scan(mod, space.sig)
    for e in W.candidates:
        W.walks[e] = walk_routine(isa, space, e)
    # Spurious signature matches sit INSIDE real code: reject a candidate whose trampoline
    # bytes are code of a candidate that is itself uncovered ("clean") AND whose own walk is
    # error-free (a garbage walk from a match inside data must not reject a real routine);
    # iterate to a fixpoint.
    live = set(W.candidates)
    cover = {}                                    # byte -> candidates whose walk decodes it
    for c, r in W.walks.items():
        for a, ins in r.insns.items():
            if not ins.error:
                for b in range(a, a + ins.length):
                    cover.setdefault(b, set()).add(c)
    covered_by = {}
    for e in W.candidates:
        s = set()
        for b in range(e, e + max(1, space.body_off)):
            s |= cover.get(b, set())
        s.discard(e)
        if s:
            covered_by[e] = s
    changed = True
    while changed:
        changed = False
        clean = {e for e in live if not (covered_by.get(e, set()) & live) and not W.walks[e].errors}
        for e in sorted(live - clean):
            killers = covered_by.get(e, set()) & clean
            if killers:
                live.discard(e)
                W.rejected[e] = "trampoline bytes are code of $%04X (its own walk: %d decode errors)" % (
                    min(killers), len(W.walks[e].errors))
                changed = True
    W.routines = sorted(live)
    # decode errors of the accepted routines: dead opcodes / bad ext bytes, and off-module runs
    for e in W.routines:
        for kind, v in W.walks[e].errors:
            ins = W.walks[e].insns.get(v)
            if kind in ("dead", "badext"):
                W.dead_hits.append((e, v, ins.op if ins else None, ins.ext if ins else None))
            else:
                W.decode_errors.append((e, v, kind))
    # union + conflicts over the accepted routines
    owner = {}
    for e in W.routines:
        for v, ins in W.walks[e].insns.items():
            if ins.error:
                continue
            if v in W.insns:
                continue
            W.insns[v] = ins
            for b in range(v, v + ins.length):
                o = owner.get(b)
                if o is not None and o != v:
                    W.conflicts.append((b, o, v))
                else:
                    owner[b] = v
    sites = set()                                 # a call shared by two routines' walks counts once
    for e in W.routines:
        for ins in W.walks[e].calls:
            if ins.addr in sites:
                continue
            sites.add(ins.addr)
            t = ins.call_target
            if t is None:
                W.calls.append((ins.addr, e, "indirect", None, None, ins.op))
            else:
                cls, mname = space.classify_call(t)
                W.calls.append((ins.addr, e, cls, mname, t, ins.op))
    return W


def covered(W, space):
    """The module bytes the walk decoded: every accepted routine's trampoline (routine_body_offset
    bytes) and every reached instruction. (Not strings: listing.preview excludes them.)"""
    out = set()
    for e in W.routines:
        out.update(range(e, e + max(1, space.body_off)))
    for v, ins in W.insns.items():
        out.update(range(v, v + max(1, ins.length)))
    return out


def module_from_args(rom, P, a):
    """--module NAME, or the legacy --start BK:AD (ROM) window mapped back to its archive row."""
    if a.module:
        return archive.module(rom, P, a.module)
    sb, so = a.start.split(":")
    off = cpu_to_file(LOROM, int(sb, 16), int(so, 16))
    m = archive.module_at(rom, P, off)
    if m is None:
        raise SystemExit("--start %s is not inside a code module of the %s archive" % (a.start, P.get("title")))
    if a.base:
        vb, vo = a.base.split(":")
        want = int(vo, 16) - (off - m.rom_off)
        if want != m.base:
            raise SystemExit("--base %s disagrees with module %s's header base $%04X" % (a.base, m.name, m.base))
    return m


def fmt_stats(s):
    c = s["calls"]
    return ("%-9s $%04X-$%04X cand %4d rtn %4d rej %2d insns %6d bytes %6d conflicts %d dead-op %d "
            "errors %d | calls intra %d res %d cores %d syscall %d native %d unmapped %d ind %d" % (
                s["module"], s["base"], s["end"] - 1, s["candidates"], s["routines"], s["rejected"],
                s["instructions"], s["code_bytes"], s["conflicts"], s["dead_hits"], s["decode_errors"],
                c.get("intra", 0), c.get("resident", 0), c.get("coresident", 0), c.get("syscall", 0),
                c.get("native", 0), c.get("unmapped", 0), c.get("indirect", 0)))


def main(argv):
    ap = argparse.ArgumentParser(description="Recursive-descent KOEI SNES bytecode walker.")
    ap.add_argument("rom")
    ap.add_argument("--game", default=None, help="games.toml profile (else $KOEI_SNES_GAME, else the ROM header)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="walk every code module in the archive")
    g.add_argument("--module", default=None, help="archive name of one module")
    g.add_argument("--start", default=None, help="legacy: ROM BK:AD inside a module (with --base)")
    ap.add_argument("--base", default=None, help="legacy: WRAM 7E:XXXX of --start (checked)")
    ap.add_argument("--list", action="store_true", help="disassemble the reached instructions")
    ap.add_argument("--calls", action="store_true", help="list every direct call and its class")
    ap.add_argument("--syms", default=None, help="title data dir for name annotations (with --list)")
    a = ap.parse_args(argv[1:])
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    P = _profile.select(a.game, rom)
    isa = ISA.bind(rom, P)
    mods = archive.code_modules(rom, P) if a.all else [module_from_args(rom, P, a)]
    tot = Counter()
    for m in mods:
        W = walk_module(isa, Space(rom, P, m))
        s = W.stats()
        print(fmt_stats(s))
        for k in ("candidates", "routines", "rejected", "instructions", "code_bytes", "conflicts",
                  "dead_hits", "decode_errors"):
            tot[k] += s[k]
        for k, v in s["calls"].items():
            tot["calls_" + k] += v
        for e, why in sorted(W.rejected.items()):
            print("    rejected candidate $%04X: %s" % (e, why))
        for b, o, v in W.conflicts[:10]:
            print("    CONFLICT byte $%04X: decode at $%04X vs $%04X" % (b, o, v))
        for e, v, op, ext in W.dead_hits[:10]:
            print("    dead-op hit in $%04X at $%04X: op $%02X%s" % (e, v, op, (" ext $%02X" % ext) if ext is not None else ""))
        for e, v, kind in W.decode_errors[:10]:
            print("    decode error in $%04X at $%04X: %s" % (e, v, kind))
        if a.calls:
            for site, e, cls, mname, t, op in sorted(W.calls):
                print("    call $%04X (in $%04X) op $%02X -> %s %s" % (
                    site, e, op, ("$%04X" % t) if t is not None else "*regA", cls + (" " + mname if mname else "")))
        if a.list:
            from emu_harness.vm.koei.snes import listing
            from emu_harness.vm.koei.snes import vm_lift
            from emu_harness.vm.koei.snes.symbols import Symbols
            sp = Space(rom, P, m)
            L = listing.Lister(isa, Symbols(a.syms, profile=P), sp,
                               syscall_ids=vm_lift.module_syscall_ids(sp, W), code=covered(W, sp))
            print("\n== reachable disassembly: %s ==" % m.name)
            for v in sorted(W.insns):
                for line in L.lines(W.insns[v]):
                    print(line)
    if len(mods) > 1:
        print("TOTAL %d modules: candidates %d routines %d rejected %d instructions %d code bytes %d "
              "conflicts %d dead-op hits %d decode errors %d | calls intra %d resident %d coresident %d "
              "syscall %d native %d unmapped %d indirect %d" % (
                  len(mods), tot["candidates"], tot["routines"], tot["rejected"], tot["instructions"],
                  tot["code_bytes"], tot["conflicts"], tot["dead_hits"], tot["decode_errors"],
                  tot["calls_intra"], tot["calls_resident"], tot["calls_coresident"], tot["calls_syscall"],
                  tot["calls_native"], tot["calls_unmapped"], tot["calls_indirect"]))
    return 0


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
