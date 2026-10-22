"""The import contracts in pyproject.toml, run as a test so pytest enforces the layer rules.

Called through Python, not the lint-imports.exe launcher: Application Control blocks that .exe on
the work machine (Permission denied), and a blocked checker must never read as a pass.
"""
from pathlib import Path

from importlinter.cli import lint_imports

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_import_contracts_hold():
    # Name the config: import-linter otherwise looks in the cwd, and from any other folder it
    # finds no contracts and fails for the wrong reason.
    assert lint_imports(config_filename=str(PYPROJECT)) == 0, "an import contract is broken: see the report above"
