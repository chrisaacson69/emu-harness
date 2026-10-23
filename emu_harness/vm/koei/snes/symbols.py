#!/usr/bin/env python3
"""
Symbol layer for the KOEI SNES bytecode tools.

Loads a title's natural-language symbols so the disassembler/walker render addresses
and syscall ids as names:
  - strings.toml / routines.toml / globals.toml -> {runtime_addr: label}
  - enums.py                                     -> {syscall_id: name} + string tables
  - ../syscall_names.toml ([[syscall]] id/name)  -> {syscall_id: name} when enums.py has none

    from emu_harness.vm.koei.snes.symbols import Symbols
    S = Symbols("../rot3k2-snes-decompiler/data/comroot", profile=P)
    S.string(0x6D51)   -> "threaten_whom"
    S.kind(0x6D51)     -> "string"      (routine / string / global / table: which file named it)
    S.syscall(0x32)    -> "sys_blit_rect"

A module's data dir is loaded first, then the dirs of every module it can address while it
runs (sibling dirs under the same data/): the profile's resident_modules (default ["root"],
the rot3k2/Gemfire resident library; Aerobiz: COMMON + ALNMAIN) and the module's coresident
modules ([<game>.coresident], e.g. an Aerobiz $D000 app -> COMMAND, DIORAMA). Their addresses
are disjoint while co-loaded, so cross-module calls render by name. The first definition of
an address wins (the module's own). All files are optional.

The profile is REQUIRED (it decides which modules are visible): Symbols raises without one
instead of guessing a title.
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
from emu_harness.vm.koei.snes import profile as _profile  # noqa: E402

_LINE = re.compile(r'(0x[0-9A-Fa-f]+)\s*=\s*"([^"]+)"')


def load_syscall_names(path):
    """id -> name from a title's data/syscall_names.toml (the [[syscall]] array: `id = 0xNN`
    then `name = "..."`); {} when the file is absent."""
    out, cur = {}, None
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0]
            m = re.match(r"\s*id\s*=\s*(0x[0-9A-Fa-f]+|\d+)", line)
            if m:
                cur = int(m.group(1), 0)
            n = re.match(r'\s*name\s*=\s*"([^"]+)"', line)
            if n and cur is not None:
                out[cur] = n.group(1)
                cur = None
    return out


class Symbols:
    def __init__(self, data_dir=None, profile=None):
        if profile is None:
            raise _profile.ProfileError("Symbols needs the title profile (it decides the resident/coresident "
                                        "modules whose names are visible); pass profile=P")
        self.strings, self.syscalls = {}, {}
        self.kinds = {}                            # addr -> routine / string / global / table
        self.tables, self.table_base = {}, {}      # name->{idx:label}, addr->name
        self.loaded = []                           # module dirs loaded, in order
        self.P = profile
        if data_dir:
            self.load(data_dir)

    def load(self, data_dir):
        d = os.path.normpath(data_dir)
        self._load_dir(d)
        me, parent = os.path.basename(d), os.path.dirname(d)
        for m in _profile.visible_modules(self.P, me)[1:]:
            md = os.path.join(parent, m)
            if os.path.isdir(md):
                self._load_dir(md)
        if not self.syscalls:                      # the title-wide table beside the module dirs
            self.syscalls = load_syscall_names(os.path.join(parent, "syscall_names.toml"))

    def _load_dir(self, data_dir):
        self.loaded.append(data_dir)
        # strings.toml + routines.toml + globals.toml all map addr -> label
        for fn, kind in (("strings.toml", "string"), ("routines.toml", "routine"), ("globals.toml", "global")):
            p = os.path.join(data_dir, fn)
            if os.path.exists(p):
                for line in open(p, encoding="utf-8"):
                    line = line.split("#", 1)[0]
                    for a, name in _LINE.findall(line):
                        if int(a, 0) not in self.strings:
                            self.strings[int(a, 0)] = name
                            self.kinds[int(a, 0)] = kind
        ep = os.path.join(data_dir, "enums.py")
        if os.path.exists(ep):
            ns = {}
            exec(compile(open(ep, encoding="utf-8").read(), ep, "exec"), ns)
            if not self.syscalls:
                self.syscalls = ns.get("SYSCALLS", {})
            self.tables.update(ns.get("STRING_TABLES", {}))
            for name, base in ns.get("STRING_TABLE_BASE", {}).items():
                self.table_base[base] = name
                if base not in self.strings:
                    self.strings[base] = "tbl_" + name        # base addr -> table name
                    self.kinds[base] = "table"

    def string(self, addr):
        return self.strings.get(addr)

    def kind(self, addr):
        """'routine' / 'string' / 'global' / 'table' for a named address, else None."""
        return self.kinds.get(addr)

    def syscall(self, i):
        return self.syscalls.get(i)
