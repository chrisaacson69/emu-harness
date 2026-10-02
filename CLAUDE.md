# emu-harness: app-local rules

This repo is a **package in the vault's project process-table** (vault pointer:
`projects/game-annotation/emu-harness/`). It obeys, outermost first:

1. **Kernel**: user-global `~/.claude/CLAUDE.md` (grounding: never fabricate,
   reuse/convert > rebuild, missing grounding means find it or ask; verification
   independence; classify before method).
2. **Project SDK**: the vault's `projects/CLAUDE.md` (zero yield is a wrong-path
   signal; a learned fact records its cause; findings flow back to the vault).
3. **This file**: the harness.

## What this is

Drive emulators from Python, in lockstep, for replay, search and testing. Two
**independent plug-in axes**, not a stack:

- **backends**: one emulator each (Mesen 2, BizHawk). Know the emulator; know no console.
- **machines**: one console each (NES). Know the console; know no emulator.
- **game adapters** (RAM maps, goals, oracles) live in **each game's own repo**, never here.

Which backend supports which machine is configuration, not code structure: both
Mesen 2 and BizHawk are multi-system, with a different core (and different accuracy
and Lua surface) per system. Check the installed version before relying on a pairing.
A machine must not assume a backend.

*(History: began 2026-10-01 as two repos, `mesen-harness` (core) and `nes-harness`
(console layer), stacked emulator-at-the-bottom. Merged 2026-10-02 when BizHawk
became a second backend, which made emulator and machine orthogonal. Decision record:
vault `research/gaming/zelda-ai-speedrun-harness.md`, "Decision 2".)*

## Layout and directory habits

```
emu_harness/               the importable package: product code only
  backends/<emulator>/     client + the Lua that runs inside that emulator
  machines/<console>/      pad map, vectors, battery saves for that console
tools/                     CLI entry points (thin; logic lives in the package)
spike/<backend>/           probes and smoke tests: exploratory, not product
config.example.toml        committed template; config.toml is per-machine, gitignored
_scratch/                  emulator logs and run output, gitignored
ref/                       third-party reference runs, gitignored (no license, never vendored)
```

- **Import rules.** `machines/` never imports a backend's internals and `backends/`
  never imports `machines/`. Shared contract code goes in `emu_harness/core/`.
- **Known debt, stated:** `machines/nes` currently subclasses the Mesen backend and
  `wipe_battery` knows Mesen's `Saves/` folder. Pay it when the **second backend
  gets a Python client** (BizHawk has only a trace script so far): extract the shared
  contract into `core/` then, from two real implementations, not before from one.
- **No empty scaffolding.** A directory exists when it has a file that needs it.
- **No ROMs, saves or states, ever** (`.gitignore`); you supply your own.
- **Python is `py -3.14`** (the client needs `tomllib`; a bare `py -3` may resolve
  to an older install and fail on import).

## Rules built in from day one

These come from the AIBeatsZelda journal; the design and the evidence are on the
vault page `research/gaming/zelda-ai-speedrun-harness.md`.

- **Lockstep.** The bridge blocks between commands; the emulator never free-runs
  while the client is thinking.
- **Input is applied per emulated frame, not per poll.** Lag frames (no pad poll)
  still count, or a movie from another emulator will not line up.
- **One input recorder.** Every input that reaches the emulator goes through the
  one recorder, so any run can be replayed from power-on.
- **Clean slate per attempt.** Wipe battery saves and start from a known state.
- **Read RAM after the frame settles**, not mid-frame.
- **A run reports itself.** Success is the exit code plus an output file the run
  wrote, never whether a window or process is visible. (Chris minimizes and closes
  emulator windows; reading that as failure caused retry loops, 2026-10-02.)
- **Never capture the screen.** Work EDR flags it; a hook blocks it. If a frame is
  needed, the emulator renders its own (Mesen `emu.takeScreenshot`).
- **Mesen: savestate calls run from an exec callback** (Mesen refuses them elsewhere).

## Ground truth

- Mesen 2's Lua API: the `MesenLuaApiDoc.html` shipped beside `Mesen.exe` (and
  the Mesen 2 source). BizHawk's: its own Lua docs and source for the installed
  version. Do not write API names or semantics from memory.
- NES hardware facts: the [NESdev Wiki](https://www.nesdev.org/wiki/). Do not write
  vector addresses, mapper or controller behavior from memory.
- Emulator executables are machine-specific: resolve them via `config.toml`.
