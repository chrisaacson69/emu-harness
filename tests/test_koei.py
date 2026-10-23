"""The KOEI VM package (vm/koei) as moved in row F: the old-name aliases and per-title data selection.

The fold's oracle is the before/after run of every title's decompile (MIGRATION.md row F); these pin
what that run cannot show on its own."""
import subprocess
import sys

from emu_harness.core.alias import alias_package


def test_alias_gives_the_same_module_objects():
    alias_package("koei_alias_test", "emu_harness.vm.koei")
    import emu_harness.vm.koei.vm_decompile as real
    import koei_alias_test.vm_decompile as via_alias
    from koei_alias_test import vm_cfg
    assert via_alias is real
    assert vm_cfg.__name__ == "emu_harness.vm.koei.vm_cfg"


def test_python_dash_m_through_an_alias(tmp_path):
    for pkg in ("oldname", "probe_pkg"):
        (tmp_path / pkg).mkdir()
    (tmp_path / "probe_pkg" / "__init__.py").write_text("")
    (tmp_path / "probe_pkg" / "probe.py").write_text(
        "print('ran as', __name__, 'from', __file__.replace('\\\\', '/').split('/')[-2:])\n")
    (tmp_path / "oldname" / "__init__.py").write_text(
        "from emu_harness.core.alias import alias_package\nalias_package(__name__, 'probe_pkg')\n")
    r = subprocess.run([sys.executable, "-m", "oldname.probe"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ran as __main__ from ['probe_pkg', 'probe.py']" in r.stdout      # the real file, as __main__


def test_na1_certified_folds_apply_to_na1_only():
    from emu_harness.vm.koei import vm_decompile as VC
    VC.set_schema("gk")
    assert VC._EXTOP_OVERRIDE == {}
    VC.set_schema("na1")
    assert 0x8327 in VC._EXTOP_OVERRIDE
