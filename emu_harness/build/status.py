"""status: where a project stands, read from its re-roadmap.toml against the phase table.

    py -3.14 -m emu_harness status <project> [--json]

`<project>` is a folder: a path, or a name next to emu-harness (the sibling convention, as
games.toml's `repo`), e.g. `game-annotation/mappy`. The project keeps its own state in
`<project>/re-roadmap.toml` (template: roadmap.template.toml beside this file); emu-harness keeps
only the phases (phase_table.toml) and their manual (PHASES.md).

A phase is where to start when it is not done, when it is done but its gate is stale (a commit
after the one the gate ran on touched a library path that gate depends on), or when it is done
with an open revisit against it. Staleness is asked of
git; when git can't answer, the phase says "unknown", never fresh.
"""
import subprocess
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]                  # build -> emu_harness -> emu-harness
SIBLINGS = REPO.parent
PHASE_TABLE = HERE / "phase_table.toml"
TEMPLATE = HERE / "roadmap.template.toml"
ROADMAP = "re-roadmap.toml"

STATUSES = ("done", "partial", "todo", "n/a")
TRACKS = ("vm", "native")


class RoadmapError(Exception):
    pass


def load_phases():
    return tomllib.loads(PHASE_TABLE.read_text(encoding="utf-8"))["phase"]


def phase_paths(phase, track):
    paths = phase["paths"]
    return list(paths.get("common", [])) + list(paths.get(track, []))


def resolve_project(arg):
    """A folder given as a path (absolute, or relative to the cwd), else a sibling of emu-harness."""
    p = Path(arg)
    for cand in (p, SIBLINGS / p):
        if cand.is_dir():
            return cand.resolve()
    raise RoadmapError(f"no project folder '{arg}' (tried it as a path and under {SIBLINGS})")


def load_roadmap(project_dir):
    path = project_dir / ROADMAP
    if not path.is_file():
        raise RoadmapError(f"{path} does not exist: copy {TEMPLATE} there and fill it from the project's evidence")
    try:
        rm = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise RoadmapError(f"{path}: {e}") from e
    validate(rm, path)
    return rm


def validate(rm, where):
    """Refuse a roadmap the status could misread: a typo'd key would otherwise read as "todo"."""
    keys = [p["key"] for p in load_phases()]
    track = rm.get("project", {}).get("track")
    if track not in TRACKS:
        raise RoadmapError(f"{where}: [project] track must be one of {TRACKS}, got {track!r}")
    for k, ph in rm.get("phase", {}).items():
        if k not in keys:
            raise RoadmapError(f"{where}: [phase.{k}] is not a phase (known: {', '.join(keys)})")
        if ph.get("status") not in STATUSES:
            raise RoadmapError(f"{where}: [phase.{k}] status must be one of {STATUSES}, got {ph.get('status')!r}")
    for i, r in enumerate(rm.get("revisit", [])):
        if r.get("phase") not in keys:
            raise RoadmapError(f"{where}: revisit {i + 1} ({r.get('item', '')[:40]!r}) names no phase key")
        if r.get("status") not in ("open", "closed"):
            raise RoadmapError(f"{where}: revisit {i + 1} status must be 'open' or 'closed'")


def _git(*args):
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def head():
    rc, out = _git("rev-parse", "--short", "HEAD")
    return out if rc == 0 else None


def staleness(toolset, paths):
    """('fresh' | 'stale' | 'unknown', detail) for a gate run at emu-harness commit `toolset`."""
    if not toolset:
        return "stale", "ran before emu-harness"
    rc, _ = _git("rev-parse", "--verify", "--quiet", f"{toolset}^{{commit}}")
    if rc != 0:
        return "unknown", f"commit {toolset} is not in emu-harness"
    rc, log = _git("log", "--format=%h", f"{toolset}..HEAD", "--", *paths)
    if rc != 0:
        return "unknown", "git log failed"
    commits = log.split()
    rc, dirty = _git("status", "--porcelain", "--", *paths)
    if rc != 0:
        return "unknown", "git status failed"
    if commits:
        return "stale", f"{len(commits)} commit(s) since {toolset} touch its tools ({', '.join(commits[:5])})"
    if dirty:
        return "stale", f"uncommitted changes in its tools ({len(dirty.splitlines())} file(s))"
    return "fresh", f"no change to its tools since {toolset}"


def status(project_dir):
    """The project's phase table as records, plus where the next session starts."""
    rm = load_roadmap(project_dir)
    track = rm["project"]["track"]
    revisits = rm.get("revisit", [])
    rows, start = [], None
    for ph in load_phases():
        k = ph["key"]
        entry = rm.get("phase", {}).get(k, {})
        st = entry.get("status", "todo")
        row = {
            "key": k, "name": ph["name"], "status": st,
            "gate": entry.get("gate", ""), "checked": entry.get("checked", ""),
            "toolset": entry.get("toolset", ""), "evidence": entry.get("evidence", []),
            "open_revisits": sum(1 for r in revisits if r["phase"] == k and r["status"] == "open"),
        }
        if st in ("done", "partial"):
            row["freshness"], row["freshness_detail"] = staleness(row["toolset"], phase_paths(ph, track))
        else:
            row["freshness"], row["freshness_detail"] = "", ""
        rows.append(row)
        if start is None:
            if st in ("todo", "partial"):
                start = {"phase": k, "reason": st}
            elif st == "done" and row["freshness"] != "fresh":
                start = {"phase": k, "reason": f"gate {row['freshness']}: {row['freshness_detail']}"}
            elif st == "done" and row["open_revisits"]:
                # An open item contests the gate's result; earliest phase first (foundation early).
                start = {"phase": k, "reason": f"{row['open_revisits']} open revisit(s)"}
    return {
        "project": rm["project"], "folder": str(project_dir), "emu_harness": head(),
        "phases": rows, "start": start,
        "revisits": revisits, "next": rm.get("next", {}).get("session", ""),
    }


def render(s):
    p = s["project"]
    out = [f"{p.get('name', '?')}  [{p['track']}, {p.get('machine', '?')}]  {s['folder']}",
           f"emu-harness at {s['emu_harness'] or '?'}", ""]
    w = max(len(r["name"]) for r in s["phases"])
    for r in s["phases"]:
        mark = "->" if s["start"] and s["start"]["phase"] == r["key"] else "  "
        fresh = f" ({r['freshness']})" if r["freshness"] else ""
        rev = f"  [{r['open_revisits']} open]" if r["open_revisits"] else ""
        when = f"  {r['checked']}" if r["checked"] else ""
        out.append(f"{mark} {r['name']:<{w}}  {r['status']:<7}{fresh}{when}{rev}")
        if r["gate"]:
            out.append(f"     {'':<{w}}  gate: {r['gate']}")
        if r["freshness"] in ("stale", "unknown"):
            out.append(f"     {'':<{w}}  {r['freshness_detail']}")
    open_ = [r for r in s["revisits"] if r["status"] == "open"]
    out.append("")
    out.append(f"start: {s['start']['phase']} ({s['start']['reason']})" if s["start"] else "start: every phase done and fresh")
    if open_:
        out.append(f"open revisits ({len(open_)}):")
        out += [f"  [{r['phase']}] {r['item']}  ({r.get('source', '')})" for r in open_]
    if s["next"]:
        out.append(f"next session: {s['next']}")
    return "\n".join(out)
