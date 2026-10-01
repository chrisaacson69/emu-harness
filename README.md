# mesen-harness

A console-agnostic harness that drives the [Mesen 2](https://github.com/SourMesen/Mesen2)
emulator from Python: a Lua socket bridge running inside Mesen, in lockstep with a
Python client that sends inputs, advances frames, saves/loads state and reads RAM.

This is the **core** layer. Console layers (e.g. `nes-harness`) and game
adapters build on it. See `CLAUDE.md` for the layering and the rules.

**Status:** scaffold. The first milestone is replaying a known Zelda (NES) input log
from power-on and matching its recorded end state.

No ROMs are included; you supply your own.
