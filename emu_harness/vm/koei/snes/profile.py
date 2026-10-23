#!/usr/bin/env python3
"""
Load a KOEI SNES title profile from games.toml.

The bytecode VM engine is shared across KOEI SNES titles; only the addresses move.
The engine tools import this to stay title-agnostic:

    from emu_harness.vm.koei.snes import profile as _profile
    P = _profile.load("rot3k2")               # a named profile (the name is required)
    P = _profile.select(args.game, rom)       # what a tool runs with (see below)
    _profile.check_rom(rom, P)                # raises unless the ROM is that title
    P["primary_table"], P["fetch_helpers"], P["game"], ...

Selecting a profile -- there is NO silent default (there used to be "the first profile"):
  1. --game (the `game` argument), else
  2. $KOEI_SNES_GAME, else
  3. when the tool has the ROM: the ONE profile whose `header_title` equals the ROM's internal
     header title ($00:FFC0, 21 bytes) -- announced on stderr, never guessed; else
  4. ProfileError naming the profiles.
An explicitly selected profile must match the ROM too: check_rom() compares the header title
(and `rom_bytes`) and raises on a mismatch; vm_isa.bind() calls it, so every decoding tool
fails loudly on the wrong title instead of decoding it with another title's addresses.

Parsing: minitoml.load_file(), which uses tomllib when the interpreter has it (3.11+) and its
own reader otherwise (this machine's `py -3` is 3.9). Both yield the same dicts; fetch_helpers
keys are normalised to int either way. A
games.toml that does not parse RAISES -- there is no lenient fallback. Each profile dict also
carries its own name as P["game"].

Module-residency keys (optional per profile):
  resident_modules = ["COMMON", "ALNMAIN"]   modules always in WRAM (default ["root"])
  [<game>.coresident]                        per module: the modules co-loaded with it
  AIRWAY = ["COMMAND", "DIORAMA"]            (their routines/globals are callable from it)
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_GAMES = os.path.join(_HERE, "games.toml")
ENV = "KOEI_SNES_GAME"


class ToolError(ValueError):
    """A refusal a CLI reports as one clear line (exit 2): wrong / missing profile, a ROM that
    disagrees with the shared opcode table, a malformed archive."""


class ProfileError(ToolError):
    """No profile selected, an unknown profile, or a profile that is not this ROM's title."""


def run_cli(main, argv=None):
    """Run a tool's main(argv); a ToolError is printed as `tool: message` on stderr and the exit
    status is 2 (loud and clear -- no traceback, no fallback)."""
    argv = argv or sys.argv
    try:
        return main(argv)
    except ToolError as e:
        print("%s: %s" % (os.path.basename(argv[0]), e), file=sys.stderr)
        return 2


def _read_all():
    from emu_harness.vm.koei import minitoml
    games = minitoml.load_file(_GAMES)
    for name, g in games.items():
        fh_ = g.get("fetch_helpers")
        if isinstance(fh_, dict):         # TOML keys are strings ("0xA089") -> int addresses
            g["fetch_helpers"] = {int(k, 0) if isinstance(k, str) else k: v for k, v in fh_.items()}
        g["game"] = name
    return games


def names():
    """Profile names in file order."""
    return list(_read_all())


def load(game):
    """The named profile. The name is required -- there is no default profile."""
    games = _read_all()
    if not game:
        raise ProfileError("no profile named (games.toml has: %s)" % ", ".join(games))
    if game not in games:
        raise ProfileError("unknown game %r (games.toml has: %s)" % (game, ", ".join(games)))
    return games[game]


def header_title(rom, mapping="lorom"):
    """The ROM's internal header title (21 bytes at CPU $00:FFC0), right-stripped."""
    from emu_harness.machines.snes.mapper import HEADER_FILE_OFFSET
    o = HEADER_FILE_OFFSET[mapping]
    return bytes(rom[o:o + 21]).decode("ascii", "replace").rstrip(" \x00")


def check_rom(rom, P):
    """Raise ProfileError unless `rom` is the title profile P describes (header title + size)."""
    want = P.get("header_title")
    if want is None:
        raise ProfileError("profile %r has no header_title to check the ROM against" % P.get("game"))
    got = header_title(rom, P.get("mapping", "lorom"))
    if got != want:
        raise ProfileError("profile %r is %r but the ROM header title is %r -- wrong --game / $%s?"
                           % (P.get("game"), want, got, ENV))
    n = P.get("rom_bytes")
    if n is not None and len(rom) != n:
        raise ProfileError("profile %r expects a %d-byte ROM, this one has %d bytes"
                           % (P.get("game"), n, len(rom)))
    return P


def select(game=None, rom=None, quiet=False):
    """The profile a tool runs with: --game, else $KOEI_SNES_GAME, else (given the ROM) the
    unique profile whose header_title matches it. Raises ProfileError otherwise. With a ROM,
    the chosen profile is always checked against it (check_rom)."""
    g = game or os.environ.get(ENV)
    if g:
        P = load(g)
    elif rom is not None:
        games = _read_all()
        hits = [P for P in games.values()
                if P.get("header_title") == header_title(rom, P.get("mapping", "lorom"))]
        if len(hits) != 1:
            raise ProfileError("no --game / $%s given and %d profiles match the ROM header title %r "
                               "(games.toml has: %s)" % (ENV, len(hits), header_title(rom), ", ".join(games)))
        P = hits[0]
        if not quiet:
            print("profile: %s (selected by the ROM header title %r)" % (P["game"], P["header_title"]),
                  file=sys.stderr)
    else:
        raise ProfileError("no profile selected: pass --game <%s> or set $%s" % ("|".join(names()), ENV))
    if rom is not None:
        check_rom(rom, P)
    return P


def resident_modules(P):
    """Modules always resident in WRAM (the shared libraries every overlay may call)."""
    return list(P.get("resident_modules", ["root"]))


def coresident(P, module):
    """Modules co-loaded with `module` (beyond the resident ones), from [<game>.coresident]."""
    return list((P.get("coresident") or {}).get(module, []))


def visible_modules(P, module):
    """`module` itself, then the resident modules, then its coresident ones (no duplicates):
    every module whose code/data `module` can address while it runs."""
    out = [module]
    for m in resident_modules(P) + coresident(P, module):
        if m not in out:
            out.append(m)
    return out
