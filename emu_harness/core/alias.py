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

    def _real(self, fullname):
        return self.new + fullname[len(self.old):]

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self.old + "."):
            return None
        real = importlib.util.find_spec(self._real(fullname))
        if real is None:
            return None
        return importlib.util.spec_from_loader(fullname, self, origin=real.origin,
                                               is_package=real.submodule_search_locations is not None)

    def create_module(self, spec):
        # the real module; import machinery leaves its existing __name__ / __spec__ alone
        return importlib.import_module(self._real(spec.name))

    def exec_module(self, module):
        pass                                      # already executed under its real name

    def get_code(self, fullname):
        """For `python -m old.x`: runpy runs the real module's code as __main__."""
        real = self._real(fullname)
        return importlib.util.find_spec(real).loader.get_code(real)


def alias_package(old: str, new: str):
    """Make `old` and every `old.x` resolve to `new` / `new.x`. Returns the real package."""
    real = importlib.import_module(new)
    sys.modules[old] = real
    if not any(isinstance(f, _Alias) and f.old == old for f in sys.meta_path):
        sys.meta_path.insert(0, _Alias(old, new))
    return real
