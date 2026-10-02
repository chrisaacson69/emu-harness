# emu-harness

Drive emulators from Python in lockstep: send inputs, advance frames, save/load
state, read RAM, and replay input logs from power-on. Built for replay verification,
search and testing of game AI.

Emulators (**backends**) and consoles (**machines**) are independent plug-ins:

| | Status |
|---|---|
| `backends/mesen` | [Mesen 2](https://github.com/SourMesen/Mesen2): Lua socket bridge + Python client, headless via `--testRunner`, per-frame RAM trace |
| `backends/bizhawk` | [BizHawk](https://github.com/TASEmulators/BizHawk): `.bk2` movie RAM-trace script (reference oracle for cross-emulator drift) |
| `machines/nes` | pad map, battery-save wipe, power-on RAM fill |

Tools: `tools/replay_log.py` (replay an input log, report RAM SHA-1) and
`tools/trace_diff.py` (find where two per-frame RAM traces diverge).

Game adapters live in each game's own repo. See `CLAUDE.md` for the layout and rules.

Setup: copy `config.example.toml` to `config.toml` and set this machine's paths.
Requires Python 3.11+ (`tomllib`). No ROMs are included; you supply your own.

*Formerly `mesen-harness` + `nes-harness` (merged 2026-10-02).*
