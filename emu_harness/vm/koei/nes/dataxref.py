#!/usr/bin/env python3
"""
Who reads, writes or points at a data address, across a KOEI NES title's bytecode (row K of
MIGRATION.md). The data-side twin of the call graph: name a routine by behaviour, then ask "who
touches X?" to recover a data item's role.

Read from the DECODED bytecode of every bank (callgraph.TitleSpace), each operand classified by the
one opcode table's lift template, not by a mnemonic or a hex literal in the C:

  read / write / addr   an a16 operand: a store to {o} (`{o} = A`, `memb[&{o}] = A`) is a write,
                        `&{o}` outside a memory access is address-of, any other use of {o} a read
  ptr                   an i16 constant that is only MOVED or PUSHED (`A = {o}`, `C = {o}`, `push {o}`):
                        a pointer / table base candidate. An arithmetic constant (`A = A + {o}`),
                        `sp_adj`, `blkcopy`'s length and the bitfield specs are not pointers.

RAM / SRAM (< $8000) is one space for every bank. ROM is keyed by its owning bank, through the
window model: the referencing bank's own window, a pinned bank, else the single co-resident bank
whose window covers it -- anything else is reported, not guessed.

CAVEAT (from na1 data-xref): a word stored through a frame-relative or indirect opcode carries no
static address, so WRITE is a lower bound -- "0 writers" is not "never written".

    py -3.14 -m emu_harness.vm.koei.nes.dataxref <title> refs <addr|name>
    py -3.14 -m emu_harness.vm.koei.nes.dataxref <title> hot [--min N] [--unlabeled] [--lo HEX --hi HEX] [--rom]
    py -3.14 -m emu_harness.vm.koei.nes.dataxref <title> frontier [--unlabeled] [--class str|tbl|code] [--bank N] [--min N]
    py -3.14 -m emu_harness.vm.koei.nes.dataxref <title> ptr-refs <addr> [--bank N]
    py -3.14 -m emu_harness.vm.koei.nes.dataxref <title> strings | tables [--toml] [--bank N]   # proposed registry lines
    py -3.14 -m emu_harness.vm.koei.nes.dataxref <title> stats | ram [--min N]
"""
import argparse
import re
import sys
from collections import defaultdict

from emu_harness.core.symbols import read_registry
from emu_harness.vm.koei.isa import TABLE
from emu_harness.vm.koei.match import _fields
from emu_harness.vm.koei.nes.callgraph import TitleSpace
from emu_harness.vm.koei.nes.nobunaga_vm import load_labels

_LV = re.compile(r"^(\{o\}|memb\[&\{o\}\]|mem32\[&\{o\}\])$")
_PTR_TEMPLATES = {"A = {o}", "C = {o}", "push {o}"}


def kind_of(op):
    """The data kind an op's address / constant operand has, from its lift template, or None."""
    c = op.c or ""
    types = op.field_types()
    if op.cf != "none":
        return None                                    # control transfers are the call graph's
    if "a16" in types:
        for st in c.split(";"):
            lhs, eq, _rhs = st.strip().partition(" = ")
            if eq and _LV.match(lhs.strip()):
                return "write"
        if re.search(r"(?<!\[)&\{o\}", c):
            return "addr"
        return "read" if "{o}" in c else None
    if "i16" in types and c.strip() in _PTR_TEMPLATES:
        return "ptr"
    return None


class Index:
    def __init__(self, title):
        self.sp = sp = TitleSpace(title)
        self.labels = load_labels(sp.cfg["_labels"])
        self.prg_bytes = sp.cfg["_rom"].read_bytes()[16:]
        self.rom_names = {}                            # (owning bank, addr) -> name
        for e in read_registry(sp.cfg["_labels"]).entries:
            m = re.match(r"prg\.bank(\d+)$", e.section)
            if m and e.addr >= 0x8000:
                b = next((c for c in sorted(sp.pinned) if sp.covers(c, e.addr)), int(m.group(1)))
                self.rom_names.setdefault((b, e.addr), e.name)
        self.ram = defaultdict(list)                   # addr -> [ref]
        self.rom = defaultdict(list)                   # (bank, addr) -> [ref]
        self.unowned = []                              # (ref, why): a ROM ref with no single owning bank
        for b in sp.views:
            for stub, instrs in sp.subs(b):
                for ins in instrs:
                    raw = ins["bytes"]
                    ext = raw[0] == 0xB7 and len(raw) > 1
                    op = TABLE.op(raw[1], ext=True) if ext else TABLE.op(raw[0])
                    if op is None:
                        continue
                    kind = kind_of(op)
                    if kind is None:
                        continue
                    vals = []
                    try:
                        _fields(op.fields, raw, 2 if ext else 1, vals, {})
                    except (IndexError, KeyError):
                        continue
                    addr = next((v & 0xFFFF for _r, t, v in vals if t in ("a16", "i16")), None)
                    if addr is None:
                        continue
                    ref = dict(bank=b, sub=stub, ins=ins["addr"], opc=raw[0] if not ext else 0xB700 | raw[1],
                               kind=kind, mnem=op.name, cpu=addr)
                    if addr < 0x8000:
                        self.ram[addr].append(ref)
                        continue
                    own, why = sp.owner(b, addr, entry=False)
                    if own is None:
                        self.unowned.append((ref, why))
                    else:
                        self.rom[own].append(ref)

    def name_of(self, addr):
        lab = self.labels.get(addr)
        return lab[0] if lab else None

    def sub_name(self, sub):
        if sub is None:
            return "(top-level)"
        name = self.name_of(sub)
        return f"{name} (${sub:04X})" if name else f"sub_${sub:04X}"

    def prg(self, bank, addr):
        return self.sp.win.prg(bank, addr)

    def rom_name(self, addr, bank):
        """The registry name of ROM byte (bank, addr), bank-unambiguous; None for RAM or unlabeled."""
        if bank is None or addr < 0x8000:
            return None
        return self.rom_names.get((bank, addr))


# ---- views (na1 data-xref's formats) --------------------------------------------------------
def _kinds(rs):
    c = lambda k: sum(r["kind"] == k for r in rs)
    return f"{c('read')}R {c('write')}W {c('addr')}& {c('ptr')}P"


def _print_grouped(X, rs, header):
    print(header)
    by_sub = defaultdict(list)
    for r in rs:
        by_sub[(r["bank"], r["sub"])].append(r)
    for (bank, sub) in sorted(by_sub, key=lambda k: (k[0], k[1] is None, k[1])):
        sites = by_sub[(bank, sub)]
        tags = ", ".join(f"{r['kind']}@${r['ins']:04X}" for r in sorted(sites, key=lambda r: r["ins"]))
        print(f"  bank{bank:02d}  {X.sub_name(sub)}: {tags}")
    if not rs:
        print("  (no static refs; may be ZP / frame-relative / indirect only)")


def cmd_refs(X, query):
    q = query.strip()
    try:
        addr = int(q.lstrip("$").replace("0x", "").replace("0X", ""), 16)
    except ValueError:
        hit = [a for a, v in X.labels.items() if v[0] == q]
        if not hit:
            print(f"// unknown label: {q}")
            return
        addr = hit[0]
    nm = X.name_of(addr)
    if addr < 0x8000:
        rs = X.ram.get(addr, [])
        _print_grouped(X, rs, f"// ${addr:04X}  {nm or '(unlabeled)'}   {len(rs)} refs  ({_kinds(rs)})")
        return
    hits = sorted((k, rs) for k, rs in X.rom.items() if k[1] == addr)
    pinned = next((c for c in sorted(X.sp.pinned) if X.sp.covers(c, addr)), None)
    if not hits and pinned is not None:
        hits = [((pinned, addr), [])]                  # a fixed-bank address: report it there, 0 refs
    if not hits:
        print(f"// ${addr:04X}  (ROM) — no pointer refs in any bank")
        return
    for (bank, _a), rs in hits:
        where = f"  (bank{bank:02d})" if bank in X.sp.pinned else f" @ bank{bank:02d}"
        _print_grouped(X, rs, f"// ${addr:04X}{where}  {nm or '(unlabeled)'}   {len(rs)} refs  ({_kinds(rs)})")


def cmd_hot_ram(X, min_n, unlabeled, lo, hi):
    rows = []
    for addr, rs in X.ram.items():
        if not (lo <= addr <= hi) or len(rs) < min_n:
            continue
        name = X.name_of(addr)
        if unlabeled and name:
            continue
        c = lambda k: sum(r["kind"] == k for r in rs)
        rows.append((len(rs), addr, c('read'), c('write'), c('addr'), c('ptr'), len({r["sub"] for r in rs}), name))
    rows.sort(key=lambda t: (-t[0], t[1]))
    print(f"// {len(rows)} addresses  (window ${lo:04X}-${hi:04X}, min {min_n} refs"
          f"{', unlabeled only' if unlabeled else ''})")
    print(f"// {'addr':>6}  {'refs':>4} {'R':>3} {'W':>3} {'&':>3} {'P':>3} {'subs':>4}  label")
    for n, addr, nr, nw, na, npp, nsubs, name in rows:
        print(f"   ${addr:04X}  {n:>4} {nr:>3} {nw:>3} {na:>3} {npp:>3} {nsubs:>4}  {name or '-'}")


def cmd_hot_rom(X, min_n, unlabeled):
    rows = []
    for (bank, cpu), rs in X.rom.items():
        if len(rs) < min_n:
            continue
        name = X.name_of(cpu)
        if unlabeled and name:
            continue
        rows.append((len(rs), bank, cpu, len({r["sub"] for r in rs}), name))
    rows.sort(key=lambda t: (-t[0], t[1], t[2]))
    print(f"// {len(rows)} ROM data assets  (min {min_n} pointer-refs{', unlabeled only' if unlabeled else ''})")
    print(f"// {'bank':>4} {'addr':>6}  {'refs':>4} {'subs':>4}  label")
    for n, bank, cpu, nsubs, name in rows:
        print(f"   b{bank:02d}   ${cpu:04X}  {n:>4} {nsubs:>4}  {name or '-'}")
    if X.unowned:
        print(f"// {len(X.unowned)} ROM refs with no single owning bank (e.g. "
              + ", ".join(f"${r['cpu']:04X} from b{r['bank']:02d}" for r, _w in X.unowned[:4]) + ")")


# ---- ROM pointer views (na1 rom-xref's, verbatim but for the three inputs) -----------------
# rom-xref read pointers from the committed listing text and decoded each in the bank of its
# REFERENCING sub. Here the pointers are the decoded `ptr` refs (the 8A / 8C / 8E family, the same
# three ops), each decoded in its OWNING bank (callgraph.TitleSpace.owner), bytes are read through
# the window model, and names are bank-unambiguous (a fixed-window label belongs to its pinned bank
# whatever section it is filed under, as rom-xref's load_labels_by_prg normalized).
def rom_index(X):
    """-> (idx, code_addrs): idx[addr][bank] = [{sub, ins, mnem}] for every ROM pointer;
    code_addrs[bank] = the decoded instruction addresses (a pointer onto one is a CODE pointer)."""
    idx = defaultdict(lambda: defaultdict(list))
    for (bank, addr), rs in X.rom.items():
        for r in rs:
            if r["kind"] == "ptr":
                idx[addr][bank].append({"sub": r["sub"], "sb": r["bank"], "ins": r["ins"],
                                        "mnem": IMM_WORD_OPS.get(r["opc"], r["mnem"])})
    code_addrs = defaultdict(set)
    for b in X.sp.views:
        for _stub, instrs in X.sp.subs(b):
            code_addrs[b] |= {ins["addr"] for ins in instrs}
    return idx, code_addrs


IMM_WORD_OPS = {0x8A: "loadA", 0x8C: "loadB", 0x8E: "push"}


def decode_str(X, addr, bank, maxn=64):
    """Bytes at (addr,bank) until NUL -> (text, raw_len, printable_ratio)."""
    o = X.prg(bank, addr)
    rom = X.prg_bytes
    raw = []
    for i in range(maxn):
        if o + i >= len(rom):
            break
        b = rom[o + i]
        if b == 0:
            break
        raw.append(b)
    if not raw:
        return "", 0, 0.0
    printable = sum(1 for b in raw if 32 <= b < 127 or b in (0x0a, 0x0d))
    text = "".join(chr(b) if 32 <= b < 127 else ("\\n" if b == 0x0a else
            "\\r" if b == 0x0d else "[%02x]" % b) for b in raw)
    return text, len(raw), printable / len(raw)


def classify(X, addr, bank, code_addrs=None):
    if X.rom_name(addr, bank):
        return "labeled"
    # code-pointer: the immediate lands ON a decoded instruction boundary in this
    # bank => it's a callback / continuation address pushed as an arg, not data.
    if code_addrs is not None and addr in code_addrs.get(bank, ()):
        return "code"
    text, n, ratio = decode_str(X, addr, bank)
    o = X.prg(bank, addr)
    raw = X.prg_bytes[o:o + n]
    # string: NUL-terminated, >=2 bytes, high printable ratio, starts printable, and
    # not a single repeated fill byte (e.g. 0x55 'UUUU' PPU fill).
    if (n >= 2 and ratio >= 0.85 and not text.startswith("[")
            and len(set(raw)) > 1):
        return "str"
    return "tbl"


def _sub_name_rom(X, sub, bank):
    """A referencing sub's name, looked up in the sub's OWN bank (not the pointer's)."""
    if sub is None:
        return "(top-level)"
    n = X.rom_name(sub, bank)
    return f"{n} (${sub:04X})" if n else f"sub_${sub:04X}"


def _slug(text, maxlen=30):
    """Readable label stem from decoded text. Strips control/cursor prefixes (the
    `>[16]80` tile codes that prefix many strings) by anchoring on the first letter,
    so all entry-point variants of one message converge to the same stem."""
    s = re.sub(r'\[[0-9a-f]{2}\]', '', text)         # drop raw hex escapes
    s = s.replace('\\n', ' ').replace('\\r', ' ')
    m = re.search(r'[A-Za-z].*', s)                   # anchor on first letter
    core = m.group(0) if m else s
    slug = re.sub(r'[^A-Za-z0-9]+', '_', core).strip('_').lower()[:maxlen].rstrip('_')
    if len(re.sub(r'[^a-z]', '', slug)) >= 2:         # enough letters to be meaningful
        return slug
    # letterless (pure format/punct string, e.g. "%2d", " "): name by its specifiers
    fmt = "_".join(re.findall(r'%[0-9]*[dscx]', text)) or "blank"
    return ("fmt_" + re.sub(r'[^A-Za-z0-9]+', '_', fmt)).strip('_').lower()


def iter_pointers(idx, bank_filter=None):
    for addr in sorted(idx):
        for bank in sorted(idx[addr]):
            if bank_filter is not None and bank != bank_filter:
                continue
            yield addr, bank, idx[addr][bank]


def cmd_frontier(X, idx, code_addrs, unlabeled, klass, bank_filter, min_n):
    rows = []
    for addr, bank, refs in iter_pointers(idx, bank_filter):
        if len(refs) < min_n:
            continue
        name = X.rom_name(addr, bank)
        if unlabeled and name:
            continue
        c = classify(X, addr, bank, code_addrs)
        if klass and c != klass:
            continue
        text, n, ratio = decode_str(X, addr, bank)
        subs = sorted({(r["sb"], r["sub"]) for r in refs}, key=lambda s: (s[1] is None, s[1], s[0]))
        rows.append((len(refs), addr, bank, c, name, subs, text[:42]))
    rows.sort(key=lambda t: (-t[0], t[1], t[2]))
    print(f"// {len(rows)} ROM pointers  (min {min_n} refs"
          f"{', unlabeled' if unlabeled else ''}{', class='+klass if klass else ''}"
          f"{', bank %d'%bank_filter if bank_filter is not None else ''})")
    print(f"// {'addr':>6} bk {'n':>2} {'class':>7}  subs / preview")
    for n_refs, addr, bank, c, name, subs, prev in rows:
        sub0 = _sub_name_rom(X, subs[0][1], subs[0][0]) + ("  +%d" % (len(subs)-1) if len(subs) > 1 else "")
        tag = name or (f'"{prev}"' if c == "str" else prev)
        print(f"   ${addr:04X} {bank:>2} {n_refs:>2} {c:>7}  {sub0}  | {tag}")


def cmd_ptr_refs(X, addr, idx, code_addrs, bank_filter):
    if addr not in idx:
        print(f"// ${addr:04X}: no imm-word ROM references")
        return
    for bank in sorted(idx[addr]):
        if bank_filter is not None and bank != bank_filter:
            continue
        refs = idx[addr][bank]
        c = classify(X, addr, bank, code_addrs)
        text, n, ratio = decode_str(X, addr, bank)
        print(f"// ${addr:04X}  bank{bank}  {len(refs)} refs  class={c}  "
              f"printable={ratio:.0%}  name={X.rom_name(addr, bank) or '-'}")
        if c == "str":
            print(f"//   text: \"{text}\"")
        else:
            o = X.prg(bank, addr)
            print(f"//   bytes: " + " ".join("%02x" % b for b in X.prg_bytes[o:o+16]))
        by_sub = defaultdict(list)
        for r in refs:
            by_sub[(r["sb"], r["sub"])].append(r)
        for sb, sub in sorted(by_sub, key=lambda s: (s[1] is None, s[1], s[0])):
            sites = ", ".join(f"{r['mnem']}@${r['ins']:04X}" for r in by_sub[(sb, sub)])
            print(f"     {_sub_name_rom(X, sub, sb)}: {sites}")


def cmd_strings(X, idx, code_addrs, as_toml, bank_filter):
    by_bank = defaultdict(list)
    for addr, bank, refs in iter_pointers(idx, bank_filter):
        if X.rom_name(addr, bank):
            continue
        if classify(X, addr, bank, code_addrs) != "str":
            continue
        text, n, ratio = decode_str(X, addr, bank)
        by_bank[bank].append((addr, text, refs))
    total = sum(len(v) for v in by_bank.values())
    if not as_toml:
        for bank in sorted(by_bank):
            print(f"# --- bank {bank} ({len(by_bank[bank])} strings) ---")
            for addr, text, refs in sorted(by_bank[bank]):
                print(f'  ${addr:04X}  ({len(refs)}x)  "{text}"')
        print(f"# total {total} string pointers")
        return
    # toml emission, per-bank section. Names are globally unique (load_labels is flat,
    # so a name reused across banks would shadow); on collision append the address.
    seen = set()
    for bank in sorted(by_bank):
        print(f"\n[prg.bank{bank}]   # ROM strings (rom-xref auto-decode)")
        for addr, text, refs in sorted(by_bank[bank]):
            nm = "msg_" + _slug(text)
            if nm in seen:
                nm = f"{nm}_{addr:04x}"
            seen.add(nm)
            ctext = text.replace('"', "'")
            print(f'"0x{addr:04X}" = {{ name = "{nm}", comment = "[ROM-STR] \\"{ctext}\\"" }}')


def is_ptr_table(X, addr, bank, n=8):
    """True only if the first n words read as a CLEAN table of code pointers — the
    first word is in the switchable/fixed code window AND >=0.85 of words are
    $8000-$EFFF (excludes $Fxxx vectors/$FFFA and obvious non-pointers). Conservative
    on purpose: a borderline table keeps the neutral `_data_` name rather than falsely
    asserting `jumptab`."""
    o = X.prg(bank, addr)
    rom = X.prg_bytes
    ws = [rom[o + 2 * i] | (rom[o + 2 * i + 1] << 8)
          for i in range(n) if o + 2 * i + 1 < len(rom)]
    if not ws or not (0x8000 <= ws[0] <= 0xEFFF):
        return False
    return sum(0x8000 <= w <= 0xEFFF for w in ws) >= len(ws) * 0.85


def _table_name(addr, primary_subname, ptr):
    if ptr:
        return f"jumptab_{addr:04x}"          # content-verified dispatch table
    base = re.sub(r'_[0-9a-f]{4}$', '', primary_subname or "")[:24].strip('_')
    return f"{base}_data_{addr:04x}" if base else f"rodata_{addr:04x}"


def cmd_tables(X, idx, code_addrs, as_toml, bank_filter, defer_subs=()):
    """Name the non-string ROM tables from PROVENANCE (the named sub that reads them)
    + content-verified type (jump table vs binary blob). Honest by construction: the
    name asserts only what's grounded (who reads it / it's a pointer table); the bucket
    heuristic + byte preview live in the comment. Tables read only by subs whose names
    contain a `defer_subs` pattern are left for a focused session (na1: combat geometry)."""
    by_bank = defaultdict(list)
    deferred = 0
    for addr, bank, refs in iter_pointers(idx, bank_filter):
        if X.rom_name(addr, bank):
            continue
        if classify(X, addr, bank, code_addrs) != "tbl":
            continue
        subnames = [(X.rom_name(r["sub"], r["sb"]) or "") for r in refs if r["sub"] is not None]
        ptr = is_ptr_table(X, addr, bank)
        if defer_subs and not ptr and subnames and all(any(d in sn for d in defer_subs) for sn in subnames):
            deferred += 1
            continue
        by_bank[bank].append((addr, refs, subnames, ptr))
    total = sum(len(v) for v in by_bank.values())
    if not as_toml:
        for bank in sorted(by_bank):
            print(f"# --- bank {bank} ({len(by_bank[bank])} tables) ---")
            for addr, refs, subnames, ptr in sorted(by_bank[bank]):
                kind = "jumptab" if ptr else "data"
                print(f"  ${addr:04X}  {kind:8} ({len(refs)}x)  <- {subnames[0] if subnames else '(top)'}")
        print(f"# total {total} tables to name; {deferred} combat-geometry tables deferred")
        return
    seen = set()
    for bank in sorted(by_bank):
        print(f"\n[prg.bank{bank}]   # ROM tables (rom-xref provenance-named, B1 data-walk)")
        for addr, refs, subnames, ptr in sorted(by_bank[bank]):
            nm = _table_name(addr, subnames[0] if subnames else "", ptr)
            if nm in seen:
                nm = f"{nm}_{addr:04x}"
            seen.add(nm)
            o = X.prg(bank, addr)
            preview = " ".join("%02x" % b for b in X.prg_bytes[o:o + 12])
            who = ", ".join(sorted({s for s in subnames if s})) or "(top-level)"
            kind = "pointer/dispatch table" if ptr else "binary table (unanalyzed)"
            print(f'"0x{addr:04X}" = {{ name = "{nm}", '
                  f'comment = "[ROM-DATA] {kind}; read by {who} via imm-word ptr; bytes: {preview}" }}')


def cmd_stats(X, idx, code_addrs):
    cls = defaultdict(int); reg = defaultdict(lambda: defaultdict(int))
    for addr, bank, refs in iter_pointers(idx):
        c = classify(X, addr, bank, code_addrs)
        cls[c] += 1
        reg[f"${addr>>8:02X}xx"][c] += 1
    print("// class totals:", dict(cls))
    print("// by region (region: {class:count}):")
    for r in sorted(reg):
        print(f"   {r}: {dict(reg[r])}")


def cmd_ram_subs(X, min_n):
    known = set(X.labels)
    rows = []
    for a, rs in X.ram.items():
        if a in known:
            continue
        subs = {X.name_of(r["sub"]) or f"sub_{r['sub']:04X}" for r in rs}
        if len(subs) >= min_n:
            rows.append((a, subs))
    for a, subs in sorted(rows, key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"  ${a:04X}  refs={len(subs):2d}  {', '.join(sorted(subs)[:6])}")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("title")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("refs"); p.add_argument("addr")
    p = sub.add_parser("hot")
    p.add_argument("--min", type=int, default=3)
    p.add_argument("--unlabeled", action="store_true")
    p.add_argument("--rom", action="store_true")
    p.add_argument("--lo", default="0x6000")
    p.add_argument("--hi", default="0x7FFF")
    p = sub.add_parser("frontier", help="ranked ROM pointers: addr / bank / refs / class / subs / preview")
    p.add_argument("--unlabeled", action="store_true")
    p.add_argument("--class", dest="klass", choices=["str", "tbl", "code", "labeled"])
    p.add_argument("--bank", type=int)
    p.add_argument("--min", type=int, default=1)
    p = sub.add_parser("ptr-refs", help="one ROM pointer's detail (rom-xref refs)")
    p.add_argument("addr"); p.add_argument("--bank", type=int)
    for name in ("strings", "tables"):
        p = sub.add_parser(name)
        p.add_argument("--toml", action="store_true"); p.add_argument("--bank", type=int)
        if name == "tables":
            p.add_argument("--defer", default="", help="comma list: leave tables read only by subs named *like* these")
    sub.add_parser("stats")
    p = sub.add_parser("ram", help="unlabeled vars by number of referencing subs"); p.add_argument("--min", type=int, default=1)
    a = ap.parse_args(argv[1:])
    X = Index(a.title)
    if a.cmd == "refs":
        cmd_refs(X, a.addr)
    elif a.cmd == "hot":
        if a.rom:
            cmd_hot_rom(X, a.min, a.unlabeled)
        else:
            cmd_hot_ram(X, a.min, a.unlabeled, int(a.lo, 16), int(a.hi, 16))
    elif a.cmd == "ram":
        cmd_ram_subs(X, a.min)
    else:
        idx, code_addrs = rom_index(X)
        if a.cmd == "frontier":
            cmd_frontier(X, idx, code_addrs, a.unlabeled, a.klass, a.bank, a.min)
        elif a.cmd == "ptr-refs":
            cmd_ptr_refs(X, int(a.addr.lstrip("$").lower().replace("0x", ""), 16), idx, code_addrs, a.bank)
        elif a.cmd == "strings":
            cmd_strings(X, idx, code_addrs, a.toml, a.bank)
        elif a.cmd == "tables":
            cmd_tables(X, idx, code_addrs, a.toml, a.bank, tuple(x for x in a.defer.split(",") if x))
        else:
            cmd_stats(X, idx, code_addrs)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
