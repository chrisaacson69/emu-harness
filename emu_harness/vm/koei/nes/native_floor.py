#!/usr/bin/env python3
"""
native_floor.py - the NATIVE-6502 floor of a KOEI NES title's fixed bank: enumerate every native
sub in $C000-$FFFF and propose canonical names by matching an already-named ORACLE title.

One profile-driven tool replacing seven per-title copies (gk, na2, rot3k2, bk, GemFire, LEmp, UW;
emu-harness MIGRATION.md row D). The algorithm is the last copy's (UW), which the chain improved:

  1. Seeds = NMI/RESET/IRQ vectors (+ the private syscall vector), the 256 VM opcode handlers, the
     VM entry/dispatcher (+ ext-op dispatcher), and, when the profile gives their counts, the
     syscall-table handlers (+ dispatcher, trampolines) and ext-op table handlers, plus any extra
     seeds (bytecode-only leaves, paged-native callers) the profile lists.
  2. Reach: recursive descent from the seeds UNION a flat JSR census of the bank.
  3. Filters: drop bytecode stubs; JUNK (an illegal opcode before any rts/rti/jmp); PHANTOM (a
     "sub" that starts on another sub's operand byte, e.g. the $20 inside `sta $2005`).
  4. Name: relocation-invariant signature vs the oracle's named native subs. >=.90 inherit
     (relocated-identical), .70-.90 inherit + verify, <.70 placeholder `native_XXXX` for a read.

Profile: games.toml [<title>] (vm_entry_stub, handler_lo/hi, vm_entry, dispatcher, syscall_*,
extop_*) plus a [<title>.native_floor] table: oracle, syscall_slots, extop_slots, syscall_tramps,
extra_seeds. A seed group without its count stays off.

Moved from koei-nes tools/native_floor.py (MIGRATION.md row E), which is now an alias.

Usage:
  py -3.14 -m emu_harness.vm.koei.nes.native_floor <title>            # ranked match table + summary
  py -3.14 -m emu_harness.vm.koei.nes.native_floor <title> --toml     # the proposed native label block
  py -3.14 -m emu_harness.vm.koei.nes.native_floor <title> --check    # proposals vs the reviewed registry
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

from emu_harness.core.symbols import format_entry, names_by_address, read_registry
from emu_harness.cpu.m6502.descent import Window, descend, flow
from emu_harness.cpu.m6502.disasm import MODE_LEN, OPS
from emu_harness.cpu.m6502.signature import best_match, signature
from emu_harness.machines.nes.memory import fixed_bank
from emu_harness.vm.koei.nes.titles import game_cfg            # the title profiles

BASE, SIZE = 0xC000, 0x4000
SHARED, NEAR = 0.90, 0.70


def stub_bytes(cfg):
    return bytes(int(b, 16) for b in cfg["vm_entry_stub"].split())


def word(buf, addr):
    return buf[addr - BASE] | buf[addr - BASE + 1] << 8


class Title:
    def __init__(self, name):
        self.name = name
        self.cfg = game_cfg(name)
        self.nf = self.cfg.get("native_floor", {})
        self.buf = fixed_bank(Path(self.cfg["_rom"]).read_bytes())
        self.stub = stub_bytes(self.cfg)

    def is_stub(self, a):
        o = a - BASE
        return 0 <= o <= SIZE - 3 and self.buf[o:o + 3] == self.stub

    def in_bank(self, a):
        return BASE <= a <= 0xFFFF


def seed_groups(t):
    """The seeds by where they come from: {"vector": .., "vm_handler": .., "vm_core": .., "extop": ..,
    "syscall": .., "extra": ..}. A call graph reads them as roots (reached from outside any routine)."""
    c, nf, buf = t.cfg, t.nf, t.buf
    g = {"vector": {word(buf, 0xFFFA), word(buf, 0xFFFC), word(buf, 0xFFFE)}}
    if c.get("syscall_vector"):
        g["vector"].add(word(buf, c["syscall_vector"]))
    lo, hi = c["handler_lo"] - BASE, c["handler_hi"] - BASE
    g["vm_handler"] = {buf[lo + i] | buf[hi + i] << 8 for i in range(256)}
    g["vm_core"] = {c["vm_entry"], c["dispatcher"]}
    if c.get("extop_dispatch"):
        g["vm_core"].add(c["extop_dispatch"])
    if nf.get("syscall_slots"):
        table, n = c["syscall_table"], nf["syscall_slots"]
        if c.get("syscall_kind") == "jsr3":                  # 3-byte `JMP abs` entries
            g["syscall"] = {word(buf, table + 3 * k + 1) for k in range(n)}
        else:                                                # raw 2-byte addresses
            g["syscall"] = {a for a in (word(buf, table + 2 * k) for k in range(n)) if t.in_bank(a)}
        if c.get("syscall_dispatch"):
            g["syscall"].add(c["syscall_dispatch"])
        g["syscall"] |= set(nf.get("syscall_tramps", []))
    if nf.get("extop_slots"):
        table = nf.get("extop_table", c.get("extop_table"))
        g["extop"] = {a for a in (word(buf, table + 2 * k) for k in range(nf["extop_slots"])) if t.in_bank(a)}
    g["extra"] = set(nf.get("extra_seeds", []))
    return g


def seeds(t):
    return set().union(*seed_groups(t).values())


def jsr_census(buf):
    c = Counter()
    for i in range(SIZE - 2):
        if buf[i] == 0x20:
            tgt = buf[i + 1] | buf[i + 2] << 8
            if tgt >= BASE:
                c[tgt] += 1
    return c


def reach(t, start):
    """Seeds (minus bytecode stubs) + every fixed-bank JSR target reached from them; a JSR or JMP
    into a bytecode stub is not followed."""
    follow = lambda a: not t.is_stub(a)
    return descend(Window(t.buf, BASE), {a for a in start if follow(a)}, follow=follow).entries


def is_code(buf, a):
    """JUNK filter: an illegal opcode before any rts/rti/jmp means data, not a routine."""
    i, n = a - BASE, 0
    while 0 <= i < SIZE and n < 24:
        op = buf[i]
        if op not in OPS:
            return False
        if op in (0x60, 0x40, 0x4C, 0x6C):
            return True
        i += 1 + MODE_LEN[OPS[op][1]]
        n += 1
    return True


def decode_flow(buf, a, nxt):
    """-> (instruction starts, operand bytes) along a sub's control flow: fall-through and both
    branch targets, each path ending at rts/rti/jmp, an illegal opcode or the next sub."""
    f = flow(Window(buf, BASE), a, end=nxt, into_jmp=False)
    return f.starts, f.operands


def drop_phantoms(buf, subs, trusted):
    """PHANTOM filter: a JSR-census hit on an operand byte (the $20 in `sta $2005`) is not a sub.

    A candidate starting on another sub's operand byte is dropped, unless it is `trusted` (read
    from a vector or dispatch table) or some JSR to it is itself a real instruction of a kept
    sub. Overlapped entries are a real 6502 trick: the VM handler table points at $E960 in na2
    and rot3k2, the second byte of a `sta $08`. Decoding follows control flow; the copies before
    this swept linearly past the rts into data tables and lost real entries after them (na2
    write_ppuctrl $F94F, rot3k2 umul32 $EF93). MIGRATION.md row D."""
    ss = sorted(subs)
    flow = {a: decode_flow(buf, a, ss[k + 1] if k + 1 < len(ss) else 0x10000) for k, a in enumerate(ss)}
    interior = set().union(*(f[1] for f in flow.values())) if flow else set()
    jsr_sites = {}
    for i in range(SIZE - 2):
        if buf[i] == 0x20:
            jsr_sites.setdefault(buf[i + 1] | buf[i + 2] << 8, []).append(BASE + i)
    kept = {a for a in subs if a not in interior or a in trusted}
    while True:                                   # only kept subs vouch for others, to a fixpoint
        real = set().union(*(flow[a][0] for a in kept)) if kept else set()
        more = {a for a in subs - kept if any(s in real for s in jsr_sites.get(a, ()))}
        if not more:
            return kept
        kept |= more


def handler_helpers():
    """ro3k tools/handler_match's three shared helpers as `H.bank15 / H.load_names / H.signature`,
    for the tools that path-hacked into ro3k to import them (UW, rot3k2; MIGRATION.md row E)."""
    from types import SimpleNamespace
    return SimpleNamespace(bank15=lambda rom_path: fixed_bank(Path(rom_path).read_bytes()),
                           load_names=lambda toml_path: names_by_address(read_registry(toml_path)),
                           signature=signature)


def native_subs(t):
    census = jsr_census(t.buf)
    trusted = {a for a in seeds(t) if t.in_bank(a)}
    found = reach(t, trusted | {a for a in census if not t.is_stub(a)})
    found |= {a for a in census if t.in_bank(a) and not t.is_stub(a)}
    found = {a for a in found if not t.is_stub(a) and is_code(t.buf, a)}
    return sorted(drop_phantoms(t.buf, found, trusted)), census


def oracle_sigs(o):
    names = names_by_address(read_registry(o.cfg["_labels"]))
    sigs = {}
    for a in names:
        if o.in_bank(a) and not o.is_stub(a):
            s = signature(o.buf, a)
            if len(s) >= 3:
                sigs[a] = s
    return names, sigs


def analyze(title):
    t = Title(title)
    o = Title(t.nf["oracle"])
    subs, census = native_subs(t)
    onames, osigs = oracle_sigs(o)
    rows = []
    for a in subs:
        sig = signature(t.buf, a)
        if len(sig) < 3:                       # too short to fingerprint (a jmp thunk)
            rows.append((a, len(sig), None, 0.0, census.get(a, 0)))
            continue
        oa, sc = best_match(sig, osigs)
        rows.append((a, len(sig), oa, sc, census.get(a, 0)))
    return t, o, rows, onames


def proposals(t, o, rows, onames):
    """-> [(addr, name, comment)] for every fingerprintable sub, in address order."""
    ORA, TTL, seen, out = o.name.upper(), t.name.upper(), set(), []
    for a, ln, oa, sc, callers in sorted(rows):
        if ln < 3:
            continue
        nm = onames.get(oa) if oa else None
        if sc >= NEAR and nm:
            key = nm if nm not in seen else f"{nm}_{a:04x}"
            seen.add(nm)
            c = (f"== {ORA} {nm} (${oa:04X}) score {sc:.2f} relocated-identical; {callers} callers" if sc >= SHARED
                 else f"~ {ORA} {nm} (${oa:04X}) score {sc:.2f} diverged (reloc); same function -- verify; {callers} callers")
        else:
            key = f"native_{a:04x}"
            c = f"{TTL}-specific native (best {ORA} {nm or '-'} {sc:.2f}); {callers} callers -- READ before naming"
        out.append((a, key, c))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("title", help="games.toml profile name, e.g. gk, na2, uw")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--toml", action="store_true", help="print the proposed native label block")
    g.add_argument("--check", action="store_true", help="compare proposals with the title's registry")
    a = ap.parse_args(argv)
    t, o, rows, onames = analyze(a.title)

    if a.toml:
        print(f"# --- {t.name.upper()} fixed-bank NATIVE floor (GENERATED by emu-harness vm/koei/nes/native_floor.py; "
              f"oracle = {o.name.upper()}) ---")
        print(f"# >={SHARED:.2f} inherit the oracle's name; {NEAR:.2f}-{SHARED:.2f} diverged (verify); "
              f"<{NEAR:.2f} title-specific (read before naming).")
        for addr, key, c in proposals(t, o, rows, onames):
            print(format_entry(f"0x{addr:04X}", key, c))
        return 0

    if a.check:
        reg = names_by_address(read_registry(t.cfg["_labels"]))
        agree = differ = unnamed = 0
        for addr, key, c in proposals(t, o, rows, onames):
            have = reg.get(addr)
            if key.startswith("native_"):
                continue
            if have is None:
                unnamed += 1
                print(f"  NOT IN REGISTRY ${addr:04X} proposed {key}")
            elif have == key or have.startswith(key + "_") or key.startswith(have):
                agree += 1
            else:
                differ += 1
                print(f"  DIFFERS ${addr:04X} registry {have} | proposed {key}  ({c[:60]})")
        print(f"{t.name}: inherited proposals agreeing with the registry {agree}, differing {differ}, "
              f"not in registry {unnamed}")
        return 0

    print(f"{'addr':>6} {'len':>4} {'callers':>7} {o.name.upper():>6} {'score':>6}  verdict / inherited name")
    print("-" * 96)
    shared = near = spec = 0
    for addr, ln, oa, sc, callers in sorted(rows, key=lambda r: -r[3]):
        nm = onames.get(oa, f"sub_{oa:04x}") if oa else "-"
        if sc >= SHARED:
            tag, shared = "==", shared + 1
        elif sc >= NEAR:
            tag, near = "~ ", near + 1
        else:
            tag, spec = "!!", spec + 1
        print(f"${addr:04x} {ln:>4} {callers:>7} {f'${oa:04x}' if oa else '  -  ':>6} {sc:>6.2f}  {tag} {nm}")
    print("-" * 96)
    thunks = sum(1 for _, ln, *_ in rows if ln < 3)
    print(f"{t.name.upper()} fixed-bank native subs: {len(rows)} total "
          f"({sum(1 for *_, c in rows if c > 0)} JSR-reached + seeds)")
    print(f"  SHARED w/ {o.name.upper()} (>={SHARED:.2f}): {shared}   NEAR: {near}")
    print(f"  TITLE-SPECIFIC (<{NEAR:.2f}): {spec - thunks} real  +  {thunks} thunks/<3-op")
    print(f"  => named canonically: {shared + near} / {len(rows) - thunks} fingerprintable native subs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
