#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deterministic driver that generates the layered source deliverable for a KOEI SNES title,
straight from the ROM's own resource archive (single source of truth -- no hand-kept module
table to drift). Three layers, mirroring how the program is actually built:

    assembly  ->  src/native.asm        (gen_native.py : the native 65C816 kernel/VM engine)
    bytecode  ->  src/<module>.src      (gen_source.py : every byte of the module, bytecode with
                                          all operand values, native stubs, data rows)
    C         ->  src/<module>.c         (vm_lift.py    : EVERY walked routine, named or not)

The per-module ROM location / WRAM base / length come from the archive directory at the
profile's `resource_archive` (tools/archive.py: [name8][bank u16][addr u16][len u32] rows up
to the all-zero row); the WRAM base is read from each module's own `JMP base+3` header. A
module is generated iff it is bytecode in the ROM (header + routine_sig) AND the repo has a
non-empty data/<name>/routines.toml (its symbol table); ROM code modules without one are listed.

The profile must be selected explicitly (--game or $KOEI_SNES_GAME) and must bind to the ROM
(header title + size, and every opcode handler vs the shared table): otherwise gen_layers
exits 2 with the reason before writing anything.

    py -3 tools/gen_layers.py <rom> <title-repo> --game g [--native-end BA60] [--only m1,m2]
    (or KOEI_SNES_GAME=<game> instead of --game)
"""
import argparse
import os
import subprocess
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.machines.snes.mapper import strip_copier_header  # noqa: E402
from emu_harness.vm.koei.snes import archive  # noqa: E402
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402
from emu_harness.vm.koei.snes import vm_isa as ISA  # noqa: E402


def archive_code_modules(rom, repo, P, skipped=None):
    """Yield each CODE module (archive.Module) the repo has a symbol table for; append ROM code
    modules without one to `skipped`."""
    for m in archive.code_modules(rom, P):
        rt = os.path.join(repo, "data", m.name, "routines.toml")
        if not os.path.isfile(rt) or not any(l.lstrip().startswith("0x") for l in open(rt, encoding="utf-8")):
            if skipped is not None:
                skipped.append(m.name)                    # bytecode in the ROM, but no names in the repo yet
            continue
        yield m


def run(tool, args, outpath, env):
    with open(outpath, "w", encoding="utf-8") as fh:
        r = subprocess.run([sys.executable, "-m", "emu_harness.vm.koei.snes." + tool[:-3]] + args,
                           stdout=fh, stderr=subprocess.PIPE, text=True, env=env)
    lines = sum(1 for _ in open(outpath, encoding="utf-8"))
    return lines, r.returncode, (r.stderr or "").strip()


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("repo")
    ap.add_argument("--game", default=None, help="games.toml profile (or $KOEI_SNES_GAME) -- required")
    ap.add_argument("--native-end", default=None, help="native kernel stop addr (hex); default from table")
    ap.add_argument("--native-mx", default="8,16", help="native M/X widths (kernel steady state)")
    ap.add_argument("--only", default=None, help="comma list of modules to (re)generate")
    a = ap.parse_args(argv[1:])

    game = a.game or os.environ.get(_profile.ENV)
    if not game:
        print("gen_layers: no profile selected -- pass --game <%s> or set $%s (there is no default)"
              % ("|".join(_profile.names()), _profile.ENV), file=sys.stderr)
        return 2
    rom, _ = strip_copier_header(open(a.rom, "rb").read())
    try:
        P = _profile.load(game)
        _profile.check_rom(rom, P)                        # header title + size
        ISA.bind(rom, P)                                  # every handler vs the shared table
    except (_profile.ProfileError, ISA.IsaMismatch) as e:
        print("gen_layers: profile %r does not bind to %s:\n  %s" % (game, a.rom, e), file=sys.stderr)
        return 2
    env = dict(os.environ, **{_profile.ENV: game})       # every layer tool decodes with this profile
    srcdir = os.path.join(a.repo, "src")
    os.makedirs(srcdir, exist_ok=True)
    only = set(a.only.split(",")) if a.only else None

    print("=== layered source: %s (%s) ===" % (os.path.basename(os.path.normpath(a.repo)), game))

    errors = 0
    # --- assembly layer: native kernel ---
    nat = os.path.join(a.repo, "data", "native", "routines.toml")
    if only is None or "native" in only:
        if os.path.isfile(nat):
            end = a.native_end or ("%X" % (max(int(l.split("=")[0].strip(), 16)
                                    for l in open(nat, encoding="utf-8")
                                    if l.strip().startswith("0x")) + 0x40))
            ln, rc, err = run("gen_native.py", [a.rom, "--game", game, "--start", "00:8000", "--end", end,
                                                "--syms", nat, "--mx", a.native_mx],
                              os.path.join(srcdir, "native.asm"), env)
            errors += bool(rc)
            print("  [asm ] native.asm            %5d lines%s" % (ln, "  ERR:" + err if rc else ""))
        else:
            print("  [asm ] (no data/native/routines.toml - native symbols are in games.toml; skipped)")

    # --- bytecode + C layers, per code module ---
    skipped = []
    mods = list(archive_code_modules(rom, a.repo, P, skipped))
    unknown = sorted(only - {m.name for m in mods} - {"native"}) if only is not None else []
    if unknown:                                           # a typo would otherwise generate nothing and exit 0
        print("gen_layers: --only names no code module with a symbol table: %s (known: %s)"
              % (", ".join(unknown), ", ".join(m.name for m in mods)), file=sys.stderr)
        return 2
    tot_src = tot_c = 0
    for m in mods:
        if only is not None and m.name not in only:
            continue
        syms = os.path.join(a.repo, "data", m.name)
        common = [a.rom, "--game", game, "--module", m.name, "--syms", syms]
        s_ln, s_rc, s_err = run("gen_source.py", common, os.path.join(srcdir, m.name + ".src"), env)
        c_ln, c_rc, c_err = run("vm_lift.py", common, os.path.join(srcdir, m.name + ".c"), env)
        tot_src += s_ln
        tot_c += c_ln
        errors += bool(s_rc) + bool(c_rc)
        flag = ("  SRC-ERR:" + s_err) if s_rc else (("  C-ERR:" + c_err) if c_rc else "")
        r = m.row
        print("  [byte] %-10s %02X:%04X -> 7E:%04X  .src %5d  .c %5d%s"
              % (m.name, r.bank, r.addr, m.base, s_ln, c_ln, flag))
    done = [m for m in mods if only is None or m.name in only]
    print("  --- %d modules%s: %d .src lines + %d .c lines%s ---" % (
        len(done), (" (of %d with a symbol table)" % len(mods)) if only is not None else "", tot_src, tot_c,
        ("; %d tool error(s)" % errors) if errors else ""))
    if skipped:
        print("  (bytecode modules in the ROM with no data/<name>/routines.toml, not generated: %s)" % ", ".join(skipped))
    return 1 if errors else 0


if __name__ == "__main__":
    from emu_harness.vm.koei.snes import profile as _cli
    sys.exit(_cli.run_cli(main))
