# mesen-harness: app-local rules

This repo is a **package in the vault's project process-table** (vault pointer:
`projects/game-annotation/mesen-harness/`). It obeys, outermost first:

1. **Kernel**: user-global `~/.claude/CLAUDE.md` (grounding: never fabricate,
   reuse/convert > rebuild, missing grounding means find it or ask; verification
   independence; classify before method).
2. **Project SDK**: the vault's `projects/CLAUDE.md` (zero yield is a wrong-path
   signal; a learned fact records its cause; findings flow back to the vault).
3. **This file**: the Mesen-generic layer.

## What this layer is

The **console-agnostic core** of a three-layer stack, the same shape as
`snes-decompiler` -> `koei-snes` -> title repos:

| Layer | Repo | Owns |
|---|---|---|
| core (this repo) | `mesen-harness` | `bridge.lua` socket server, lockstep, savestates via an exec hook, RAM read/hash, the Python client, the single input recorder, `--testRunner` scouts |
| console | `nes-harness` | NES pad map, battery-save wipe, NMI vector as the per-frame hook |
| game | each game's own repo | RAM maps, goals, oracles (Zelda first) |

**Nothing console- or game-specific belongs here.** If a change needs to know
what an NES pad or a Zelda RAM byte is, it goes down a layer. The core takes
the per-frame hook address and the input encoding as parameters.

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
- **Savestate calls run from an exec callback** (Mesen refuses them elsewhere).

## Ground truth

- Mesen 2's Lua API: the `MesenLuaApiDoc.html` shipped beside `Mesen.exe` (and
  the Mesen 2 source). Do not write API names or semantics from memory.
- The emulator executable's location is machine-specific: resolve it via
  `config.toml` (gitignored; copy `config.example.toml`).
