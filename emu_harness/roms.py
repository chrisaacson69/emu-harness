"""Where a title's ROM is: the machine's ROM store, checked by hash.

A ROM is copyrighted and never committed, so it lives outside every repo, in one folder per
machine: `roms` in config.toml (gitignored; see config.example.toml). A title names its ROM by
file name and, where known, its SHA-1, and find_rom looks in the store first, then in the
fallback folders the caller gives (a title repo's root, where ROMs used to sit), so a title can
move to the store without breaking anything that runs before it does.

The SHA-1 is of the whole file, iNES header included (`sha1sum`, as the project READMEs record
it). BizHawk's NES hash leaves the 16-byte header out; that is a different number.

    find_rom("Nobunaga's Ambition (USA).nes", sha1="1f7d44...", fallback=[repo_root])
"""
from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config.toml"


def store(config: Path = CONFIG) -> Path | None:
    """The ROM store from config.toml (`roms = 'C:\\...'`), or None if it is not set."""
    if not config.exists():
        return None
    d = tomllib.loads(config.read_text(encoding="utf-8")).get("roms")
    return Path(d) if d else None


def sha1_of(path: Path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def find_rom(name: str, sha1: str | None = None, fallback=(), missing_ok: bool = False) -> Path:
    """The path of ROM `name`: <store>/name, else the first <fallback>/name that exists.

    A found file whose SHA-1 differs from `sha1` is an error wherever it is (the wrong dump, or a
    patched one). Not found: FileNotFoundError naming every place looked, or with missing_ok the
    first candidate, so a caller that only builds paths keeps working on a machine without it."""
    s = store()
    cands = ([s / name] if s else []) + [Path(d) / name for d in fallback]
    for p in cands:
        if p.is_file():
            if sha1 and sha1_of(p) != sha1.lower():
                raise ValueError(f"{p}: SHA-1 {sha1_of(p)}, expected {sha1.lower()} (a different dump?)")
            return p
    if missing_ok and cands:
        return cands[0]
    where = "\n  ".join(map(str, cands)) or "(no store set and no fallback)"
    raise FileNotFoundError(f"{name} not found; looked in:\n  {where}\n"
                            f"Set `roms` in {CONFIG} to this machine's ROM folder and put it there.")
