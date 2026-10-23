#!/usr/bin/env python3
"""
The string front-load pass for a KOEI NES title (row L of MIGRATION.md): decode text straight from the
ROM, before any routine is named, so strings clarify routines as the walk reaches them.

  pure   bk / GemFire pure_strings: every NUL-terminated run in every bank that passes the strict
         text filter (passes.strings.is_text), each bank classified TEXT (>= 20) / some text / sparse;
         --toml emits msg_ lines for the text banks and the fixed bank, --apply inserts them.
  dump   UW dump_strings: one bank's printable runs (>= minlen), or --table: its NUL-delimited slots.

Banks are the cart's (PRG size / the profile's bank_size; pure_strings assumed 8K), each read through
its window from the title profile (games.toml).

    py -3.14 -m emu_harness.vm.koei.nes.strings <title> pure [--toml | --apply]
    py -3.14 -m emu_harness.vm.koei.nes.strings <title> dump <bank> [minlen | --table]
"""
import re
import sys

from emu_harness.passes.strings import is_text, nul_runs, nul_slots, printable_runs, slug_words
from emu_harness.vm.koei.nes import titles


class Rom:
    def __init__(self, title):
        self.cfg = cfg = titles.game_cfg(title)
        rom = cfg["_rom"].read_bytes()
        self.prg = rom[16:16 + rom[4] * 0x4000]          # PRG only (CHR-ROM follows it in the file)
        self.win = titles.windows(cfg)
        self.size = cfg.get("bank_size", 0x4000)
        self.banks = len(self.prg) // self.size

    def bank(self, b):
        return self.prg[b * self.size:(b + 1) * self.size]


def pure_scan(R):
    """{bank: [(cpu_addr, text)]} for every bank with a run passing is_text."""
    per = {}
    for b in range(R.banks):
        base = R.win.base(b)
        rows = [(base + off, raw.decode("latin1")) for off, raw in nul_runs(R.bank(b)) if is_text(raw)]
        if rows:
            per[b] = rows
    return per


def cmd_pure(R, mode):
    per = pure_scan(R)
    if mode is None:
        print(f"{'bank':>4} {'strings':>7}  verdict / sample")
        print("-" * 78)
        for b in sorted(per):
            n = len(per[b]); sample = " | ".join(t[:22] for _, t in per[b][:3])
            verdict = "TEXT" if n >= 20 else ("some text" if n >= 5 else "sparse")
            print(f"{b:>4} {n:>7}  [{verdict:9}] {sample}")
        print("-" * 78)
        print("graphics/tile banks (few/no clean strings) get NO labels; text banks -> --apply")
        return
    fixed = max(R.win.fixed()) if R.win.fixed() else None
    text_banks = {b for b in per if len(per[b]) >= 20} | ({fixed} if fixed in per else set())
    blocks = {}
    for b in sorted(text_banks):
        seen = {}
        bl = [f"# --- strings (pure_strings.py: oracle=decoded ASCII, base=${R.win.base(b):04X}; base to verify in walk) ---"]
        for a, t in per[b]:
            nm = "msg_" + slug_words(t)
            if nm in seen:
                nm = f"{nm}_{a:04x}"
            seen[nm] = 1
            cm = t.replace('"', "'").replace("\\", "/")[:70]
            bl.append(f'"0x{a:04X}" = {{ name = "{nm}", comment = "{cm}" }}')
        blocks[b] = bl
    if mode == "--toml":
        for b in sorted(blocks):
            print(f"\n[prg.bank{b}]  ({len(blocks[b])-1} strings)")
            print("\n".join(blocks[b]))
        return
    # --apply: insert each block after its [prg.bankN] header (create the section if absent)
    toml = R.cfg["_labels"]
    lines = toml.read_text(encoding="utf-8").splitlines()
    present = {int(m.group(1)) for ln in lines for m in [re.match(r'\s*\[prg\.bank(\d+)\]\s*$', ln)] if m}
    out, applied = [], 0
    for ln in lines:
        out.append(ln)
        m = re.match(r'\s*\[prg\.bank(\d+)\]\s*$', ln)
        if m and int(m.group(1)) in blocks:
            out.extend(blocks[int(m.group(1))]); applied += len(blocks[int(m.group(1))]) - 1
    for b in sorted(blocks):
        if b not in present:
            out.append(f"\n[prg.bank{b}]")
            out.extend(blocks[b]); applied += len(blocks[b]) - 1
    toml.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"applied {applied} string labels across {len(blocks)} text banks: {sorted(blocks)}")


def cmd_dump(R, b, arg):
    if arg == "--table":
        rows = nul_slots(R.bank(b))
        print(f"bank {b}: {len(rows)} non-empty string slots")
        for idx, off, s in rows:
            print(f"  [{idx:3d}] ${off:04X}(bankoff): {s.decode('ascii', 'replace')}")
        return
    minlen = int(arg) if arg else 3
    rows = printable_runs(R.bank(b), minlen)
    print(f"bank {b}: {len(rows)} ASCII strings (minlen {minlen})")
    for off, s in rows:
        print(f"  ${off:04X}: {s.decode('ascii', 'replace')}")


def main(argv):
    if len(argv) < 3 or argv[2] not in ("pure", "dump"):
        sys.exit(__doc__)
    R = Rom(argv[1])
    if argv[2] == "pure":
        mode = argv[3] if len(argv) > 3 else None
        if mode not in (None, "--toml", "--apply"):
            sys.exit(__doc__)
        cmd_pure(R, mode)
    else:
        cmd_dump(R, int(argv[3], 0), argv[4] if len(argv) > 4 else None)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
