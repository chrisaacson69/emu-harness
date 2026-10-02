# nes-harness: app-local rules

This repo is a **package in the vault's project process-table** (vault pointer:
`projects/game-annotation/nes/harness/`). It obeys, outermost first:

1. **Kernel**: user-global `~/.claude/CLAUDE.md`.
2. **Project SDK**: the vault's `projects/CLAUDE.md`.
3. **This file**: the NES layer.

## What this layer is

The **console layer** of the harness stack. It depends on `mesen-harness` (the
core; sibling `../mesen-harness`) and is depended on by NES game adapters, which
live in each game's own repo (Zelda first, as the oracle).

Owns, and only owns, what is true of every NES cart:

- the controller button map (A, B, Select, Start, Up, Down, Left, Right);
- wiping the battery save before a replay (a stale `.sav` silently changes the run);
- the per-frame hook: the **NMI vector, read from the ROM at `$FFFA`**, never hardcoded.

Nothing game-specific belongs here. A RAM address with a meaning is an adapter's.

## Ground truth

- NES hardware facts: the [NESdev Wiki](https://www.nesdev.org/wiki/). Do not write
  vector addresses, mapper or controller behavior from memory.
- The vault pages `research/nes/` and the design page
  `research/gaming/zelda-ai-speedrun-harness.md`.
