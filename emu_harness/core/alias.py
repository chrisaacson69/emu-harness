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
import types


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, old: str, new: str, also: tuple[str, ...] = ()):
        self.old, self.new, self.also = old, new, also

    def _real(self, fullname):
        """The first of new.x, also[0].x, ... that exists (a package can be split after it moved)."""
        tail = fullname[len(self.old):]
        for pkg in (*self.also, self.new):
            name = pkg + tail
            try:
                if importlib.util.find_spec(name) is not None:
                    return name
            except ModuleNotFoundError:
                pass
        return self.new + tail

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


def alias_package(old: str, new: str, also: tuple[str, ...] = ()):
    """Make `old` resolve to `new`, and every `old.x` to the first of `also[0].x`, `also[1].x`, ...,
    `new.x` that exists (for a package that was split into subpackages after it moved).
    Returns the real package."""
    real = importlib.import_module(new)
    if not any(isinstance(f, _Alias) and f.old == old for f in sys.meta_path):
        sys.meta_path.insert(0, _Alias(old, new, tuple(also)))
    # The old name gets its own small package object. Putting `real` itself there would make
    # `from old import x` look for `x` under the REAL name, missing a submodule that moved to an
    # `also` package; this proxy routes every attribute it lacks through the finder above.
    proxy = types.ModuleType(old, real.__doc__)
    proxy.__path__ = []                               # a package: `old.x` imports reach the finder

    def __getattr__(name):
        if hasattr(real, name):
            return getattr(real, name)
        try:
            return importlib.import_module(f"{old}.{name}")
        except ModuleNotFoundError:
            raise AttributeError(f"module {old!r} has no attribute {name!r}") from None

    proxy.__getattr__ = __getattr__
    sys.modules[old] = proxy
    return real
