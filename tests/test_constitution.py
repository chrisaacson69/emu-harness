"""The import contracts in pyproject.toml, run as a test so pytest enforces the layer rules.

Called through Python, not the lint-imports.exe launcher: Application Control blocks that .exe on
the work machine (Permission denied), and a blocked checker must never read as a pass.
"""
from importlinter.cli import lint_imports


def test_import_contracts_hold():
    assert lint_imports() == 0, "an import contract is broken: see the lint-imports report above"
