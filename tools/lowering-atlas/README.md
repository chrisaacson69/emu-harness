---
status: active
created: 2026-06-05
---
# Lowering Atlas — what shape does each C construct take when compiled?
> Compile *known* C control-flow constructs through a dumb, non-optimizing compiler and catalog the
> CFG signature each one leaves. That catalog **is** the finite inverse-lowering ("atom") table the
> [nobunaga](https://github.com/chrisaacson69/na1-decompiler/tree/main/nobunaga) decompiler is trying to discover one failing sub at a time. Source the
> rules forward, instead of making every reverse-engineered function its own exception.

**Moved here 2026-10-29** from na1-decompiler `lowering-atlas/` (last commit there `cbad080`), because the catalog is about structured lowering in general, not NA1. Its checking role, decompiled C lowered back to a CFG and compared with the bytecode, is cfg_gate's (`vm_cfg.lower_goto_cfg`); this stays as the forward catalog the structurers' atoms were sourced from (`b_or` in `vm_cfg.py`, the cross-jump result in NA1's DREAM thesis).

**Served:** [nobunaga](https://github.com/chrisaacson69/na1-decompiler/tree/main/nobunaga) (NA1 — the NES reverse-engineering effort this serves).
**Read first there:** [decompiler-bottom-up-thesis](https://github.com/chrisaacson69/na1-decompiler/blob/main/nobunaga/decompiler-bottom-up-thesis.md) (the *why*) ·
[decompiler-atom-log](https://github.com/chrisaacson69/na1-decompiler/blob/main/nobunaga/decompiler-atom-log.md) (atoms found so far, by reverse search).

## The thesis, in one paragraph

NA1's bytecode engine is **100% reducible** (`probe-cfg-reducibility.py`: 495/495 subs, 0 irreducible).
A reducible CFG always has a goto-free structured form, which is *exactly* the property compiling from
a structured language guarantees. So the engine was compiled from structured source, and its decompiler
is a **reverse compiler**: invert the *finite* set of control-flow lowerings the forward compiler used
(one per `if`/`while`/`for`/`switch`/`&&`/`break`/early-`return`/…), bottom-up, until the procedure
collapses to clean C. The hard part isn't whether the table exists (proven) — it's *enumerating it* so
you know when you're **done** and can tell a real atom (clears many subs) from a 1-off (clears one).

This project enumerates it the cheap way: **run the forward direction on purpose.** Compile minimal
constructs, read the jump patterns, write them down. The NA1 census already proved the target obeys
such a table; this hands us the table itself.

## What this is NOT

- **Not NA1's compiler.** GCC will not emit NA1's `$D5`/`$D9` switch opcodes or its bytecode. We are
  cataloging **CFG shape**, not opcodes — the *space* of structured→jump lowerings and the invariants
  (one back-edge per loop; shared-return = N branches to a terminal; switch = jump-table or if-chain).
- **Not byte-matching.** GCC `-O0` always lowers `while` as jump-to-test; NA1's compiler may have made
  a different (but equally finite) choice. The deliverable is the *menu* of shapes + how to recognize
  each, so an NA1 session matches an observed sub to a known construct instead of inventing a handler.

## Method

```
corpus/*.c   one construct per function, minimal, no library calls
   │  py -3.14 tools/lowering-atlas/tools/compile-dumps.py        (gcc -O0 -fdump-tree-cfg, deterministic)
   ▼
build/*.cfg  GCC GIMPLE-CFG dumps: explicit basic blocks, edges, loop header/latch annotations
   │  py -3.14 tools/lowering-atlas/tools/cfg-signature.py
   ▼
atom-table.md   construct → normalized CFG signature, cross-checked vs what NA1's
                vm_reduce.py already inverts (confirm / missing / 1-off-risk)
```

Why GCC `-O0 -fdump-tree-cfg`: it is free, installed (msys2), non-optimizing, and emits **explicit
basic blocks + edges + loop header/latch metadata** — a cleaner CFG to read than parsing raw asm.

**Toolchain:** GCC 15.2.0 at `C:\msys64\ucrt64\bin\gcc.exe` (not on PATH; the driver locates it, override
with `$env:LA_GCC`).

## Roadmap

- **v1 (now): GCC -O0 only.** Prove the pipeline; extract the first table; cross-check vs the NA1 atom log.
- **v2: cc65 — a different goal, not a second catalog source.** cc65 (real 6502 backend) is for
  scoping whether we could **write our own bytecode runner / NA1-style toolchain** (compile to a target
  we control), NOT for cross-checking fingerprints. Parked behind the real priority below.

> **Priority is the live decompiler, not this catalog.** The atlas exists to *source* atoms; the
> payoff is landing them in `emu_harness/vm/koei/nes/vm_reduce.py` and folding subs. Don't expand the catalog
> when there's an un-written atom this table already identified.

## Hand-off contract back to NA1

Each catalogued construct becomes a candidate **pre-contraction atom** in `emu_harness/vm/koei/nes/vm_reduce.py`
(the bottom-up move: rewrite the matched subgraph to a token before the region tree runs). A signature
that matches a behind-V1 sub's shape → write the inverse, delete the rung-6 handler it obsoletes, re-run
`probe-cfg-reducibility.py`. A signature GCC produces that NA1 *never* shows (or vice-versa) is itself a
finding: it bounds which atoms are real vs which NA1-specific shapes need their own (still finite) entry.

