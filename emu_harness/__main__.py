"""py -3.14 -m emu_harness <verb> <project> [--json]: the build tool's one entry point (PHASES.md)."""
import argparse
import json
import sys

from emu_harness.build import status as status_mod


def _status(args):
    try:
        s = status_mod.status(status_mod.resolve_project(args.project))
    except status_mod.RoadmapError as e:
        sys.exit(f"status: {e}")
    print(json.dumps(s, indent=2) if args.json else status_mod.render(s))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="emu_harness", description="The RE build tool (PHASES.md).")
    verbs = ap.add_subparsers(dest="verb", required=True)
    st = verbs.add_parser("status", help="where a project stands and which phase the next session starts at")
    st.add_argument("project", help="project folder: a path, or a name next to emu-harness (e.g. game-annotation/mappy)")
    st.add_argument("--json", action="store_true", help="records, not prose")
    st.set_defaults(fn=_status)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
