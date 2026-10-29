"""The build tool: the phase table, its manual, the roadmap template, and `status`."""
import json
import re
import tomllib
from pathlib import Path

import pytest

from emu_harness import __main__ as cli
from emu_harness.build import status as st

REPO = Path(__file__).resolve().parents[1]
KEYS = [p["key"] for p in st.load_phases()]


def test_manual_follows_the_phase_table():
    """PHASES.md has one heading per phase, in table order, each carrying its gate verbatim."""
    text = (REPO / "PHASES.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## \d+\. .+ \(`(\w+)`\)$", text, re.M)
    assert headings == KEYS
    for p in st.load_phases():
        assert f"**Gate:** {p['gate']}" in text, f"PHASES.md lacks {p['key']}'s gate as written in phase_table.toml"


def test_every_gate_path_exists():
    """A missing path would make a gate look fresh forever (git log on nothing finds no commits)."""
    for p in st.load_phases():
        for track in st.TRACKS:
            for path in st.phase_paths(p, track):
                assert (REPO / path).exists(), f"{p['key']}: {path}"


def test_template_names_only_real_phases():
    t = tomllib.loads(st.TEMPLATE.read_text(encoding="utf-8"))
    assert set(t["phase"]) <= set(KEYS)
    assert {"name", "track", "machine", "profile", "entry"} <= set(t["project"])


def _project(tmp_path, body):
    (tmp_path / st.ROADMAP).write_text(body, encoding="utf-8")
    return tmp_path


BASE = '[project]\nname = "T"\ntrack = "native"\nmachine = "nes"\n'


def test_start_is_the_first_phase_not_done(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "staleness", lambda toolset, paths: ("fresh", "ok"))
    p = _project(tmp_path, BASE + '[phase.classify]\nstatus = "done"\ntoolset = "x"\n'
                 '[phase.recon]\nstatus = "n/a"\n[phase.discover]\nstatus = "partial"\ntoolset = "x"\n')
    s = st.status(p)
    assert s["start"] == {"phase": "discover", "reason": "partial"}
    assert [r["status"] for r in s["phases"]][:4] == ["done", "n/a", "partial", "todo"]


def test_a_stale_done_gate_is_where_to_start(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "staleness", lambda toolset, paths: ("stale", "2 commit(s)"))
    p = _project(tmp_path, BASE + '[phase.classify]\nstatus = "done"\ntoolset = "x"\n')
    assert st.status(p)["start"]["phase"] == "classify"


def test_an_open_revisit_on_a_done_phase_is_where_to_start(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "staleness", lambda toolset, paths: ("fresh", "ok"))
    p = _project(tmp_path, BASE + '[phase.classify]\nstatus = "done"\ntoolset = "x"\n'
                 '[phase.recon]\nstatus = "done"\ntoolset = "x"\n'
                 '[[revisit]]\nitem = "a"\nphase = "recon"\nstatus = "open"\n'
                 '[[revisit]]\nitem = "b"\nphase = "classify"\nstatus = "closed"\n')
    assert st.status(p)["start"] == {"phase": "recon", "reason": "1 open revisit(s)"}


def test_staleness_never_reads_as_fresh_without_git_saying_so():
    assert st.staleness("", ["PHASES.md"])[0] == "stale"
    assert st.staleness("0" * 40, ["PHASES.md"])[0] == "unknown"


def test_staleness_follows_the_paths(monkeypatch):
    calls = {}

    def fake_git(*args):
        calls[args[0]] = args
        if args[0] == "rev-parse":
            return 0, "abc"
        if args[0] == "log":
            return 0, "d1 d2" if "touched.py" in args else ""
        return 0, ""
    monkeypatch.setattr(st, "_git", fake_git)
    assert st.staleness("abc", ["touched.py"])[0] == "stale"
    assert st.staleness("abc", ["quiet.py"])[0] == "fresh"
    assert calls["log"][-2:] == ("--", "quiet.py")


@pytest.mark.parametrize("body, msg", [
    (BASE + '[phase.clasify]\nstatus = "done"\n', "not a phase"),
    (BASE + '[phase.classify]\nstatus = "finished"\n', "status must be"),
    (BASE.replace('"native"', '"asm"'), "track must be"),
    (BASE + '[[revisit]]\nitem = "x"\nphase = "nope"\nstatus = "open"\n', "names no phase"),
])
def test_a_misread_roadmap_is_refused(tmp_path, body, msg):
    with pytest.raises(st.RoadmapError, match=msg):
        st.status(_project(tmp_path, body))


def test_cli_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(st, "staleness", lambda toolset, paths: ("fresh", "ok"))
    p = _project(tmp_path, BASE + '[[revisit]]\nitem = "fix"\nphase = "discover"\nstatus = "open"\n'
                 '[next]\nsession = "do it"\n')
    cli.main(["status", str(p), "--json"])
    s = json.loads(capsys.readouterr().out)
    assert s["start"]["phase"] == "classify"
    assert next(r for r in s["phases"] if r["key"] == "discover")["open_revisits"] == 1
    assert s["next"] == "do it"


def test_missing_roadmap_points_at_the_template(tmp_path):
    with pytest.raises(st.RoadmapError, match="roadmap.template.toml"):
        st.status(tmp_path)


def test_resolve_project_finds_a_folder_inside_a_sibling(tmp_path):
    (tmp_path / "solo").mkdir()
    (tmp_path / "mono" / "mappy").mkdir(parents=True)
    (tmp_path / "mono" / "mappy" / st.ROADMAP).write_text("")
    (tmp_path / "mono" / "notes").mkdir()                       # no roadmap: never a match
    assert st.resolve_project("solo", tmp_path) == (tmp_path / "solo").resolve()
    assert st.resolve_project("mappy", tmp_path) == (tmp_path / "mono" / "mappy").resolve()
    with pytest.raises(st.RoadmapError):
        st.resolve_project("notes", tmp_path)
    (tmp_path / "other" / "mappy").mkdir(parents=True)
    (tmp_path / "other" / "mappy" / st.ROADMAP).write_text("")
    with pytest.raises(st.RoadmapError, match="ambiguous"):
        st.resolve_project("mappy", tmp_path)


def test_doccheck_reads_each_pair_against_the_label_file(tmp_path):
    from emu_harness.build import doccheck
    mlb = tmp_path / "t.mlb"
    mlb.write_text("NesPrgRom:3C100:music_driver:\nNesPrgRom:3C180:next_routine:\n"
                   "NesInternalRam:0072:ppumask_shadow:\nNesPrgRom:3C200:sqrt_int:\n", encoding="utf-8")
    (tmp_path / "p.md").write_text(
        "`music_driver` ($C100) and `music_driver` ($C150), `ppumask_shadow` ($0071),\n"
        "`cbcd_sqrt` ($C200), `no_such_name` ($D000), `$0072 ppumask_shadow`.\n", encoding="utf-8")
    got = [(r[2], r[3], r[4]) for r in doccheck.check(tmp_path, mlb, ["*.md"])]
    assert got == [("music_driver", 0xC100, "ok"), ("music_driver", 0xC150, "inside"),
                   ("ppumask_shadow", 0x0071, "moved"), ("cbcd_sqrt", 0xC200, "renamed"),
                   ("no_such_name", 0xD000, "unknown"), ("ppumask_shadow", 0x0072, "ok")]
