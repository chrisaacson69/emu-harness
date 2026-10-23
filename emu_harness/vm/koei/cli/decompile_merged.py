"""Merged PRG-keyed decompiled view — all 4 CODE banks in ONE C file, flat by PRG.

Reuses the exact bytecode->C pipeline of decompile-all.py (via its `bank_subs`), then
makes the result navigable as a single flat address space:

  * PRG REWRITE — every CPU address becomes its bank-unambiguous PRG ROM offset
    (prg = bank*0x4000 + (cpu & 0x3FFF)). The switchable $8000-$BFFF window means the
    same CPU address is a DIFFERENT function per bank; the PRG offset disambiguates, so
    ctrl-F a label finds exactly one definition. Goto labels become `L_pPPPPP`, address
    comments become `// PRG $PPPPP`.
  * CALL-TARGET ANNOTATION — each named call gets `// -> bankT $CPU` so you can see (and
    follow) where the callee is defined, across banks. The two cross-bank ENTRY
    trampolines are resolved to the REAL bytecode entry they JMP into:
        call_bank_wrap(N)    -> bank N's $8000 native JMP target (e.g. 2 -> battle_init_driver)
        call_bank10_entry(c) -> bank 10 audio engine ($8003), arg = command

Output: decompiled/all_banks.c. Deterministic (fixed ROM + toml -> identical C), so it
is committable. This is a DERIVED navigation view; the per-bank decompiled/bank_NN.c
files remain the canonical, regen-guarded artifacts (see decompile-all.py).

Usage:
  py -3 tools/decompile-merged.py                      # -> decompiled/all_banks.c
  py -3 tools/decompile-merged.py --out path/to/file.c
"""

import argparse
import re
import sys
import tempfile
from pathlib import Path

from emu_harness.vm.koei.titles import game_cfg as _game_cfg

ROOT = Path(_game_cfg("na1")["_root"])                         # nobunaga/, through the [na1] profile (F1b)

from emu_harness.vm.koei import nobunaga_vm

CODE_BANKS = [0, 1, 2, 10, 14, 15]
ROM_PATH = ROOT / "Nobunaga's Ambition (USA).nes"


def prg_offset(bank, cpu):
    """Flat PRG ROM offset for a (bank, cpu-address) pair — the bank-unambiguous identity."""
    return bank * 0x4000 + (cpu & 0x3FFF)


def cpu_of_prg(prg):
    """Inverse of prg_offset for ROM space: $C000+ is fixed bank 15, else the window."""
    return (0xC000 if prg >= 15 * 0x4000 else 0x8000) + (prg & 0x3FFF)


def _sanitize(name):
    """Mirror vm_decompile._label_name so reverse-lookup names match the C call names."""
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if name and name[0].isdigit():
        name = "_" + name
    return name or None


def entry_jmp_targets():
    """bank -> CPU address that bank's $8000 native entry JMPs to (the bytecode entry the
    call_bank trampoline really reaches). Read straight from ROM (deterministic)."""
    rom = ROM_PATH.read_bytes()
    out = {}
    for bank in range(16):
        off = 0x10 + bank * 0x4000
        if rom[off] == 0x4C:  # JMP abs
            out[bank] = rom[off + 1] | (rom[off + 2] << 8)
    return out


# Reserved words that are followed by '(' but are not call targets.
_KEYWORDS = {"if", "while", "for", "switch", "return", "sizeof", "word", "do"}


def rewrite_prg(text, bank):
    """Rewrite this sub's CPU addresses to flat PRG offsets. Every 4-hex `$XXXX` and every
    `L_XXXX` in a sub's C is a code address in the sub's OWN bank (line-address comments,
    goto/branch targets, the header) — verified: _mem_name/immediates emit `mem_`/`0x`/
    labels, never `$`; ext-op indices are 2-hex. So a uniform per-bank rewrite is correct."""
    text = re.sub(r'L_([0-9A-Fa-f]{4})\b',
                  lambda m: f"L_p{prg_offset(bank, int(m.group(1), 16)):05X}", text)
    text = re.sub(r'\$([0-9A-Fa-f]{4})\b',
                  lambda m: f"PRG ${prg_offset(bank, int(m.group(1), 16)):05X}", text)
    return text


def _append_anno(line, anno):
    """Append a `-> ...` target note. Merge into the line's existing `// PRG $...` site
    comment when present, else start a fresh comment."""
    if "//" in line:
        return line.rstrip() + " " + anno
    return line.rstrip() + "    // " + anno


def annotate_call_targets(line, bank, name_to_prg, entry_tgts, prg_names):
    """Add a cross-bank target note for the first resolvable call on `line`.
    Definition lines (`word NAME(`) are skipped; the two entry trampolines are resolved
    to the real bytecode entry they reach."""
    if line.lstrip().startswith("word "):
        return line

    # --- entry trampolines (resolve the bank-switch JMP, not their own $CBB1/$CA03) ---
    m = re.search(r'\bcall_bank10_entry\((\d+)\)', line)
    if m:
        tgt = entry_tgts.get(10)
        loc = f"${tgt:04X}" if tgt is not None else "$8003"
        return _append_anno(line, f"-> bank10 audio entry {loc} (cmd {m.group(1)})")
    m = re.search(r'\bcall_bank_wrap\((\d+)\)', line)
    if m:
        n = int(m.group(1))
        tgt = entry_tgts.get(n)
        if tgt is not None:
            nm = prg_names.get(prg_offset(n, tgt))
            nm = f" {nm}" if nm else ""
            return _append_anno(line, f"-> bank{n} entry{nm} ${tgt:04X}")
        return _append_anno(line, f"-> bank{n} entry $8000")

    # --- generic named call: first identifier-with-'(' that resolves uniquely ---
    for cand in re.finditer(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(', line):
        nm = cand.group(1)
        if nm in _KEYWORDS:
            continue
        prgs = name_to_prg.get(nm)
        if prgs and len(prgs) == 1:
            p = next(iter(prgs))
            return _append_anno(line, f"-> bank{p // 0x4000} ${cpu_of_prg(p):04X}")
    return line


def transform_sub(sub_c, bank, name_to_prg, entry_tgts, prg_names):
    text = rewrite_prg(sub_c, bank)
    out = []
    for line in text.split("\n"):
        out.append(annotate_call_targets(line, bank, name_to_prg, entry_tgts, prg_names))
    return "\n".join(out)


def fn_name_of(sub_c):
    m = re.search(r'^word ([A-Za-z_][A-Za-z0-9_]*)\(', sub_c, re.M)
    return m.group(1) if m else "?"


def build(bank_subs):
    """Build the merged records. `bank_subs` is decompile-all.py's shared decompile core
    (injected, so this module doesn't import decompile-all at load time — keeps the
    decompile-all -> decompile-merged dependency one-directional / non-circular)."""
    # ROM-space name map (PRG-keyed, bank-unambiguous) — used both to resolve trampoline
    # targets and to build the reverse name->prg map for call annotation.
    prg_names = {prg: _sanitize(name)
                 for prg, (name, _c, _b, _cpu) in nobunaga_vm.load_labels_by_prg().items()}
    name_to_prg = {}
    for prg, nm in prg_names.items():
        if nm:
            name_to_prg.setdefault(nm, set()).add(prg)
    entry_tgts = entry_jmp_targets()

    records = []   # (prg, bank, cpu, fn_name, transformed_c)
    warnings = []
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        for bank in CODE_BANKS:
            _labels, subs, misaligned = bank_subs(bank, tmp_dir)
            if misaligned:
                warnings.append(f"bank {bank}: {misaligned} subs did not tile cleanly")
            for stub, sub_c in subs:
                t = transform_sub(sub_c, bank, name_to_prg, entry_tgts, prg_names)
                records.append((prg_offset(bank, stub), bank, stub, fn_name_of(sub_c), t))
    records.sort(key=lambda r: r[0])
    return records, warnings


def render(records, warnings):
    L = [
        "// ============================================================",
        "// source/4-c/all_banks.c  —  GENERATED by `python -m koeivm.cli.decompile_merged`",
        f"// All {len(records)} bytecode subroutines from the 4 code banks (0,1,2,15) of",
        "// Nobunaga's Ambition (USA).nes, MERGED into one flat PRG-keyed address space.",
        "//",
        "// Addresses are PRG ROM offsets (prg = bank*0x4000 + (cpu & 0x3FFF)); the same CPU",
        "// address is a different function per bank, so PRG disambiguates -> ctrl-F a label",
        "// finds exactly one definition. Goto labels are L_pPPPPP; `// PRG $P` are site",
        "// addrs; `// -> bankN $CPU` names where a call's target is DEFINED (cross-bank",
        "// follow). call_bank_wrap(N)/call_bank10_entry(c) resolve to the real entry.",
        "//",
        "// DERIVED VIEW — deterministic (fixed ROM+toml -> identical C). The canonical,",
        "// regen-guarded artifacts are decompiled/bank_NN.c (tools/decompile-all.py).",
        "// ============================================================",
        "",
    ]
    for w in warnings:
        L.append(f"// WARNING: {w}")
    if warnings:
        L.append("")
    # Table of contents (PRG-sorted) — the navigation index.
    L.append("// ---- index (PRG-sorted) ----")
    for prg, bank, cpu, name, _t in records:
        L.append(f"//   PRG ${prg:05X}  bank{bank:<2} ${cpu:04X}  {name}")
    L.append("// ----------------------------")
    L.append("")
    for prg, bank, cpu, _name, t in records:
        L.append(f"// ===== bank{bank} ${cpu:04X}  (PRG ${prg:05X}) =====")
        L.append(t)
        L.append("")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Merged PRG-keyed decompiled view")
    ap.add_argument("--out", default=str(ROOT / "source" / "4-c" / "all_banks.c"),
                    help="output path (default: source/4-c/all_banks.c)")
    args = ap.parse_args()
    from emu_harness.vm.koei.cli import decompile_all
    records, warnings = build(decompile_all.bank_subs)
    text = render(records, warnings)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"Merged {len(records)} subs across {len(CODE_BANKS)} code banks -> "
          f"{out_path} ({text.count(chr(10))} lines)")
    for w in warnings:
        print(f"  warning: {w}")


if __name__ == "__main__":
    main()
