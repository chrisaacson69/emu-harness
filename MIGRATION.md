# MIGRATION — folding the console-RE toolset into emu-harness

The decompiler family grew one game at a time, and each game copied or rewrote what it needed.
This ledger is the audit: what exists, which jobs are done more than once, where each job should
live, and how each move will be proven not to change output. It plays the role `MIGRATION.md`
played for dj-score (vault: `research/dj-library-extraction.md`); the method is that page's §4.

**Status (2026-10-22): audit done; step 0 (packaging) and row A (label export) done.**

## Why

- **REA is the model, consoles are the scope.** A large deterministic toolset that an inference
  engine *calls*, so tokens go to inference only. Two consoles today (NES, SNES).
- **Share code instead of rewriting it.** The audit below finds the same job written up to seven
  times.
- **KOEI was the exception, and the finished exception is leverage.** Its VM is one ISA on a
  machine-independent address space behind a per-console syscall ABI (vault:
  `research/gaming/koei-snes-portable-vm.md`). In this layout it is a plugin like a CPU.

## How the inventory was made

`py -3.14 tools/audit_family.py _scratch/audit.json`. That gives files, imports, `sys.path` hacks,
and verbatim function copies across repos. Verbatim copies are only the forked half. The other half
is the same job **written independently**: those share almost no text (line similarity 0.01–0.33
on every pair checked below), so the role table was built by reading the docstrings and is folded by
**comparing outputs, never by diffing source**.

## Inventory (2026-10-22)

339 Python files, 62,657 lines, 0 parse errors, plus 16 Lua scripts.

| Repo | Files | Lines | Role today |
|---|---:|---:|---|
| emu-harness | 11 | ~1.0K | backends (Mesen, BizHawk) × machines (NES), segment search |
| nes-render | 1 | 156 | NES PPU renderer |
| koei-nes | 28 | 13.9K | KOEI NES VM toolchain (`koeivm/`), asset decoders, `games.toml` |
| koei-snes | 22 | 5.3K | KOEI SNES VM toolchain, profile-driven, own `games.toml` |
| snes-decompiler | 10 | 1.8K | 65C816 disasm/walk/lift, LoROM/HiROM, header |
| game-annotation | 21 | 3.6K | Mappy static-RE pipeline + 4 old shared tools |
| na1-decompiler | 140 | 25.6K | NA1 + the **walk pipelines** + `na1sim` + 13 Lua probes |
| ro3k-decompiler | 14 | 1.9K | ROTK + **the de facto matcher library** (see below) |
| 6 NES KOEI titles | 23 | 2.8K | mostly `native_floor.py` forks + strings/records |
| Elite-decompiler | 4 | 0.4K | label import/verify/.mlb |
| UW-decompile | 14 | 2.9K | **not a git repo**; several tools worth harvesting |
| 4 SNES titles | 51 | 3.4K | walk prep/apply, recon checks, generated enums |
| *nes-disasm6* | 4 | 2.9K | **third-party** (`mcgrew/nes-disasm6`, 2020); not scanned by the script |

## Findings: structure

1. **`ro3k-decompiler` is a library that isn't declared as one.** Seven title repos import
   `handler_match` from it through `C:\Users\…\ro3k-decompiler\tools`. Its `bc_match`,
   `native_match` and `subs` say "graduation candidate → koei-nes" in their own docstrings.
2. **`native_floor.py` was forked down a chain.** gk → na2 → rot3k2 → bk → GemFire → LEmp → UW,
   with line similarity falling 0.64 → 0.37 along it (each title copied the previous one). Seven
   NES copies; koei-snes has a separate profile-driven 65C816 version.
3. **One dependency points upward.** `koei-nes/koeivm/gates/stack_audit.py` imports `vm_assemble`
   from **na1-decompiler**, so the library depends on one of its games.
4. **Game code sits inside libraries.** `snes-decompiler/na1_datawalk.py`, `na1_dataxref.py`;
   `koei-nes/koeivm/nobunaga_vm.py`; `game-annotation/tools/vm-disasm.py` ("Nobunaga's Ambition
   VM", older than koeivm); NA1's font atlas in `nes-render`.
5. **No packaging, so nothing is portable.** Imports go through `sys.path.insert`: 5 titles
   hardcode `koei-nes\tools`, 7 hardcode `ro3k-decompiler\tools`, and na1-snes plus
   snes-decompiler hardcode `snes-decompiler\tools`. None of them works on a second machine.
6. **The walk skills bind to tools that live in one game.** `/label-walk` runs
   `PROJ/tools/label-walk-{prep,apply}.py`, which exist only in na1-decompiler. AeroBiz wrote its own
   `walk_prep` / `walk_apply` / `walk_harvest` instead.
7. **Two profile registries.** `koei-nes/tools/games.toml` and `koei-snes/tools/games.toml`, with
   different code reading each.
8. **Graduation left originals behind.** na1's `render-portrait`, `render-strategic-map`,
   `render-text` and `run-animation` coexist with koei-nes `assets/` versions (similarity
   0.11–0.29). It's unknown which is current.

## Findings: the role table

Each row is one job. **Copies** counts implementations, forked or independent.
The **oracle** is how the fold is proven to change nothing.

| # | Job | Copies (repo:file) | Proposed home | Oracle for the move |
|---|---|---|---|---|
| A | **Emulator label export** (`.mlb`, workspace JSON) | koei-nes `gen_mlb_nes` · koei-snes `gen_mlb` · Elite `gen_mlb` · UW `export_mlb` · mappy `mappy_mlb` · na1 `mesen-labels` · game-annotation `mesen-labels`, `mesen-workspace` | `backends/mesen/labels` over `core/symbols` | byte-identical `.mlb` per repo |
| A2 | Symbol registry + merge | `mesen-labels.toml` (na1, ro3k, Elite, UW) · mappy `labels.toml`/`vars.toml` · koei-snes `symbols.py` · merge: ro3k `merge_data_labels`, mappy `merge_labels`, UW `gen_labels` (1,275 lines), AeroBiz `gen_data_labels` | `core/symbols` (one schema) | round-trip each repo's toml unchanged |
| A3 | Apply labels to a listing | game-annotation `asm-relabel` · Elite `apply_labels` · mappy `asm_gen` · UW `stamp_prg` | `passes/listing` | identical `.asm` |
| B | **6502 disassembly** | koei-nes `disasm6502` (imported by 24 files) · ro3k `subs` · rot3k2 `disn` · UW `disasm_native` (control-flow-following, paged) · GemFire `inbank_native` · *nes-disasm6* (third-party) | `cpu/m6502` | identical listings over each title's banks |
| C | 6502 CPU emulation | koei-nes `koeivm/cpu6502` · ro3k `emulator_rotk` · koei-nes `koei_vm` (emulate) | `cpu/m6502` | na1's `test-cpu6502` / `test-dispatcher` + value-oracle |
| D | **Native floor** (enumerate + name the kernel under the VM) | 7 NES forks (gk, na2, rot3k2, bk, GemFire, LEmp, UW) · koei-snes `native_floor` | `vm/koei/native_floor`, profile-driven | each title's current output, all 7 |
| E | **Cross-title matchers** | ro3k `handler_match`, `bc_match` (gk has a verbatim copy), `native_match` · GemFire `multi_oracle` | `vm/koei/match` | same match tables |
| F | **KOEI VM toolchain** | koei-nes `koeivm/*` + `koei_vm.py` · koei-snes `vm_*` · na1 `vm_assemble`, `vm-roundtrip`, `analyze-vm-opcodes`, `classify-handlers`, `gen-opcode-appendix`, `vm-opcode-labels` · wrappers: ro3k `vm_disasm_rotk`, `vm_decompile_rotk`, na1 `na1dream` alias, UW `disasm_fixed_bytecode` · opcode tables: `vm-opcodes-v2.toml`, `vm_opcode_names.toml` · retired candidate: game-annotation `vm-disasm` | `vm/koei` + `machines/{nes,snes}` syscall bindings | every title's decompiled C byte-identical |
| G | **DREAM structuring** + its gates | koei-nes `dream.py` (1,918) · koei-snes `vm_struct` (159) · snes-decompiler `native_decompile` (602, a port) · gates: `cfg_gate`, `stack_audit`, na1 `detect-prop-clobber`, `v2-corpus`, `value-oracle` | `passes/structure` over a shared IR | decompiled C byte-identical; na1 V2 corpus |
| H | Call graphs | bk `bank_callgraph` · GemFire `call_hierarchy` · UW `call_graph` · mappy `call_graph` · na1 `native-call-index`, `driver-call-index` · koei-snes `vm_callgraph` · snes-decompiler `native_callgraph` · rot3k2 `bank_hierarchy`, `native_bytecode_called` | `passes/callgraph` | identical edge sets |
| I | Walk ordering (leaves first) | bk `walk_order` · UW `walk_bank` · ro3k `bc_layers` · na1 `bank-ground-order`, `cluster-anon-subs` · koei-snes `gen_layers` | `walk/order` | identical worklists |
| J | **Walk prep / apply / guard** (the skills' deterministic half) | na1 `label-walk-*`, `var-walk-*`, `walk_guard` · AeroBiz `walk_prep`, `walk_apply`, `walk_harvest` · vault `.claude/skills/*/scripts` | `walk/` (one per walk kind) | replay a past batch: same toml diff |
| K | Data xref | na1 `data-xref`, `rom-xref` · koei-nes `data_xref` · mappy `var_xref` · rot3k2 `table_frontier` · koei-snes `data_walk` | `passes/xref` | identical xref reports |
| L | Strings | bk + GemFire `pure_strings` (verbatim) · bk `ref_strings` · rot3k2 `extract_strings`, `strdump` · UW `dump_strings` · koei-snes `strings_snes` · na1 `ui-strings` | `passes/strings`; text encoding in the profile | identical string dumps |
| M | Records / data dumps | koei-nes `dump_data` ~ ro3k `dump_data` (0.45) · bk `dump_records` · UW `dump_records`, `field_census` · koei-nes `RECORD_SCHEMAS.md` | `passes/records` | identical dumps |
| N | **Address model** (ROM offset ↔ CPU address, banks) | UW `prg.py` ("PRG offset is the label identity") · koei_vm `game_cfg` ROM resolver · snes-decompiler `mapper`, `header_parse` | `machines/{nes,snes}/address` | self-tests + every title resolves the same |
| O | Rendering + asset decode | nes-render · koei-nes `assets/*`, `chr_extract` · na1 `render-*` (17) · mappy `render_level` | `machines/nes/ppu` (generic); KOEI formats in `vm/koei/assets` | identical PNGs |
| P | Live emulator work | emu-harness backends · na1 `tools/lua/*` (13), `capture-test`, `mesen-dump`, `run-effect` · mappy `demo_in_real_game` · UW `trace_boot` | `backends/mesen` (+ probes); game Lua stays in the game | same SRAM / trace from same inputs |
| Q | Trace decoding | na1 `combat-trace-decode`, `extract-chr-from-trace`, `title-nametable` · emu-harness `trace_diff` | `machines/nes/trace` | identical reconstructions |
| R | **Static RE of native (non-VM) games** | mappy `trace_codegraph`, `static_expand`, `jump_tables`, `pointer_tables`, `stored_pointers`, `label_leaves`, `obj_record` | `cpu/m6502` + `passes/*` | Mappy's committed tables reproduce |

**Stays in the game repo (game-specific, by design):** `na1sim/`, na1's site/wiki builders and
`lowering-atlas/`, per-fief renders/analyses, `econ_sim`, ROTK `atlas/`, the AeroBiz
`recon_checks/`, generated `data/*/enums.py`, Elite's `import_labels` / `verify_rom`, mappy's
`mappy_search` adapter, game Lua probes, and every `attic/` probe. A game repo becomes
**a profile + adapters + findings**.

## Proposed layout (fills in as moves land; no empty scaffolding)

```
emu_harness/
  core/        symbols (one toml schema), profiles (one games.toml schema), ROM loading
  cpu/         m6502, w65c816           disasm, emulation, native lifter
  vm/          koei                     disasm, opcode tables, lift, native_floor, match, assets
  machines/    nes, snes                address model, PPU/render, pad, syscall bindings, traces
  passes/      structure, callgraph, xref, strings, records, listing   (ISA-independent)
  walk/        order, label, var, data  (the skills' deterministic half)
  backends/    mesen, bizhawk           live control + debugger export (.mlb)
```

Import direction: `passes`/`walk` → `core`; `vm` → `cpu` + `core`; `machines` → `core`;
`backends` know no console. Enforced by an import contract (as dj-score), with a planted violation
to prove it fails.

## Proposed order of moves

0. ✅ **Packaging (2026-10-22).** `pyproject.toml`; editable install into `py -3.14` (`py -3` is
   3.9 on the work machine, kept for unrelated tools). Import contracts run in pytest, each shown
   to fail on a planted violation. The three `sys.path` hacks pointing at emu-harness are gone
   (`tools/replay_log`, `spike/mesen/smoke`, game-annotation `mappy_search`). **Rule from here:**
   a tool that moves in is imported, never path-hacked, and the family runs on `py -3.14`.
1. ✅ **A: label export (2026-10-22).** See "Row A done" below.
2. **D: native floor.** 7 forks → one profile-driven tool. The oracle is each title's current output.
3. **E: matchers.** Dissolves the ro3k pseudo-library and its 7 hardcoded paths. *Generic half done
   2026-10-22:* `cpu/m6502/signature.py` (relocation-invariant signature + best_match),
   `machines/nes/memory.fixed_bank` (last 16K of PRG; `handler_match.bank15` was wrong on 512K UW and
   128K Elite), `core/symbols.names_by_address`. ro3k `handler_match` delegates. Oracle: 19 consumer
   outputs byte-identical (all 7 native_floor forks, table + `--toml`; handler_match x2;
   native_match; GemFire multi_oracle; UW native_banks), and the 3 functions agree on all 10 ROMs and
   registries. KOEI half (syscall-table decode, bc_match, multi-oracle naming) moves with D.
4. **B/C/N: CPU + address model** for both consoles. *B started 2026-10-22, pulled forward because
   D and E import it:* koei-nes `disasm6502.py` moved to `emu_harness/cpu/m6502/disasm.py`, koei-nes
   keeps a re-export (all public names, checked). Oracle: `validate` vs NA1's committed `bank_15.asm`
   (6,634 instructions, 0 mismatches) plus 73 bank listings of 5 ROMs (NROM/MMC1/MMC5),
   byte-identical through both import routes. Still to fold into B: ro3k `subs`, rot3k2 `disn`,
   UW `disasm_native`, GemFire `inbank_native`, and the nes-disasm6 decision.
5. **F: the KOEI VM**: koei-nes + koei-snes → `vm/koei` + two bindings. The largest move.
6. **G: DREAM.** Three implementations at very different maturity; needs an IR contract. Last.

## Decisions (Chris, 2026-10-22)

- **emu-harness is the home.** Splitting by dependency was right for dj-score/dj-render because
  those two shared only the topic; here the tools share the subject *and* most of their imports.
- **UW-decompile is now a git repo** (local, `d4bb7a3`, 126 files; ROM/manual/RAM dumps ignored).
  It is a title, so it stays a title; its tools are harvest candidates. *"I am always up for trying
  to improve a disassembler"*: `disasm_native` (control-flow-following, paged) is a row-B input.
- **nes-disasm6: decide at row B,** when the tools that read its output format are reviewed.
- **Renders: na1 is the oracle, koei-nes `assets/` is the generalization attempt; test the port
  against na1, keep na1's final output, not its early mistakes.** Measured below.
- **The target for row O is a virtual canvas:** a game decodes its map format into
  (CHR, nametable of any size, attribute plane, palette) and `machines/nes/ppu` draws it. Chris's
  test case: the whole Zelda 1 overworld from its map data. `nes-render.render_tiles` already takes
  arbitrary `cols × rows`, so the core exists; what's missing is the contract and a non-KOEI user.

### Principles confirmed after row A (Chris, 2026-10-22)

- **With N disagreeing copies, the oracle is correctness, not agreement.** Matching six tools
  that disagree is undefined; the reference is the lower artifact they all feed (for labels:
  Mesen's own parser). Agreement with the old tools is how the *differences* get found and named,
  not the pass condition. *"The oracle is less does it create the same output as the 6 different
  tools, and more does it create correct output."*
- **One model, several output formats.** A formatter that can write more than one version
  (Mesen today; patch tables, other debuggers, automation) is the point of the Symbol layer.
- **Title-specific means really title-specific.** A tool stays in a title repo only if no sibling
  can use it. The portrait generator already works on Genghis Khan, so it is a KOEI tool
  (`vm/koei/assets`), not an NA1 one. A title repo's output is the decompiled code, the assets
  it discovers and the write-ups of the game's rules.

## Row O measured: na1 render tools vs their koei-nes ports (2026-10-22)

Harness: run both at scale 1 on the same ROM. **EXACT** = identical RGB. **STRUCTURE** = identical
under a 1:1 colour map (only the palette differs). First, na1 against its own committed PNGs:

| na1 output | Reproduces? |
|---|---|
| `daimyo-anthology-17/50.png`, `daimyo-random-36.png`, `font-glyphs.png` | **EXACT** |
| `strategic-map-50-stitched.png` (faithful stitch, scale 3) | **EXACT** |
| `strategic-map-17/50.png` contact sheets (committed 06-12) | no: stale, older than the 06-17 alignment fix |
| `animations/frames/anim-12-frames.png` | no: stale, older layout (current na1 crops to the sprite) |

Then the port against na1:

| Pair | Verdict | Read |
|---|---|---|
| preset portraits (mapid 0, 5, 24, 49) | **EXACT** | port is good |
| text (3 strings) | **STRUCTURE**: white `(255,255,255)` → `(236,238,236)` | port is good; the colour is nes-render's real NES white (intended) |
| composite faces (3 codes) | **DIFFERENT** | **port bug:** `render_composite_face` lays the 36 tiles out in CHR order (`render_chr_atlas`) and never applies the face layout map, so the face is scrambled |
| strategic map, all 9 sections | **DIFFERENT** | **port bug:** tiles match, attribute → sub-palette decode is wrong (black holes, olive for brown) |
| animation 10, 12 | same 10/20 frames and shapes; full canvas and a different palette | **undecided:** neither palette is proven; the truth is an emulator-rendered frame (Mesen `emu.takeScreenshot` via this harness) |

**So the canon is mixed, and the fold for row O is:** take koei-nes's *structure* (KOEI layer
locates data, nes-render draws pixels, per-title config) and na1's *verified behaviour*. Fix the
composite-face layout and the strategic attribute decode until each matches na1 EXACT or
STRUCTURE, settle the animation palette against an emulator frame, then retire the na1 copies.
Nothing outside koei-nes imports `strategic_map` or `text` today, so fixing them breaks no consumer.

## Row A done: label export (2026-10-22)

**New:** `core/symbols.py` (Symbol + region vocabulary + registry reader that reports defects),
`machines/nes/memory.py` (Cart from the iNES header; locate), `backends/mesen/labels.py` (Mesen's
own export format), `tools/gen_mlb.py`, `tools/mlb_compare.py`, `tests/test_labels.py`.
**Retired:** koei-nes `gen_mlb_nes.py` (committed once for history, then removed), Elite
`gen_mlb.py`, UW `export_mlb.py`. **Rewired:** na1 `mesen-labels.py --mlb`, mappy `mappy_mlb.py`,
koei-snes `gen_mlb.py` (keeps its module merge; writes through the shared writer).

**Format, from Mesen 2's source** (CodeLabel / LabelManager): long type names, `X4` hex, comment
omitted when blank, colons allowed in comments, newlines as `\n`, names must match
`^[@_a-zA-Z]+[@_a-zA-Z0-9]*$`, and **the last label at a key wins** on import.

**Oracle.** Bytes were the wrong oracle (dialects differ while Mesen shows the same thing), so
`tools/mlb_compare.py` loads both files with Mesen's import rules. Golden = each old producer
re-run on 2026-10-22 (koei-nes `ce4f291` + uncommitted `gen_mlb_nes.py`, na1, Elite, UW, mappy,
koei-snes at their HEADs); 8 of 11 equalled the committed `.mlb`, the 3 others were stale
commits (na1, UW: labels added since; Gemfire-snes: rot3k2's VM entries, a known fixed bug).

| Title | Mesen labels old / new | Identical | Differences (all named) |
|---|---|---:|---|
| Elite | 1693 / 1693 | 1693 | none |
| rot3k2-snes | 592 (+3 rejected) / 592 | 592 | 3 labels Mesen silently rejected are now reported |
| LEmp, rot3k2, bk, GemFire, na2 | as before | 907–3066 | `:` restored in 1,899 comments (koei-nes wrote `;`); 548 comments no longer truncated by inner quotes (na2 538, bk 10); GemFire +1 label at `$9D2C` koei-nes skipped |
| na1 | 1371 / 1371 | 1207 | 156 comments keep the game's line breaks; 8 conflicts (below) |
| UW, Mappy, Gemfire-snes | | | 1 / 9 / 1 conflicts (below); Gemfire-snes had **47 labels Mesen rejected** (names starting with a digit), now reported |

**What the registries hold that nobody could see (fix in A2; each needs a decision):**
- **8 of 10 `mesen-labels.toml` are not valid TOML:** na2 538 bad lines, UW 244, bk 11, gk 2;
  duplicate sections (bk 8, UW 5, ro3k 1); duplicate addresses (ro3k 45 same-name).
- **Conflicting names for one address:** na1 `$C7C9` `music_next_voice`/`music_advance_voice` and
  4 fixed-bank aliases; na2 `$603A` `blink_period`/`menu_box_enabled`; rot3k2 `$EF93`
  `ext_mul32`/`umul32` + 2; GemFire `$A3F4` `unit_name_table`/`msg_horsemen`; UW bank 17
  `fleet_leaf_8b0a` (`$8B0A`) vs `nat_17_0b0a` (`$AB0A`), one byte through two windows.
- **Mappy's object record contradicts its variables:** `vars.toml` says `$0401` = `obj_y`, the
  record template says +1 = `obj_x_hi` (9 bytes of the `$0400` object disagree).
- **Gemfire-snes profile:** `vm_interp_loop` and `vm_run_entry` both `$2340`.
- Mesen showed the *last* of each conflicting pair; the new writer keeps the *first* and prints
  the clash, so the choice is visible until the registry is fixed.

**Also found:** na1's `*_labeled.asm` are untracked since `2d2e7ef` although its `.gitignore`
and `mesen-labels.py` say they are versioned (and this session's `--asm` run overwrote the
stale untracked copies with a current regen; the last committed version is `2d2e7ef^`).

## Row A2 done: the registries are valid TOML (2026-10-22)

**Cause.** ~20 hand-written f-strings across 9 repos (`f'... comment = "{c}" }}'`) never escaped
`"` or `\`. na1's walk writers (`toml_escape`) were right, which is why na1 had 0 bad lines; the
damage came from per-title one-off tools (string sweeps, UW `gen_labels`, native-floor printers).
Those writers are listed by `grep 'comment = "{'` and get replaced when their rows fold (I, J, L).
New: `core.symbols.format_entry()` (spec-correct escaping) and `tools/repair_registry.py`.

**Mechanical repair** (rewrite invalid entries, drop exact duplicates, merge repeated sections):
na2 538 lines + 5 duplicates, UW 244, bk 11 + 8 sections, ro3k 45 duplicates + 1 section, gk 2.
**Resolved on evidence** (one entry per address, both comments kept):

| Address | Kept | Evidence |
|---|---|---|
| na2 `$603A` | `menu_box_enabled` | bytecode: the menu toggle stores to `$603A` (`$B257`), blink speed to `$603C` (`$B1D0`), where the correct `blink_period` already was |
| na1 `$C7C9` | `music_advance_voice` | used in source/docs (2 files vs 0); what Mesen and the decompiler showed |
| rot3k2 `$EF93/$EFC2/$F1C1` | `umul32/sdiv32/clear_a_high16` | NA2-family names, already in rot3k2's C; ext-op roles kept in the comment |
| GemFire `$A3F4` | `unit_name_table` | the address is the table; "Horsemen" is its first entry |
| na1 `$C173/$C7AC/$C817`, rot3k2 `$E0EA` | (same name) | two descriptions joined |

**Proof, against every reader** (not just Mesen): `.mlb` before/after (byte-identical or
label-identical, except the resolutions); koei-nes `bank_scoped_labels` per bank,
`build_bank_call_map`, `build_syscall_names`; the NA1-lineage flattened view. Only change:
GemFire `$A3F4` in the decompiler's view (`msg_horsemen` -> `unit_name_table`; no committed file
used the old name).

**Result: 9 of 10 registries are valid TOML.** UW keeps its 5 repeated sections, on purpose: the
koei-nes decompiler resolves cross-section collisions by *file order*, and merging flipped UW bank 8
`$A003` from `win_open` (right: bank 1 is co-resident at `$A000`) to `nat_8_0003` (bank 8 seen
through the other window). The real fix is row N: a `[prg.bankN]` label applies only in the window
that bank is mapped to (UW's own `prg.py` already keys labels by PRG offset). Same class, still
open: na2's 3 window collisions at `$18056/$180A7/$1D03B` and na1's fixed-bank aliases filed under
two sections (reported by `gen_mlb`).

**Reader debt found:** koei-nes `bank_scoped_labels` reads comments with `"([^"]*)"`, so an
escaped quote ends the comment at its backslash. No output uses those comments today (all
consumers keep the name only); fix it when koei-nes reads through `core.symbols` (row F).

## Open questions (Chris)

- **nes-disasm6** (row B, deferred): still read by `asm-relabel` / `zp-harvest`?
- **na1-snes-decompiler** has no vault pointer (it is now in the path resolver).
- Do the koei-nes and koei-snes repos get **archived** after step 5, as nes-harness was?
- UW-decompile: GitHub remote, public or private? (Its siblings GemFire and LEmp are public.)
