# na1dream — architecture & where new code goes

**na1dream = the NA1 Dream Decompiler core.** The importable engine that turns
Nobunaga's Ambition (NES) bytecode into readable C, plus the gates that prove the C
is faithful. This file is the map — read it before adding a script so new code lands
in the right place instead of piling up flat in `tools/`.

## Layout

```
tools/
  na1dream/              # THE LIBRARY — importable engine (underscore module names)
    __init__.py
    nobunaga_vm.py       #   the VM/ROM model + label loading        (front-end input)
    cpu6502.py           #   the KOEI banked bus (Memory) + the 6502 core, re-exported from emu-harness
    vm_disasm.py         #   bytecode -> VM-asm listing               (pipeline stage 1)
    vm_decompile.py      #   VM-asm -> per-block C + the structurer dispatch (stage 2)
    vm_cfg.py            #   bytecode/struct CFG + the equivalence relations (the gate core)
    vm_reduce.py         #   V2 region reducer  (the per-sub FALLBACK structurer)
    dream.py             #   DREAM structurer   (the PRIMARY structurer)        (stage 3)
    vm_stack_effect.py   #   the audited per-opcode data-stack model
    vm-opcodes-v2.toml   #   package-owned data: the opcode table
    cli/                 # COMMAND ENTRY POINTS — run via `python -m na1dream.cli.X`
      decompile_all.py   #   the canonical generator: bank_NN.c / .raw.c / _vm.asm
      decompile_merged.py#   the merged PRG-keyed all_banks view
    gates/               # THE SAFETY NET — run via `python -m na1dream.gates.X`
      cfg_gate.py        #   495-sub CFG equivalence gate (--dream / --v2)
      test_vm_cfg.py     #   the 114 soundness unit-suite
      stack_audit.py     #   per-opcode stack-effect audit vs the live handlers
  attic/                 # archaeology — one-off find/map/probe scripts, kept not maintained
  lua/                   # Mesen emulator scripts (a different runtime)
  *.py                   # active standalone tools (labels, call-index, xref, assemblers, renderers)
```

Data anchors: the ROM and `mesen-labels.toml` live at the project root (`nobunaga/`);
the opcode table ships inside the package. Modules find them with `__file__`-relative
paths, so commands work regardless of the current directory.

## How to run things

Run from the **`tools/` directory** so `na1dream` resolves as a top-level package:

```
cd nobunaga/tools
py -3 -m na1dream.cli.decompile_all            # regenerate the canonical C + asm oracles
py -3 -m na1dream.cli.decompile_all --basic    # the basic goto-form (bank_NN.raw.c)
py -3 -m na1dream.gates.cfg_gate --dream       # gate the canonical hybrid output (495/495)
py -3 -m na1dream.gates.test_vm_cfg            # the 114 soundness suite
py -3 -m na1dream.gates.stack_audit           # the stack-effect audit
py -3 -m na1dream.dream --emit 15 CA9C         # inspect DREAM's output for one sub
```

## Where does a NEW script go? (the decision)

Ask, in order:

1. **Will other modules `import` it?** → it's **library code** → `na1dream/` with an
   underscore name. If it's a structurer/analysis stage, wire it through
   `vm_decompile`'s dispatch; if it's shared data, ship the table beside it in the package.
2. **Is it a command that produces a committed artifact** (an oracle, a generated page)?
   → `na1dream/cli/`, runnable as `python -m na1dream.cli.<name>`. Anchor output paths
   off `__file__`, not the cwd.
3. **Does it prove something is correct** (CFG equivalence, a model audit, a unit suite)?
   → `na1dream/gates/`. A gate exits non-zero on failure and is safe to wire into CI.
4. **Is it a one-off investigation** (find an address, dump a table, probe a shape)?
   → top-level `tools/`. When it has served its purpose, move it to `tools/attic/`.
5. **Is it a Mesen/Lua emulator script?** → `tools/lua/`.

Rule of thumb: **importable ⇒ package + underscore name; one-shot ⇒ a script in `tools/`.**
The smell that something is misfiled: needing `importlib.spec_from_file_location` or a
`sys.path` hack to reach it — that means it should have been an importable package module.
The package is renameable (everything is gate-anchored): re-running the gates proves any
move preserved behavior, so refactors here are mechanical, not risky.

## Where does OUTPUT go? (the artifact axis)

The package sorts *tools*; tools also *emit*, and that output sorts into three classes.
Keep them apart, and inside the regenerable class organize **by kind, never flat** — a
catch-all output dir is the same smell as a flat `tools/`.

| Class | What | Where | Versioned? |
|---|---|---|---|
| **Source** | hand-authored input/knowledge | `nobunaga/*.md` chapters, the ROM, `mesen-labels.toml`, `.xlsx` data | yes — it's input, not output |
| **Oracle** | committed, deterministic, read-like-source deliverables | **`source/`** — the recovered game source as a pipeline ladder | yes — the project's payoff; regenerate only when a tool improves |
| **Render / scratch** | regenerable from the ROM (graphics, traces, reports, dumps) | a gitignored renders tree, **in a kind subdir** | no — gitignored |

The **oracle** class is itself organized by pipeline stage under `source/` (each stage = one
abstraction level, so you can walk ROM → readable C):

```
source/
  1-asm-6502/   native 6502 disassembly (bank_NN[.asm|_labeled|_named] + the full .asm + chr_rom.bin)
  2-asm-vm/     Sea-16 VM bytecode disassembly (bank_NN_vm.asm) — written by decompile_all
  3-c-basic/    bytecode → direct goto C (bank_NN.raw.c) — the CFG-equivalence witness
  4-c/          structured C, DREAM canonical (bank_NN.c) — what you read
```
Writers: `mesen-labels.py` owns stage 1 (`--asm`); `na1dream.cli.decompile_all` writes stages
2–4 (and `--basic` writes 3). Stage 1 `*_labeled.asm` are **committed** (regenerated from the toml by
`mesen-labels.py --asm`; `--verify` exits 1 if they drift, so labels can't go silently stale).

Deciding where a new tool's output lands:

1. **A canonical artifact meant to be READ like source** (deterministic, the deliverable)?
   → a committed stage under `source/` (or its own top-level dir if it's a new kind). These are oracles.
2. **Regenerable from the ROM** (a render, a trace, an HTML report, a txt dump)?
   → the gitignored renders tree, under a **kind** subdir (`renders/<kind>/`) — not a flat
   pile in one dir. The tool owns its output subpath.
3. **Hand-authored knowledge?** → it's *source*, not output: a `.md` at the project root.

**Done 2026-06-10:** the oracle reorg above — `disasm/` + `decompiled/` consolidated into the
`source/` ladder, the duplicated VM-asm de-duped (one canonical copy in `2-asm-vm/`).

**Remaining artifact debt (convention defined, moves deferred):** `atlas/` is a flat
catch-all (misnamed — it holds animations, province renders, PPU/SRAM dumps, strategic maps,
UI previews, title CHR — not just "atlas" maps). Target: rename to `renders/` with
`{provinces, anim, ppu-sram, strategic, ui, title}` subdirs. The `nobunaga/` root also has
~22 loose generated `.txt`/`.html` mixed with the source `.md` chapters — they belong under
an `out/` tree (`out/reports/`, `out/dumps/`). Apply this convention when the emitting tools
(`render-*`, the atlas builders) are next touched; `atlas/`+`traces/` are gitignored, so the
moves are git-free — the only coupling is updating each tool's output path + regenerating.
