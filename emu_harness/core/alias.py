"""Keep an old package name working after its code moved: `old.x` IS `new.x`, the same module object.

When a package moves into emu-harness (MIGRATION.md), the title repos and tools that import it by its
old name keep working through an alias. A `__path__` alias would load each file a second time under the
old name, with its own module state: a setting made through one name (a title's syscall table, a ROM
path) would not reach code that imported the other. This alias returns the real module instead.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, old: str, new: str):
        self.old, self.new = old, new

    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(self.old + "."):
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        # the real module; import machinery leaves its existing __name__ / __spec__ alone
        return importlib.import_module(self.new + spec.name[len(self.old):])

    def exec_module(self, module):
        pass                                      # already executed under its real name


def alias_package(old: str, new: str):
    """Make `old` and every `old.x` resolve to `new` / `new.x`. Returns the real package."""
    real = importlib.import_module(new)
    sys.modules[old] = real
    if not any(isinstance(f, _Alias) and f.old == old for f in sys.meta_path):
        sys.meta_path.insert(0, _Alias(old, new))
    return real
