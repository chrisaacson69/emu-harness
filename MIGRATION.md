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
| B | **6502 disassembly** ✅ | koei-nes `disasm6502` (imported by 24 files) · ro3k `subs` · rot3k2 `disn` · UW `disasm_native` (control-flow-following, paged) · GemFire `inbank_native` · Mappy `static_expand`, `jump_tables`, `stored_pointers`, `pointer_tables` · koei-nes `native_floor.reach` · *nes-disasm6* (third-party) | `cpu/m6502` (`disasm`, `descent`); nes-disasm6 plugin in `machines/nes` | each fork's output on its home ROM; the CPU-log benchmark |
| C | 6502 CPU emulation ✅ | koei-nes `koeivm/cpu6502` · game-annotation Mappy `trace_codegraph.TracingCPU` (the 18 missing opcodes) · (ro3k `emulator_rotk` and koei_vm's VM runner are VM-level, row F) | `cpu/m6502/cpu` | nestest golden log + na1's `test-cpu6502` / `test-dispatcher` / value-oracle, ro3k income test, Mappy trace + render |
| D | **Native floor** (enumerate + name the kernel under the VM) | 7 NES forks (gk, na2, rot3k2, bk, GemFire, LEmp, UW) · koei-snes `native_floor` | `vm/koei/native_floor`, profile-driven | each title's current output, all 7 |
| E | **Cross-title matchers** | ro3k `handler_match`, `bc_match` (gk has a verbatim copy), `native_match` · GemFire `multi_oracle` | `vm/koei/match` | same match tables |
| F | **KOEI VM toolchain** ✅ | koei-nes `koeivm/*` + `koei_vm.py` · koei-snes `vm_*` · na1 `vm_assemble`, `vm-roundtrip`, `analyze-vm-opcodes`, `classify-handlers`, `gen-opcode-appendix`, `vm-opcode-labels` · wrappers: ro3k `vm_disasm_rotk`, `vm_decompile_rotk`, na1 `na1dream` alias, UW `disasm_fixed_bytecode` · opcode tables: `vm-opcodes-v2.toml`, `vm_opcode_names.toml` · retired candidate: game-annotation `vm-disasm` | `vm/koei` + `machines/{nes,snes}` syscall bindings | every title's decompiled C byte-identical |
| G | **DREAM structuring** + its gates | koei-nes `dream.py` (1,918) · koei-snes `vm_struct` (159) · snes-decompiler `native_decompile` (602, a port) · gates: `cfg_gate`, `stack_audit`, na1 `detect-prop-clobber`, `v2-corpus`, `value-oracle` | `passes/structure` over a shared IR | decompiled C byte-identical; na1 V2 corpus |
| H | Call graphs | bk `bank_callgraph` · GemFire `call_hierarchy` · UW `call_graph` · mappy `call_graph` · na1 `native-call-index`, `driver-call-index` · koei-snes `vm_callgraph` · snes-decompiler `native_callgraph` · rot3k2 `bank_hierarchy`, `native_bytecode_called` | `passes/callgraph` | identical edge sets |
| I | Walk ordering (leaves first) | bk `walk_order` · UW `walk_bank` · ro3k `bc_layers` · na1 `bank-ground-order`, `cluster-anon-subs` · koei-snes `gen_layers` | `walk/order` | identical worklists |
| J | **Walk prep / apply / guard** (the skills' deterministic half) | na1 `label-walk-*`, `var-walk-*`, `walk_guard` · AeroBiz `walk_prep`, `walk_apply`, `walk_harvest` · vault `.claude/skills/*/scripts` | `walk/` (one per walk kind) | replay a past batch: same toml diff |
| K | Data xref | na1 `data-xref`, `rom-xref` · koei-nes `data_xref` · mappy `var_xref` · rot3k2 `table_frontier` · koei-snes `data_walk` | `passes/xref` | identical xref reports |
| L | Strings | bk + GemFire `pure_strings` (verbatim) · bk `ref_strings` · rot3k2 `extract_strings`, `strdump` · UW `dump_strings` · koei-snes `strings_snes` · na1 `ui-strings` | `passes/strings`; text encoding in the profile | identical string dumps |
| M | Records / data dumps | koei-nes `dump_data` ~ ro3k `dump_data` (0.45) · bk `dump_records` · UW `dump_records`, `field_census` · koei-nes `RECORD_SCHEMAS.md` | `passes/records` | identical dumps |
| N | **Address model** (ROM offset ↔ CPU address, banks) ✅ NES half | UW `prg.py` ("PRG offset is the label identity") · koei_vm `game_cfg` ROM resolver · snes-decompiler `mapper`, `header_parse` | `machines/{nes,snes}/address` | self-tests + every title resolves the same |
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
1. ✅ **A: label export (2026-10-22).** See "Row A done" below. ✅ **A3 2026-10-23:** one label
   pass over listings (`passes/listing`); row A closed. See "Row A3 done" below.
2. ✅ **D: native floor (2026-10-22).** See "Row D done" below.
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
   byte-identical through both import routes. ✅ **B done 2026-10-23**: one shared descent, nine
   copies folded, nes-disasm6 a plugin. See "Row B done" below. ✅ **N (NES half) done
   2026-10-23**: one bank-window model, 11 copies folded, `window_check` for the revisits. See "Row N
   done" below. ✅ **C done 2026-10-23**: one complete 6502 core, verified on nestest. See "Row C
   done" below.
5. **F: the KOEI VM**: koei-nes + koei-snes → `vm/koei` + two bindings. The largest move.
   Phased:
   - ✅ **F1 2026-10-23:** koei-nes's VM package moves into `emu_harness.vm.koei`, with aliases.
   - ✅ **F1b:** NA1's file paths resolve through the title profile, and per-title data stays per
     title.
   - ✅ **F2:** NES vs SNES opcode tables measured; 12 NES decoder lengths and the 512K fixed bank fixed.
   - ✅ **SNES half:** substrate, koei-snes, one opcode table.
   - ✅ **F3:** two hosts over one ISA (`vm/koei/nes` + `vm/koei/snes`); encoder + round trip in the
     engine; ro3k / UW wrappers onto the driver; stack effects measured. **Row F closed.**

   See "Row F1 done" below.
6. **G: DREAM.** Three implementations at very different maturity; needs an IR contract. ✅ **G1
   2026-10-23:** one structurer over any host's block graph; SNES runs it. Stopped there by
   decision: structuring doesn't need to be 100%; conventions are deferred. See "Row G1" below.

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

## Row A3 done: one label pass over listings, and row A closed (2026-10-23)

**Three copies of one job, in two dialects.** The inventory listed four tools. Reading them gave
three relabelers, one more that the inventory had missed, and two tools that do other jobs.
- **disasm6 dialect.** NA1's `tools/mesen-labels.py --asm` is the mature copy of game-annotation's
  `asm-relabel`, which the inventory missed:
  - `bN_XXXX` tokens and RAM operands become names, and the trailing `; OFFSET: bytes` comment is
    protected.
  - `$C000+` names count as the fixed bank's under any section.
  - Fixed-bank anchors are inserted.
- **Sweep dialect** (our own listings). Elite's `apply_labels` resolves an operand by where it
  lives: RAM, the fixed bank, or its own bank, a heuristic Elite's trampolines make sound.
- **Not this job:**
  - UW `stamp_prg` appends PRG offsets, which is address-model annotation and stays UW's.
  - Mappy `asm_gen` is a coverage-driven listing generator (row R).
  - NA1 `zp-harvest` is a zero-page usage census (row K).

**Now:** `passes/listing.py` (a new layer, with the contract "Passes know no emulator" shown to
fail on a planted violation).
- **Functions:** `registry_maps` (via `core.symbols`), `disasm6_maps` + `fixed_anchors` +
  `relabel_disasm6`, and `relabel_sweep`.
- **The fixed bank is a parameter** (NA1 15, Elite 7); the copies hardcoded it.

| caller | oracle |
|---|---|
| NA1 `mesen-labels.py --asm / --verify` | `--verify`: all 4 labeled banks identical to the old algorithm's output. Needs `py -3.14` now (it said `--asm` ran on py -3) |
| Elite `tools/apply_labels.py` (thin) | fresh raw listings by its README recipe (which reproduce the committed ones), relabeled old vs new: 8 banks + log identical |
| game-annotation `tools/asm-relabel.py` (thin CLI) | on NA1's raw banks it now equals NA1's labeled output exactly. Named: vs its own old output, 128 lines gain a name: fixed-bank labels filed under `[prg.bank0]`, which the old copy could not see. No other difference |
| Mappy `asm_gen.py` | only its path-hacked koei-nes import → `emu_harness.cpu.m6502.disasm`; regenerated `mappy_labeled.asm` unchanged |

**Row A is complete:** label export (A), valid registries (A2), and labels applied to listings
(A3) each have one implementation.

## Row H1 done: one call graph for the KOEI NES titles (2026-10-23)

Chris: the call graph "is what structures the code for proper inference". Who calls whom sets the
bottom-up order a walk names routines in.

**Shared IR: `passes/callgraph.py`.** The graph is host-neutral:
- **Nodes** are `(bank, addr)`, a routine's entry in its owning bank.
- **Edges** carry kinds (`call` / `jsr` / `jmp` / `bankcall` / `bankrun` / `dispatch`), plus data
  edges.
- **Roots** are vectors and dispatch tables, so an orphan means nothing calls it and no root
  reaches it.
- **Queries:** callers, callees, orphans, `leaves_first` (routines) and `bank_order` (banks).

**The KOEI NES builder: `vm/koei/nes/callgraph.py`.** It builds from the bytes, never from listing
text:
- **Bytecode `CALL`s:** decoded through the ISA (`cf = "call"`). The owner is the bank the window
  model maps there: the caller's own bank, a pinned bank, or the single bank with a stub exactly
  at the target, ties broken by `coresident_<bank>`. Otherwise the call is reported.
- **Bank-crossing calls:** the trampoline (`bankcall_fn`), the run-bank syscall, `memcpy_banked`
  and GemFire's mode table. A call counts only when its *decoded* target is that native's address.
  The constant arguments come from the decompiler's raw C line for the call, via its `collect`
  hook: the first line from the call to the next call of the same native.
  - Conventions are profile data: `[<title>.callgraph]` `bankrun` / `dataload` / `dispatch` /
    `tables`.
  - `titles.decompiler_for` is the one decompiler setup (`decompile` uses it; NES 889/889
    identical).
- **Natives:** the fixed bank's routines are nodes too, being the leaves. They come from
  `native_floor`'s enumeration plus the natives only bytecode calls, which LEmp had hand-listed as
  `extra_seeds`. The row-B descent walks them for JSR and tail-JMP edges.
- **Roots:** `native_floor.seed_groups`. `seeds()` is now their union, and `native_floor`'s 21
  outputs are identical.
- **Profile additions:** `[na1.native_floor]` (23 syscalls; ext-op table `$F261` × 48, measured
  from `ext_op_dispatch`'s `lda $F261,x`), `[ro3k.native_floor]` (30 syscalls, 48 ext ops),
  `[na1.callgraph]` (the command-driver table, bank 1 `$B9B2` × 34, ROM-verified against
  `command-table.txt`).

**All 9 NES titles build** with 0–15 unresolved calls each, each one reported with its reason
(two banks with a stub at the target, or a bank switch the code makes at run time).

**Folded, against each old output** (every old-only edge was traced to a cause):

| old | now | oracle |
|---|---|---|
| bk `bank_callgraph` (broken: shelled out to `py -3`, which cannot import emu-harness; run for the oracle with py -3.14) | thin CLI: `--banks --bank-order` | trampoline / run-bank / fixed-bank / co-resident / `memcpy_banked` edges ⊇ old; data edges equal (22/22) |
| GemFire `call_hierarchy` | thin CLI: `--banks --indegree` | ⊇ old, except 5 edges the old tool mis-owned: when two `$8000` banks had a stub at the target it took whichever its set iteration met first (bank 0); the profile's co-residency says 4 / 9 |
| UW `call_graph` | thin CLI: `--indegree --banks` | ⊇ old, except bank 25 → 1. Bank 25 runs at `$A000` itself, so its `$Axxx` calls are its own; the old rule was "`$Axxx` = bank 1" |
| rot3k2 `bank_hierarchy` | thin CLI: `--banks` | ⊇ old |
| NA1 `native-call-index` | thin CLI (`callers` / `calls` / `orphans` / `stats`) | bytecode calls ⊇ old (+ bank 14). Fixed-bank JSR targets ⊇ old, except 7 false ones (bytecode subs' bytes parsed as 6502: `format_string → $81D4`), a stub's own `JSR vm_entry`, and `$8000` (after a bank switch: reported unresolved). Roots ⊇ old. Orphans are routines now (the old universe held data and interior labels) |

**What the old graphs missed:**
- **`CALL_abs_imm1`**, the call form that carries arguments. The regex `CALL[_a-z]*\s+\$` cannot
  match it, because the `1` in `imm1` breaks it. That hid bk's calls into `rand_mod` / `muldiv`
  and others.
- **Trampoline and syscall edges whose target is already named.** The tools grepped the annotated
  C, where the decompiler has rewritten those into direct calls (rot3k2's tool said so). So they
  lost an edge each time a target got a name, which is backwards for a walk-ordering tool.
- **Finding:** UW `docs/10-call-graph.md`'s "ZERO subsystem→subsystem edges" holds for direct
  calls only. Through `call_in_bank_dcd1`, 34 subsystem → subsystem edges exist (e.g. bank 18
  `$83BB` → bank 29 `$872B`). Revisit row below.

**Stays in its title:** NA1 `driver-call-index`, a per-driver formula-decode aid over bank 1.

## Row H2 done: SNES and Mappy on the one graph; row H closed (2026-10-23)

Chris: SNES swaps memory pages differently, but the job is the same. A module is loaded into WRAM
as an overlay where an NES bank is mapped into a window, so the node is `(module, addr)`. The
resident modules play the fixed banks, and the parent an overlay runs beside plays a co-resident
bank.

- **The ordering is Mappy's.** Its Tarjan SCC order is the better leaves-first: reverse
  topological, with a recursive cluster kept together rather than cut at an arbitrary member.
  - It is now `passes.callgraph.sccs`, made iterative with the recursive version's exact visiting
    order (a test checks it against the recursive original on 200 random graphs).
  - `leaves_first` and `bank_order` are that order. They replace the H1 heuristic, which was
    used only by the not-yet-committed views.
  - snes-decompiler's subtree worklist is `passes.callgraph.postorder`. Mappy's walk and the
    native post-order are the reuse here, not the H1 code.
- **koei-snes `vm_callgraph`** builds a `passes.callgraph.Graph` per module (`module_graph`, from
  `vm_walk`'s classified calls). Its forest roots and naming frontier are the shared `tops()` /
  `frontier()`; `frontier` is the walk's "name these next", now available for NES too. All 384
  outputs (3 titles, every module, 6 modes) are identical.
- **New: `vm_callgraph --title <repo>`.** It builds one graph over every module and orders the
  *modules* leaves-first:
  - rot3k2: `root → comroot / hex / … → their overlays → the nested hsovl51-53`;
  - GemFire: `root → command → its command overlays`;
  - AeroBiz: `COMMON → ALNMAIN / COMMAND → the apps`.
- **Co-residency for rot3k2 and GemFire, measured** (`[rot3k2.coresident]`,
  `[gemfire.coresident]`). Their overlays' calls into the parent left in WRAM were "unmapped"
  (rot3k2 855 calls). The exact-entry census assigned them: `csovl1-7` / `excsub1-6 → comroot`,
  `hsovl1-6 → hex`, `hsovl51-53 → hex + hsovl5`, GemFire `comcmp` / `comusr1-2 → command`.
  - Effect on the SNES run, 80 of 439 outputs changed, all one of four kinds:
    - calls into the parent named in the C (`sub_5767(...)` → `list_castle_officers_b(...)`) and
      in the listings (`; list_units_paged`);
    - call-class counts;
    - symbols-visible counts;
    - one native stub recovered: rot3k2 `comroot` `$BBF6` (`PHP; REP #$30; JSR $BC20 …`), which
      only the overlays call, was listed as `db` bytes.
- **Mappy `call_graph`:** its Tarjan is the shared one now, and its path-hacked koei-nes import is
  `emu_harness.cpu.m6502.disasm`. Stdout is identical, and the committed `callgraph.dot` /
  `bottom_up_order.txt` regenerate unchanged.
- **snes-decompiler `native_callgraph`:** `dfs_postorder` delegates. All 3 modes on NA1-SNES are
  identical, and the post-order from every one of its 727 routines equals the old one.

**Row H is complete:** one graph and one ordering, with a builder per host (KOEI NES, KOEI SNES,
native 65816, native 6502 / Mappy).

## Row D done: one native-floor tool (2026-10-22)

**Shape.** The 7 copies were not one algorithm with per-title constants: they were 7 stages of
one algorithm improving along the copy chain (syscall seeding 4 versions, reach 4, native_subs 6,
oracle NA1 -> NA2 -> several). The merged tool is koei-nes `tools/native_floor.py <title>`: the
last stage (UW's), driven by `games.toml` plus a `[<title>.native_floor]` table (oracle, slot
counts, trampolines, extra seeds) whose values were copied from the forks by code. It lives in
koei-nes beside the profile and moves with the KOEI VM (row F); it uses emu-harness for the
6502 decode, signature, fixed bank and registry.

**Oracle, per title:** (1) coverage vs the fork's own output, (2) proposals vs the fork,
(3) inherited names vs the reviewed registry (`--check`). UW reproduced its fork exactly (326/326)
before any change, so the port is faithful.

**What the other six exposed, and the fix.** UW's phantom filter decoded each sub *linearly past
its rts*; where a data table followed, the decode misaligned and swallowed the next real entry
(na2 `write_ppuctrl` after the syscall table, rot3k2 `umul32` after the ext-op table,
`syscall_set_prg_bank` in 4 titles). New rule: decode by control flow, and a candidate on another
sub's operand byte is still kept if it comes from a vector/dispatch table or a real (decoded)
JSR targets it; overlapped entries are a real 6502 trick. Result: UW gains 2 real subs its own
copy dropped (`$EF81`, in its own seed list; `$FAF7 syscall_set_prg_bank`).

| Title | Fork | Merged | Dropped (all junk or phantom, by evidence) | Added |
|---|---:|---:|---|---|
| gk | 249 | 247 | `$EE60` junk; `$C0A9` see below | |
| na2 | 324 | 319 | 6 junk/phantom-named; `$E960` phantom duplicate | `$EC23`, `$F46B` |
| rot3k2 | 273 | 263 | 9 junk; `$E960` phantom (registry fixed) | `$F91A` (syscall dispatcher, seeded via its vector) |
| bk | 305 | 300 | 5 junk; `$FF20` phantom (registry fixed) | `$F6D0` |
| GemFire | 306 | 300 | 4 junk/`DATA_`; `$F5D0`, `$F8D0` see below | |
| LEmp | 332 | 322 | 10, all already named `phantom_*` in its registry (its recon's follow-up: "tighten the filter so those phantoms aren't emitted") | |
| UW | 326 | 328 | | `$EF81`, `$FAF7` |

**Registry corrections (phantoms that had inherited a real name; a phantom decoded 1-2 bytes off
realigns with the real routine, so it scored 0.93-0.98 against the oracle):** rot3k2 `$E960` ->
`$E962` `vm_op_B1_storeA_ind_word` (opcode `$B1`'s handler-table entry); bk `$FF20` -> `$FF22`
`sound_update_noise` (the one real JSR); na2 `$E960` (duplicate of the real `$E957`) removed.
No decompiled source referenced the moved names.

**Also fixed:** koei_vm `load_games` used a line regex that knew only `[title]` headers, so a
`[title.native_floor]` table poured its keys into the previous title (it would have replaced UW's
`extop_table`). Now tomllib; identical values for all 9 existing titles. **Retired:** the 7
`tools/native_floor.py`; GemFire `multi_oracle` and rot3k2 `native_bytecode_called` rewired
(the latter finds nothing left to name, so its identical output is a weak check).
`--check`: inherited names agree with every registry as the forks' did; the rest differ because
reviewers chose better names after reading.

## Row B: how well each 6502 disassembler does (2026-10-22)

`tools/disasm_bench.py` (capture + score) reduces every contender to one verdict per PRG byte
(instruction start / operand / data) and scores it against two oracles:
**the running CPU** (Mesen's code/data log via the new bridge `cdl` command; what executed vs what
was only read) and, for Elite, **Moxon's annotated source** (every byte). Where both speak they
agree 100%, so the source is a trustworthy oracle for the bytes the run never reached.

| Contender | Elite: code found / data called code / starts real | Mappy: executed found / data called code | UW: executed found / data called code |
|---|---|---|---|
| linear sweep (ours) | 100% / **64.5%** / **46.4%** | 100% / **71.1%** | 99.6% / **75.2%** |
| **nes-disasm6** (third-party) | **99.1% / 4.1% / 92.6%** | **98.5% / 5.7%** | 92.9% / 4.7% |
| recursive descent, cold (vectors only) | 10.4% / 0% / 100% | 17.2% / 0.3% | 12.0% / 0% |
| Mappy trace (NA1's Python 6502) | | 58.2% / 0.0% | |
| Mappy static_expand (trace-seeded descent + jump tables) | | **96.3% / 0.5%** | |
| UW disasm_native, bank 3 (home turf) | | | **99.5% / 0.0%** (nes-disasm6 there: 76.3% / 16.4%) |

**Reading.**
- Our linear sweep is a *listing* renderer, not a code/data separator: it calls two thirds of
  all data "code" and fewer than half its instruction starts are real. It stays (byte-exact,
  validated), labelled for what it is.
- **nes-disasm6 is the best generic separator** (Apache-2.0; run as an external plugin, never
  vendored; path in `config.toml`). Its weakness is KOEI's inline-operand calls (entries-as-starts
  falls to 71-83% on UW).
- Recursive descent is exact but only as good as its seeds. Seeded well (Mappy: CPU trace + jump
  tables; UW: KOEI's `$E299`/`$E338` bridges) it beats everything on its home ROM.
- NA1's Python 6502 core never called data "code" (0.0%): its trace is faithful, just shorter.
- So the target is one seeded recursive descent in `cpu/m6502` (seeds from vectors, tables, the
  CPU log, a title profile; inline-bridge lengths as a parameter), with nes-disasm6 for the regions
  descent cannot reach. It folds 6 descent copies: ro3k `subs.reach`, GemFire `inbank_native`,
  UW `disasm_native`, Mappy `static_expand`, koei-nes `native_floor.reach`, rot3k2 `disn` (a wrapper).
  Oracle for that fold: no tool may score lower than its fork on its home ROM.

**Found by the CPU log (UW):** bank 59 executed 966 bytes of native 6502 (28 runs, 16 subroutine
entries, mapped at $8000) during boot and title; it is not in `native_banks` and has 0 registry
labels, so UW's "complete" 555-sub native inventory misses it. Bank 1's executed bytes are the
3-byte `JSR vm_entry` stubs of bytecode subs (correctly not native subs).

## Row B done: one shared descent (2026-10-23)

`emu_harness/cpu/m6502/descent.py`: a `Window` (a ROM slice at a CPU base, optionally mirrored),
`flow` (one routine: fall-through, both branch sides, `JMP abs` inside the window; ends at rts / rti
/ `JMP (ind)`, an undocumented opcode, a known data byte, optionally BRK; a `JSR` to an
inline-operand bridge skips its N data bytes), `descend` (every followed JSR target becomes an entry,
to a fixpoint; a `follow` predicate vetoes targets, e.g. KOEI bytecode stubs), `gaps` (UW's
prologue-recovery heuristic, generic), `verdicts`, and `Descent.overlaps()` (bytes one path decodes
as a start and another as an operand; reported, not resolved by visiting order). nes-disasm6 is
`machines/nes/nes_disasm6.py` (run from the configured clone, never vendored); the disasm6-listing
parser is `disasm.listing_verdicts`. 10 tests; each of 11 planted mutations of the walker fails them.

**Folded:** 9 copies of the walker in 6 repos, plus rot3k2's `disn` wrapper.

| Fork | Now | Oracle on its home ROM |
|---|---|---|
| ro3k `subs.reach` | `descend` | 244 entry points x 16 banks: identical |
| GemFire `inbank_native.reach` | `descend` | 231 cases x 32 banks + a full CLI run: identical |
| koei-nes `native_floor.reach`, `decode_flow` | `descend` with a stub veto; `flow(end=, into_jmp=False)` | 7 titles x (table, `--toml`, `--check`) = 21 outputs: identical |
| rot3k2 `disn` | `disasm.render` via the package | identical |
| Mappy `static_expand`, `jump_tables`, `stored_pointers`, `pointer_tables` (4 copies of one walker) | `mappy/code_map.py`, one adapter over `descend` | whole pipeline in a temp copy: every coverage map and `jump_tables.txt` byte-identical, routine sets identical |
| UW `disasm_native` | `descend` + `gaps`; keeps UW's stub convention and listing | 53 of 64 bank renders identical; 11 differ, every line named below |
| disasm_bench's cold contender | `descend` | Elite identical; Mappy +0.1% data called code, named below |

**Named differences (each a bug in a fork, not in the fold):**
- UW listed an undocumented opcode it ran into as an instruction (`.db` lines; banks 4, 10, 39, 41,
  54-56). Gone. In the data banks 33, 40 and 51 that byte had also hidden the start of runs of
  `brk`/garbage, which the recovery pass then "recovered" as 9 subs; they are gone too.
- UW's recovery pass listed subs first reached from a recovered sub but never walked them: bank 6's
  `$9985` (`inc $00,x / bne / inc $01,x / rts`, a 16-bit increment) is now disassembled. UW's 555
  count is unchanged (the sub was already counted). The 12 committed listings were regenerated: apart
  from banks 4, 6 and 10 they differ from a fresh run only by label comments added since they were
  last generated.
- Mappy's routine lists now come out in plain sorted order (the old sort put raw `$8xxx` aliases
  first; same sets). `pointer_tables` printed "routines: 378" counting 8 routines under both mirror
  addresses; it now prints 370, matching its own "nodes=370".
- The benchmark's old cold walker stopped a path that overlapped an earlier decode. On Mappy that
  happened to cut one wrong path entering the middle of `sta ($f0),y` at `$F6A3` (3 bytes). No fork had
  that rule, and whichever path arrived first is no evidence of which is right, so the shared descent
  reports overlaps instead (`Descent.overlaps()`).

**Combinations measured** (`+ nes-disasm6` = a contender's claims, nes-disasm6 for the rest):

| | Elite: code found / data called code / starts real | Mappy: executed found / data called code | UW: executed found / data called code |
|---|---|---|---|
| nes-disasm6 alone | 99.1% / 4.1% / 92.6% | 98.5% / 5.7% | 92.9% / 4.7% |
| cold descent + nes-disasm6 | 99.2% / 4.1% / 92.6% | 98.8% / 6.0% | 93.2% / 4.7% (with KOEI bridges) |
| Mappy static_expand + nes-disasm6 | | **100.0%** / 6.1% | |
| UW disasm_native + nes-disasm6 (its 12 banks) | | | 93.1% / 6.8% (nes-disasm6 alone there: 89.4% / 7.4%) |

Filling a seeded descent with nes-disasm6 recovers what the seeds missed, at nes-disasm6's own
false-code rate. Use descent's claims where it has them, and treat nes-disasm6's verdicts on the
remainder as unverified.

**Residue (not row B):**
- UW `native_banks.walk_body` is a linear copy of the row-E `signature`, with target collection
  added. It moves with E.
- Running Mappy's `jump_tables.py` overwrites `jump_tables.txt`, which is curated: commit `673a53d`
  added three level-data pointer tables by hand that the script does not find. That belongs to
  row R. The committed Mappy outputs were left as they are.
- The na1 handler classifiers (`classify-handlers`, `analyze-vm-opcodes`) use short fixed-length
  scans, not descents. They belong to row F.

## Row N evidence: which window does each bank run in? (2026-10-23)

**Four copies of the bank-window list, and they disagree:** koei-nes `games.toml`
(`banks_at_8000` per title), UW `prg.py` (the same as games.toml), UW `disasm_native`
(`BANKS_AT_8000`, its own list), and GemFire `inbank_native` (the same as games.toml).

**The oracle, a bank's own code, scored against each list:**
- *Words onto own stubs.* A 16-bit word in the bank that lands exactly on one of the bank's own
  `JSR vm_entry` / `JSR $E299` stubs when the bank sits at that base. A call into another bank
  rarely lands on a stub.
- *Rendered call targets.* Where the jsr/jmp targets of an already-rendered listing point.

The first try, a raw count of in-bank JSRs, was confounded: an app bank at `$A000` calls library
banks at `$8000`.

**What the bank's own code says:**
- na2, rot3k2, bk, LEmp: every bank agrees with games.toml.
- GemFire bank 2: `$8000` (70 to 3); games.toml says `$A000`. It is not in `code_banks`, has no
  labels and no listing, and opens with native code (`jmp $8142`, `jsr $80f4`). It is unwalked
  code: a coverage gap like UW bank 59.
- UW banks 7, 10, 13 and 59: `$8000`; games.toml says `$A000`. Bank 59 is confirmed by the CPU log.
- UW `disasm_native`'s list is wrong for 11 banks (0, 7, 8, 10, 12, 13, 17, 20-24, 29, 30 as
  scored). Of its 12 committed listings, banks 7, 8, 10, 13 and 17 are rendered at `$A000`, yet
  they make 289 calls into `$8000` and none into `$A000`. Banks rendered at `$8000` call their own
  window heavily. So those listings are at the wrong base. Every in-bank call was treated as
  external and never followed, and the recovery pass guessed the subs instead (28 in bank 7, 27 in
  bank 13). UW's 555-sub inventory and its "106 jumptable-recovered" figure need recounting.
- **The 4 remaining same-byte collisions** (na2 x3, UW x1) are each a label at a CPU address its
  bank never runs at (`$A056` in a `$8000` bank, and so on): wrong-window duplicates.

## Row N done, NES half: one window model (2026-10-23)

Per Chris, this row builds the tool; the project corrections it turns up go to the revisit table
below.

- `machines/nes/memory.py`:
  - `Windows`: fixed banks from the cart (MMC1 last 16K at `$C000`; MMC5 last two 8K at
    `$C000`/`$E000`), switchable banks from a profile via `from_profile`. It provides
    `base` / `cpu` / `prg` / `fits`.
  - `window_votes`: the evidence. Words landing on the bank's own entry points, or, with `after=`,
    JSR/JMP operands only, the fallback for banks without stubs.
  - 4 tests; each of 6 planted mutations fails them.
- koei-nes: `koei_vm.windows(cfg)` is the one place a KOEI tool asks where a bank runs.
  `bank_base_for` delegates to it, keeping its old profile-only answer, which ignores fixed banks.
  New `tools/window_check.py <title>` checks a profile against the banks' own code and flags
  registry labels outside their bank's window. It is the review tool for the revisits and changes
  nothing.
- **Folded: 11 copies of the window list or logic.** UW `prg.window`, `disasm_native.base_of`,
  `native_banks.base_of`; GemFire `inbank_native.window`, `pure_strings.base_of`,
  `call_hierarchy` (`AT_8000`/`AT_A000` now derived); bk `pure_strings`, `ref_strings`,
  `bank_callgraph`, `walk_order`; rot3k2 `extract_strings`. koei_vm's own function makes 12.
- **Oracle 1, every copy's own code (taken from git) vs the model, bank by bank, 6 titles:**
  7 identical, and koei_vm identical for all 6 titles. Named differences:
  - UW `disasm_native` / `native_banks` shared a private list that was wrong for 15 banks. The
    model follows games.toml, which the bank evidence supports for all 15 (63 is fixed at `$E000`).
  - rot3k2 `extract_strings` put banks 30 and 31 at `$A000`, but it skips those banks, so its
    output is unchanged.
  - GemFire `pure_strings` knew data bank 19 is read through `$8000` (its own `MEMORY_MAP.md`
    says so too). That fact moved into games.toml `[gemfire] banks_at_8000`, so `inbank_native`
    now agrees.
- **Oracle 2, tool outputs vs the row-B state:**
  - GemFire `inbank_native` (231 cases), native_floor (21 outputs) and ro3k: byte-identical.
  - UW `disasm_native` renders change for exactly the 15 banks whose window changed. Bank 8's
    decoded share rises from 25% to 77% at `$8000`, independent confirmation. The committed UW
    listings were not regenerated (revisit).
- **Not done here:** the profile corrections and label fixes (revisit table). The SNES half
  (snes-decompiler `mapper`, `header_parse`) moves when the SNES tools do.

**What `window_check` reports today** (the stub check, then the fallback for banks without
stubs):
- Every MMC5 title's sound-engine bank runs at `$8000` while its profile says `$A000`: na2,
  rot3k2 and bk bank 28 (38:4, 40:1, 25:1); GemFire and LEmp bank 27 (39:1, 39:4).
- GemFire bank 2 and UW banks 7, 13 and 59 are also at `$8000`.
- 184 registry labels sit outside their bank's window: na2 139, bk 23, UW 18, rot3k2 3,
  GemFire 1. They come in three kinds:
  - **Filed under the bank that uses them**, while the byte lives in the bank sharing the other
    window (na2's `*_data_A0xx` names in `$8000` banks 1, 2, 9, 12, 14, 16; bk bank 14's
    `tbl_*`; rot3k2's `gauge_tile_data`, `ruler_stat_table`). `locate` then puts the name on the
    wrong byte. That is the source of na2's 3 dropped `.mlb` names.
  - **Rendered at the wrong base:** UW's `nat_8_*` / `nat_17_*`. The byte is right, the CPU
    address wrong.
  - **The profile is wrong, the label is right:** bk bank 28's `sound_engine_*` at `$80xx`.

## Row C done: one 6502 core (2026-10-23)

**There was only one core.** It was koei-nes `koeivm/cpu6502.py`, which na1's `na1dream` aliases.
It held about 130 of the 151 documented opcodes: the ones KOEI's VM handlers use. Mappy's
`trace_codegraph` completed it in a subclass with 18 more plus BRK, and its `nmi()` / BRK pushed a
constant status byte. So every RTI after an NMI wiped N, V, Z and C. ro3k `emulator_rotk` and
koei_vm's VM runner sit above the CPU (row F).

**Now:**
- `emu_harness/cpu/m6502/cpu.py` is the koei-nes class, converted (not rewritten), plus Mappy's 18
  opcodes, a `p` property, and real interrupts. `nmi()`, `irq()` (masked by I) and BRK push the
  actual flags, with B set only for BRK, and BRK returns past its padding byte.
- Exactly the 151 documented opcodes: undocumented ones raise, matching the disassembler.
- No decimal mode, because the 2A03 has none. Instruction counts only, no cycle timing.
- koei-nes `koeivm/cpu6502.py` keeps its KOEI banked `Memory` (the console binding; it moves with
  row F) and re-exports the core.
- Mappy's `TracingCPU` is now just the core.

**Oracles:**
- **nestest** (kevtris's NES CPU test, with Nintendulator's golden log): the core matches PC, A,
  X, Y, P and SP on all 5,003 documented-opcode lines (`tools/nestest.py`). The ROM and log are
  third-party test data fetched into `_scratch/nestest/`, never committed.
- **Baselines for comparison:** the old koei-nes core stopped at line 1,147 (`ORA ($80,X)`
  unimplemented). Mappy's subclass passed all 5,003, because nestest never takes an interrupt.
- **Every consumer, before and after:** na1's `test-cpu6502`, `test-dispatcher`,
  `test-loadl-quick` and `value-oracle certify-extop`; ro3k's income ground-truth test; Mappy's
  `render_level` (2 PNGs) and `trace_codegraph` (600 frames, and 4,000 frames). All byte-identical.
  The interrupt fix changes nothing Mappy's demo reaches.
- **6 tests:** nestest (skipped when the data is absent), the exact opcode set, BRK / NMI / IRQ /
  PHP-PLP. Each of 8 planted mutations fails them, the old Mappy constant-P bug among them.

## Row F1 done: the KOEI VM toolchain moves in (2026-10-23)

**F1, the move.** These all moved into `emu_harness/vm/koei/` unchanged, apart from package
imports and the sibling-root depth:
- koei-nes `tools/koeivm/` (disasm, decompile, CFG, V2 reducer, DREAM, stack model, the VM runner,
  `cli/`, `gates/`; about 10K lines);
- `tools/koei_vm.py`, now `titles.py`;
- `games.toml`.

New `core/alias.py`: old package names resolve to the *same module objects*, through
`alias_package` (which also supports `python -m old.x`). The old `__path__` trick loaded each file
twice, with separate state. koei-nes `tools/koeivm` and `tools/koei_vm.py` and na1's `na1dream`
are now aliases. New contract: "VMs know no emulator" (a planted violation fails it).

Oracle: 876 outputs, run before and after in fresh processes:
- every code bank of 9 titles (144 banks): listing, structured C, raw C;
- NA1's `decompile_all` and its 3 gates;
- the na1 CPU tests and value oracle;
- ro3k's income test, `window_check`, native_floor.

872 were byte-identical. The other 4 were NA1-route runs that already failed, on the same
missing-file error.

**F1b, what the move exposed in the engine, fixed:**
- **NA1's paths were computed from the code's own location.** That stopped pointing at NA1 when
  the package left na1-decompiler (2026-06-29). Since then:
  - `python -m na1dream.cli.decompile_all` and the gates crashed (NA1's `source/README.md` even
    says the old command "no longer runs");
  - `asset-enums.toml` silently loaded nothing, so `trigger_cutscene(CUTSCENE_BRIBE)` went back to
    `trigger_cutscene(26)`.

  Now these come from the `[na1]` profile, and the enums load per title in `set_schema`.
  `decompile_all` produces 499 subs again, `test_vm_cfg` passes 114 of 114, and `stack_audit`
  runs.
- **`NobunagaVM(labels_path=...)` was accepted and ignored.** Every title's listing header said
  "labels: 0 CPU addresses named". The title drivers now pass the title's labels, and the 144
  listings show their real counts.
- **NA1's certified ext-op folds were keyed by address and applied to every title.**
  - Genghis Khan's bank-5 `$8327` (different bytecode: compared byte for byte) rendered as NA1's
    `scale_div10_capcheck`. Its real body computes a wrapped tile-row offset, which is what its
    registry name says.
  - UW's bank-38 `$8327` crashed on the fold ("list index out of range").

  The folds now apply to NA1 only.

**F1b oracle:**
- Every difference from the F1 state is one of those named changes: 144 listing headers, 6 NA1 C
  files with enum names, 4 NA1 runs that work again, gk bank 5, and UW bank 38.
- **The pre-F1b title-route C for NA1 is byte-identical to NA1's committed `4-c` and
  `3-c-basic`**, all 6 banks, after its recipe's banner and path clean-up. So the committed NA1 C
  is the engine's output, and F1b changes it only by the 19 enum substitutions.
- 3 new tests: alias identity, `-m` through an alias, NA1-only folds.

**Engine issue for row G (not caused here):** `cfg_gate --dream` fails at `b1/$8327`,
`b15/$D6B8`, `b15/$D6DE`, three of NA1's certified-fold subs. NA1's own engine at the last commit
before graduation (`c0db804^`, run in a temporary worktree) fails identically, so it has been red
since then. NA1's ROADMAP records it green (495/495) before the folds.

## Row F2 done: the NES and SNES instruction sets, measured (2026-10-23)

Two toolchains grew independently over one engine:
- **NES:** lengths in `nobunaga_vm.OPCODE_INFO` plus overrides in `vm_disasm.instr_len`, names in
  `vm-opcodes-v2.toml`, control-flow sets hardcoded in `vm_disasm`.
- **SNES:** one table, koei-snes `data/vm_opcode_names.toml`, which its loader `vm_isa.bind`
  checks against each SNES ROM's handlers and raises on a mismatch.

Two new tools:
- **`tools/koei_isa_census.py`** compares them per opcode: NES decoder length + class vs the SNES
  table read through its own loader.
- **`tools/koei_isa_probe.py`** is the lower artifact. On each of the 9 NES titles it places an
  opcode and zero operands in RAM, runs the cartridge's own dispatcher and handler for one
  instruction, and reads how far the VM instruction pointer moved.

**Found:**
- **The NES decoder was wrong for 12 opcodes, and all 9 NES cartridges agree with the SNES table:**
  - `$AD` blkcopy takes 2 operand bytes, not 1. `OPCODE_INFO` said 4; a hand reading of the
    handler said 1.
  - Bitfields `$E0`–`$E2` take `#i16`, not 1 byte.
  - **`$B7 $0C`–`$13`** (32-bit load / store / push of a frame slot or absolute address) take a
    2-byte operand, not 0. These occur **251 times in 7 titles** (gk, ro3k, na2, rot3k2, GemFire,
    LEmp, UW). Each operand decoded as two opcodes; when those were `$EB`/`$FF`, the C showed fake
    `trigger_syscall_EB_FE` / `op_FF` statements. ro3k's `ai_invest_commerce_and_distribute_capability_pay`
    had a 57-parameter signature and a 128-underflow indirect call; it is now `(void)` and
    `if (score_officer_capability_weight(...))`.
- **The VM's NES bus hardcoded the fixed region as 16K bank 15**, which is right only for 256K
  carts. So UW's banks 62 and 63 (512K) were decoded from banks 30 and 31's bytes, UW's
  fixed-bank call targets were misclassified `{native}` / `{bytecode}`, and the probe could not
  run UW at all. Now it is the last 16K of PRG, the same rule as row E's `fixed_bank`.
  `read_banked` reads through it.
- **UW's bank 63 was rendered at `$A000`:** `bank_base_for` skips fixed banks and the profile
  does not list 63. The title driver now asks `windows(cfg)`, the full model (unchanged for the
  MMC1 titles, whose path ignores it).
- **Real per-revision differences, recorded, not fixed:**
  - `$B2` is a NOP on every NES title and dead on SNES.
  - Dead slots BRK on NA1 and return to the dispatcher on the MMC5 titles.
  - The calling-convention / syscall differences are already profile data (`syscall_kind`,
    `vm_entry_stub`, `bankcall_fn`, the ext-op table).

**Now:**
- The census agrees on **every live opcode: 217/217 primary, 47/47 extended** (size and
  control-flow class).
- The probe shows the NES decoder equal to the measured handlers on all 9 titles, for every
  fall-through opcode.

**Oracle for the fixes:** the full F run (876 outputs). Every changed file is explained:
- 21 listings contain a fixed opcode;
- UW banks 62 and 63 (the fixed bank);
- 20 UW listings with fixed-bank call targets reclassified;
- every changed C / log file follows its listing.

**Tests:** the measured lengths, the census on every opcode, and the 512K fixed region (4
planted reversions each fail them).

**Still F:** the single physical table moves in with koei-snes's loader (`vm_isa`, the SNES half
of row F). NES names stay NES-style until then. The two now provably describe one instruction
set.

## Row F, SNES half done: one engine package, one opcode table (2026-10-23)

- **S1, the substrate.**
  - snes-decompiler's `w65c816` (a pure decoder) became `cpu/w65c816/isa`.
  - `mapper`, `header_parse`, `snes_regs`, `disasm65816` and `walk65816` became
    `machines/snes/{mapper, header, regs, disasm, walk}`. The disassembler and walker read a ROM
    through the SNES mapper, so they are machine code, which the CPU contract requires.
  - snes-decompiler keeps six aliases: `import X` gives the moved module, and `py tools/X.py`
    runs it. Its `native_decompile` / `native_callgraph` (rows G/H) import through them.
- **S2, koei-snes.**
  - The 22 tools moved into `vm/koei/snes`, and `games.toml` moved with them.
  - Bare-name imports became package imports. One was a module named `profile` that shadowed
    the stdlib's, and worked only because each script put its folder first on `sys.path`.
  - `gen_layers` now runs its sub-tools as `-m` package modules.
  - The new "VMs know no emulator" contract caught the label exporter importing the Mesen
    backend: it is `tools/gen_mlb_koei_snes.py`, beside the NES exporter.
  - koei-snes keeps 22 aliases; its `gen_mlb.py` runs the emu-harness tool.
- **S3, one table.**
  - The table is `vm/koei/vm_opcode_names.toml`, and its machine-neutral model (classes,
    layouts, lift templates, `Op` / `Table`) is `vm/koei/isa.py`. Both moved verbatim out of
    `vm_isa`, which keeps the SNES binding and re-exports the model. `minitoml` is
    `vm/koei/minitoml`.
  - The NES decoder now takes its operand lengths from that table: `vm_disasm.instr_len`, and
    `OPCODE_INFO`, which is filled from the table and keeps its NES-dialect names.
  - The NES-only `$B2` NOP and the dead slots keep their entries. The census and probe tools
    import the package.
- **Oracle.**
  - The SNES run: 7 ROMs (header, disasm, walk, root walk), NA1-SNES's
    `native_decompile` / `native_callgraph`, and the 3 KOEI titles (`vm_opcodes`, `vm_walk`,
    native floor, per-module call graphs, `vm_disasm`, plus `gen_layers` and `gen_mlb` on
    copies): **439 outputs**. Identical through S1, S2 and S3, except 3 runs that already failed
    (Aerobiz Supersonic is not a KOEI root layout; rot3k2's `cbmdata1` and GemFire's `native` are
    not bytecode modules), which fail with the same error through the alias.
  - The NES run after S3: all **889 outputs identical**.
  - A planted change to the table (`$B7 $0C` `f16` → `f8`) fails the tests.

The calling-convention and syscall differences (NES fake-BRK vs SNES `JSL` ABI, the param block,
the per-title tables) remain host and profile data: `vm/koei/games.toml` (NES) and
`vm/koei/snes/games.toml` (SNES), two profile schemas. **Still in F:** F3 (separate the NES host
binding: `nobunaga_vm` + its `Memory`; fold the ro3k / na1 / UW wrappers) and one profile schema.

## Row F3 done, and row F closed (2026-10-23)

**The shape: two hosts over one instruction set.**
- `vm/koei/` holds what both share: `isa.py` plus `vm_opcode_names.toml` (the one opcode table),
  and `minitoml`.
- `vm/koei/nes/` holds the 6502 host and its pipeline: the banked bus (`cpu6502.Memory`), the VM
  runner (`nobunaga_vm`), `vm_disasm`, `vm_assemble`, `vm_decompile` with
  CFG / V2 / DREAM / stack model, `cli`, `gates`, and `titles` + `games.toml`.
- `vm/koei/snes/` holds the 65C816 host.
- `core/alias` now maps an old name to several packages, and the old name is its own proxy
  package, so `from koeivm import vm_cfg` finds a module that moved to `nes`.
- NES + SNES runs after the split: 889/889 and 439/439 identical, every caller still using the
  old names.

**Folded into the engine:**
- **NA1's `vm_assemble` (the encoder) and `vm-roundtrip` (the Rung-1 gate)** are now
  `nes/vm_assemble` and `nes/gates/roundtrip`. NA1 keeps aliases, and the outputs are identical.
  - The engine's own `stack_audit` gate had imported the encoder from NA1's tools folder; it now
    runs from any folder.
  - The round trip takes `--game`, through `titles.bank_view` (the one bank setup the disassembler
    and gate share). The encoder hardcoded NA1's `20 23 E8` prologue; now it uses each title's
    stub, and the ext-op widths from the table.
  - **All 9 NES titles round-trip clean: 6,649 subs, 770,185 bytes.**
  - Measured limit: the pre-F2 `$B7 $0C`–`$13` widths also round-trip clean, because the misread
    bytes re-encode as themselves. So the round trip proves encode/decode consistency, not
    lengths; the handler probe proves lengths. The encoder's docstring claimed otherwise and is
    corrected.
- **ro3k's `vm_disasm_rotk` / `vm_decompile_rotk`** are now thin CLIs over the driver, and
  `emulator_rotk`'s `RotkVM` takes its dispatcher, ROM and labels from the `[ro3k]` profile. It had
  reached into na1-decompiler by absolute path and monkeypatched the label path.
  - `emulator_rotk` and `probe_gold`: byte-identical.
  - The old wrappers vs the driver on all 8 code banks: the listing banner named the NA1 ROM and
    NA1's label count, and the C used NA1's ext-op handler addresses (`ext_op_F569` where ROTK's
    is `$FBFA`), NA1's province fields, and unnamed syscalls. Everything else was identical.
- **UW's `disasm_fixed_bytecode`** worked around the fixed-bank bug F2 fixed, and is now a thin
  command over the driver, with output equal to it. Its blind `$Axxx` → `$Cxxx` address rewrite had
  also changed 5 immediate operands in bank 62.
- **game-annotation's `tools/vm-disasm.py`** (NA1's original VM disassembler, driven by the old
  wrong-length spec, referenced by nothing) is retired with `git rm`.

**Stack effects, measured** (`tools/koei_isa_census.py --effects`): NES `vm_stack_effect` (audited
on the NES handlers) vs the SNES lift templates (checked on the SNES handlers). 188 agree; the rest
were run on all 9 NES cartridges:
- `$B6`/`$B9` signed div/mod leave regR = |divisor|. The NES table had it kept, because its audit
  saw only positive divisors. Corrected.
- `$BD`–`$BF` shifts clobber regR's low byte to `$FF` on the NES, while the SNES handlers keep
  regC. That is a real host difference, kept in the NES layer.

**Decisions:**
- **Profiles stay per host:** `nes/games.toml` and `snes/games.toml`. Their keys do not overlap
  (dispatcher / handler tables / bank windows vs resource archive / JSL ABI / header title /
  fetch helpers), so one schema would add structure without adding a check. The per-revision
  exceptions are profile data on both sides (`syscall_kind`, `vm_entry_stub`, `bankcall_fn`, the
  ext-op tables, NA1's certified folds; the shift clobber, `$B2`, and the dead slots in the NES
  layer).
- **Title-specific tools stay in their title:** NA1's `analyze-vm-opcodes`, `classify-handlers`,
  `gen-opcode-appendix`, `vm-opcode-labels`.
- `nes/vm-opcodes-v2.toml` stays as the NES-dialect notes (names, descriptions; `stack_audit` can
  annotate it). Nothing decodes from it.

**For row G:**
- The NES `vm_decompile` and the SNES `vm_lift` are two lifters, and DREAM and `vm_struct` are two
  structurers.
- The NES decompiler does not yet model the 32-bit ext ops, or NES shifts clobbering regR.
- `cfg_gate`'s three NA1-fold subs are red (since before graduation).

## Row G1: DREAM on the SNES lifter (2026-10-23)

**One structurer, two lifters.** DREAM (`nes/dream.py`) now takes a ready block graph:
- `tagged_cfg_graph(lines, cfg, leaders)` and `dream_structure_gated_graph` take any host's graph.
  The NES entry builds the graph with `vm_cfg.bytecode_cfg` and calls the same code.
- `snes/vm_dream.py` turns `vm_lift` blocks into DREAM's goto dialect. It checks itself first:
  lowering the printed lines (`vm_cfg.lower_goto_cfg`, the gate's own lowering) must give back the
  lifter's graph exactly. A routine that fails the check, or has an unhandled shape, stays in goto
  form.

**DREAM fixes found by running it on SNES.** NES output is identical after all of them (889/889).
- **Exponential emission.** `_emits` / `_min_addr` re-emitted both arms of every `if`, so the
  cost doubled per nesting level, and rot3k2 root `$2A21` never finished. Probes are now cached
  per emission, and the boolean layer is memoized. `EMIT_CAP` rejects an AST whose tree-walked
  size is over 200,000 lines before emitting it.
- **A switch that merges into its own loop's header** (rot3k2 root `$34F4`). The switch region
  dropped every block the merge dominates. When the merge is the enclosing loop's header, it
  dominates the switch too, so the region came out empty and the case bodies were orphaned. One
  orphan reached a latch, so the loop finder took the CONTINUE sink for a header, and emission
  crashed on its string address. Now post-merge code is excluded only when the merge comes after
  the switch, and a sink header bails out of scope. The regression test uses the real routine
  (it skips without the sibling ROM), and it fails if either fix is reverted.
- `_gated` reports why it rejects (`out of scope` / `emit cap` / `gate reject` / `over-dup`), and
  an emission error rejects the routine instead of crashing.
- Pure-statement detection also covers `mem[` / `memb[` / `mem32[` reads.

**Measured** (`vm_dream --stats`). Goto-free routines; "cutover" means DREAM when it passes the
gate, else `vm_struct`:

| title | routines | DREAM structured | goto-free: DREAM | `vm_struct` | cutover |
|---|---|---|---|---|---|
| rot3k2 | 1,358 | 805 (59.3%) | 698 (51.4%) | 611 (45.0%) | **882 (64.9%)** |
| GemFire | 591 | 335 (56.7%) | 294 (49.7%) | 244 (41.3%) | **362 (61.3%)** |
| Aerobiz | 479 | 156 (32.6%) | 108 (22.5%) | 82 (17.1%) | **144 (30.1%)** |

DREAM's rejects, by cause:

| title | over-dup | gate reject | out of scope |
|---|---|---|---|
| rot3k2 | 376 | 128 | 49 |
| GemFire | 173 | 49 | 34 |
| Aerobiz | 267 | 31 | 25 |

- **Over-dup is the main loss.** These routines are structured and proved equivalent, but the
  NES-tuned `DUP_CAP` prefers the goto form. On the NES, the fallback was V2. On SNES it is
  `vm_struct`, so the cap's trade-off has to be re-measured for this host.
- **Gate rejects mean either DREAM built something wrong or the gate is too strict.** Each one is
  a bug lead for row G.
- **Not yet cut over:** the SNES `.c` output still comes from `vm_struct`. Switching it changes
  committed title files, so it is a separate step.

**Decision (Chris, 2026-10-23): row G stops here; structuring does not need to be 100%.**
DREAM's purpose is decent-looking, readable C. Its authors note that structuring is lossy: past
the core algorithm it becomes conventions and special cases. Chasing every goto was that trap.
- Correctness is already settled: the gate proves that whatever DREAM emits is equivalent. What
  remains is taste.
- **Deferred:** the house conventions (duplication cap, loop forms, when a goto is clearer),
  defined once, later. Per-project special cases as profile data, not code forks.
- **Deferred with them:** the SNES `.c` cutover, triage of the gate rejects, the
  `passes/structure` move, `native_decompile`, the NES decompiler's 32-bit ext ops and shift
  clobber, and the 3 red `cfg_gate` NA1 subs.

## Row E, KOEI half: cross-title matching (2026-10-23)

**One matcher over decoded bytes.** `vm/koei/match.py` signs a bytecode routine from its decoded
bytes through the one opcode table:
- **Kept:** the opcode (plus the ext byte), immediates, frame offsets, and RAM operands.
- **Normalized to 'A':** ROM operands and every control target.

`vm/koei/nes/match.py` enumerates every sub of every code bank of every NES title through
`titles.bank_view`, with names from each bank's scoped labels.

**Why decoded, not text.** `bc_match` tokenized listing text, and the committed listings name the
same byte differently: ro3k's `PUSH_quick` / `LOADL_quick` vs NA1's `PUSH_qimm`. So ro3k vs NA1
banks 0–2 scored 0 shared, while bank 15, whose listing happens to use the current names, scored 17.

**What the measurement says about the library.** Bytecode twins across titles are few, even
opcode-only:
- About 10–25 between close relatives (na1/ro3k/gk, gk/bk/GemFire).
- Almost none for LEmp, even against NA2, whose engine it uses.
- Normalizing RAM globals changes nothing.

So the game bytecode was rewritten per title. The shared library is a small bytecode core
(`fill_bytes`, `strcpy`, `strcmp`, `char_to_upper`, `muldiv`, percent / clamp math, digit
formatting) plus the native layer: 337 native twin clusters vs 44 bytecode. That matches koei-nes
`docs/shared-library.md`.

**`match family [--layer bytecode|native]`: twin clusters and their names.**
- Each sub is joined to its best match (≥ .90, ≥ 8 tokens) in every other title.
- Each cluster's canonical name is the earliest title's, because games.toml is in release order.
  That is `shared-library.md`'s rule: NA1 is the established vocabulary.
- Every member named otherwise is listed:
  - **RENAME:** identical code.
  - **READ:** a near twin ("shared-but-evolved: cross-ref, don't blind-rename").
  - **FILL:** unnamed here.
- Proposals only; applying them belongs to the title revisits.
- The drift it finds is the drift Chris saw: GemFire `memset` / `to_upper` / `mul_div` vs NA1/ro3k
  `fill_bytes` / `char_to_upper` / gk `muldiv32_signed`, and ro3k `strcpy_bytes` vs NA1 `strcpy`.
- **The earliest-title rule is only a default.** Identical code can carry a caller's purpose as
  its name: gk `win_probability_percent` and rot3k2 `calc_loyalty_gain` for what bk / GemFire call
  `ratio_percent`. And some pairs need a read to know which side is right. Choosing the canonical
  name is inference (open question below).

**The vocabulary: `vm/koei/nes/vocabulary.toml` (Chris: "inference > tool").** A quick inference pass
over the clusters whose names disagree (26 bytecode, 76 native):
- **Format:** each entry names an anchor routine, and every byte-identical routine in any title takes
  its name. Near twins that differ by a constant are separate functions (cap 999 vs clamp 100 vs
  uncapped: `blend_clamped999` / `blend_pct_clamped100` / `blend_unclamped`).
- **Rules:**
  - Name what the code does at its own level (`ratio_percent`, not `calc_loyalty_gain`).
  - Pairs read alike (`char_to_upper` / `char_to_lower`, `*_saturating`).
  - Settle by a lower artifact where one exists. GemFire's 32-bit ALU names were checked against
    its ext-op dispatch table, and `inc32` / `dec32` against the handler code.
  - Otherwise the earliest title stays the default.
- **Reads behind entries:** GemFire's `memcmp` returns 1/0 (so `mem_equal`), and `itoa_append` is
  recursive digit emission.
- **Unsettled:** identical code under contradictory names that no cheap artifact settles stays a
  `[[conflict]]` for the title's revisit (UW's ALU, LEmp/UW `max_s16`/`min_s16`, some boot and
  syscall names).
- **Grounded by construction:** `family` refuses to run if an anchor is not a real sub.
- **Result:** 24 bytecode and 11 native clusters now take reviewed names. Proposals:
  38 + 83 RENAME, 9 + 32 READ, 4 + 163 FILL.

**Folded (each against its old output on the same inputs):**

| old | now | oracle |
|---|---|---|
| ro3k + gk `bc_match.py` (verbatim copies) | `match pair`; both are thin CLIs | 10 bank pairs identical. Named: the header column takes the game's name (it said "ROTK" for gk too); a bank with no match no longer crashes on `None` (banks 10, rc=1 before) |
| GemFire `multi_oracle.py` (6 hardcoded ROM/label/stub triples) | `match oracles <title> --oracles …`, each from its profile; thin CLI | all 3 modes identical. Named: the oracle tag is the profile name (`rot3k2`, was `rotk2`) |
| koei-nes `tools/native_floor.py` | `vm/koei/nes/native_floor.py`; koei-nes keeps an alias | 7 titles × 3 modes identical. Named: the `--toml` banner names its new home |
| ro3k `handler_match` imported by path hack (UW `gen_labels`, `native_banks`; rot3k2 `native_bytecode_called`) | `native_floor.handler_helpers()` (the three emu-harness functions) | every mode of all three identical, `gen_labels`' generated registry included |

ro3k's `handler_match` and `native_match` stay as ro3k's own tools (ro3k vs NA1), and nothing
outside ro3k imports them any more.

## Per-project revisits (after the toolset is complete; one session each)

Chris, 2026-10-23: "focus on the task of creating the toolset ... have each revisit in its own
session after the toolset is complete, otherwise we will keep looping." Gaps the toolset turns up
are recorded here, not fixed in passing. Each revisit is a full audit of that project with the
finished toolset, not just the items below.

| Project | Found | By |
|---|---|---|
| UW-decompile | Native listings for banks 7, 8, 10, 13 and 17 were rendered at `$A000` but run at `$8000`. Recount the 555-sub inventory and the "106 recovered" figure. Re-render. | row N window evidence |
| UW-decompile | games.toml `[uw]` windows wrong for banks 7, 10, 13 and 59 (all `$8000`) | row N |
| UW-decompile | Bank 59: 966 bytes of executed native code with no labels, not in `native_banks` | row B CPU log |
| UW-decompile | `$22B0A` named twice: `nat_17_0b0a` at `$AB0A` (bank 17 runs at `$8000`) vs `fleet_leaf_8b0a` | row N collision |
| UW-decompile | `native_banks.walk_body` is a private copy of the row-E signature | row B residue |
| GemFire-decompiler | Bank 2 is unwalked code at `$8000`: 48 bytecode stubs, no labels, not in `code_banks`. games.toml says `$A000`. | row N |
| na2-decompiler | 139 labels outside their bank's window, mostly `*_data_A0xx` names filed under the `$8000` bank that uses them. They belong under the `$A000` partner bank, and `locate` puts them on the wrong byte; 3 of them displace correct names in the `.mlb`. Bank 28 (sound) runs at `$8000`; the profile says `$A000`. | row N `window_check` |
| bk-decompiler | Bank 28 (sound) runs at `$8000`; the profile says `$A000`. Its 16 `sound_engine_*` labels are right. 7 bank-14 `tbl_*` labels are outside the window. | row N `window_check` |
| rot3k2-decompiler | Bank 28 (sound) runs at `$8000`; the profile says `$A000`. 3 labels outside the window (`gauge_tile_data`, `scenario_ruler_data`, `ruler_stat_table`). | row N `window_check` |
| GemFire-decompiler | Bank 27 (sound) runs at `$8000`; the profile says `$A000`. `native_battle_fx_driver` `$A313` filed under the `$8000` bank 12. | row N `window_check` |
| LEmp-decompiler | Bank 27 (sound) runs at `$8000`; the profile says `$A000`. | row N `window_check` |
| UW-decompile | `disasm_native` now renders 15 banks at their profile window (banks 8 and 17 among the committed listings), so a fresh run differs from the committed listings until the UW revisit regenerates them. 18 `nat_8_*` / `nat_17_*` labels carry `$Axxx` CPU addresses. | row N |
| game-annotation (Mappy) | `jump_tables.py` overwrites the curated `jump_tables.txt` (3 hand-added tables, 673a53d) | row B |
| game-annotation (Mappy) | The committed `coverage.bin` / `codegraph.txt` (4,940 bytes covered, 158 edges) come from an older tracer; the current `trace_codegraph.py 4000` gives 5,024 / 162, the same before and after row C. The static pipeline (`static_expand` ...) is seeded from the stale pair. | row C |
| all KOEI NES titles | Committed `.mlb` files added 2026-10-23; regenerate after registry edits | row A gap |
| all KOEI NES titles | Committed `2-asm-vm` listings say "labels: 0 CPU addresses named"; a regeneration shows the real count | row F1b |
| na1-decompiler | Regenerate `4-c` / `3-c-basic`: the engine restores 19 asset-enum names (`trigger_cutscene(CUTSCENE_…)`, `call_bank10_entry(AUDIO_…)`). `source/README.md` says `decompile_all` no longer runs: it does again. `v2-corpus.py` still loads the long-gone `decompile-all.py`. | row F1b |
| gk-decompiler | `source/4-c/bank_05.c` `$8327` carries NA1's certified `scale_div10_capcheck` formula (leaked by address); a regeneration gives its real body | row F1b |
| UW-decompile | `source/4-c/bank38.c` has `// DECOMPILE ERROR @$8327` (the leaked NA1 fold); a regeneration decompiles it | row F1b |
| gk, ro3k, na2, rot3k2, GemFire, LEmp, UW | Committed bytecode listings and C mis-decode `$B7 $0C`–`$13` (251 uses): operand bytes shown as `UNK` / `op_EB` / `op_FF`, sometimes fake `trigger_syscall_EB_FE` and phantom arguments (ro3k bank 3). A regeneration fixes them. | row F2 |
| UW-decompile | Other banks' committed listings tag calls into the fixed `$C000` bank `{native}`/`{bytecode}` from the wrong bytes (the 16K-bank-15 fixed-region bug in the generic driver). The committed bank 62/63 listings and C read the right bytes: UW made them with its own `disasm_fixed_bytecode` workaround. (Corrected 2026-10-23: an earlier version of this row said they decode banks 30/31.) That workaround's address rewrite also changed 5 immediate operands in `bank62_C000_library.asm` (`$A418` printed as `$C418`); a regeneration with the driver fixes them. | rows F2/F3 |
| ro3k-decompiler | Committed `2-asm-vm` / `4-c` came from the old `vm_disasm_rotk` / `vm_decompile_rotk`: banner names the NA1 ROM, the C uses NA1's ext-op addresses and province field names and unnamed syscalls. A regeneration with the driver (the wrappers now run it) fixes them. | row F3 |
| na1-decompiler | `nobunaga/vm-opcodes.toml` is a legacy opcode table (the old spec's lengths) still read by `analyze-vm-opcodes` / `gen-opcode-appendix`; point them at the shared table and retire it. | row F3 |
| na1-decompiler | 148 fixed-region (`$C000`+) names are filed under `[prg.bank0]` (e.g. `rng_mod` `$CA52`, `min_word`, `draw_message`, `cursor_nav_loop`, `nmi_off`), so bank 15's own scoped view (its listing, its C, the family report) cannot see them. Move them to `[prg.bank15]`. | row E `family` FILLs |
| rot3k2-snes-decompiler, Gemfire-snes-decompiler | Regenerate `src/` (`gen_layers`): with the co-residency now declared, overlay calls into `comroot` / `hex` / `command` are named and `comroot` `$BBF6` is native code; the committed files predate both. | row H2 |
| UW-decompile | `docs/10-call-graph.md` "strict layering / ZERO subsystem→subsystem edges" counted direct `CALL_abs` only; through `call_in_bank_dcd1` 34 subsystem → subsystem edges exist (`callgraph uw --indegree`). Correct the chapter. | row H |
| bk, GemFire, LEmp, ... (all KOEI NES titles) | **The source ladder.** NA1 has every layer: 6502 listing, labeled 6502, bytecode listing, raw bytecode C, DREAM C, and each makes inference easier (Chris, 2026-10-23). Later titles may have lost some. Check each title has every layer, regenerated by the shared tools. | row H (Chris) |
| na1-decompiler | `fill_bytes` `$CA97`, `memchr` `$CAF0` and 5 more bank-15 routines have no caller and no root (`callgraph na1 --orphans --bank 15`; the old index agrees): linked, never called with a constant address. Confirm dead, or find the computed call. | row H |
| na1-decompiler | The 4 `source/1-asm-6502/bank_NN_labeled.asm` were removed from git by `2d2e7ef` ("recreate the source ladder") and exist only as untracked local files, while `CONTEXT.md` and `mesen-labels.py` call them committed and `--verify` checks them. Re-add them (they verify clean). | row A3 |
| elite-decompiler | `apply_labels.py` rewrites `source/asm/` in place, so a renamed label never reaches the listings until they are re-swept: always run the README's sweep first, or write labeled copies beside raw ones. | row A3 |
| UW-decompile | `gen_labels.py` no longer reproduces `mesen-labels.toml` (it writes 1,090 lines; the committed file has 1,468, grown by the walks). Running it overwrites walked names: retire it, or make it merge. | row E oracle |
| UW-decompile | Names that contradict their byte-identical twins: `loop_init_c4dc` `$C4DC` = GemFire `parse_uint`; `incr_deref_field` `$C4FE` = ro3k `emit_decimal_digits_recursive` / GemFire `itoa_append`. | row E `family` |
| rot3k2-decompiler | `native_bytecode_called.py` now finds 0 fixed-region `sub_XXXX` calls in the C (its input looks stale or fully named); check whether it is still needed. | row E oracle |
| all KOEI NES titles | Cross-title name drift: with `vocabulary.toml`, `match family` proposes 38 + 83 RENAME, 9 + 32 READ and 4 + 163 FILL (bytecode + native). Apply per title, then regenerate its `.mlb` / listings / C. | row E `family` |
| GemFire-decompiler | Its 32-bit ALU handler names (`ext_*`) sit one handler off. The ext-op dispatch table puts `$ED4E` at `$14` (push32) and `$EB16` at `$1E` (smod32), but they are named `ext_store_a_frame` / `ext_abs_b_to_a`; 15+ handlers are affected, and the names leak into the C's `// ext_op` comments. Rename from the table. | row E dispatch table |
| bk-decompiler, GemFire-decompiler | `halve_a32` / `halve_a32_rounded` are ext ops `$1B` / `$1C`: the code adds 1 / `$FFFFFFFF` across `$08`, so `inc32` / `dec32`. | row E handler read |
| UW-decompile, LEmp-decompiler | `vocabulary.toml` `[[conflict]]`s: LEmp `max_s16` = UW `min_s16` (identical code); UW `alu_*` names contradict their twins, and several match only on the 64-instruction signature prefix; UW `boot_banking_init` / `boot_init_oam` vs GemFire's names. Read, then settle. | row E `family` |
| ro3k-decompiler | `vocabulary.toml` `[[conflict]]`s: `upload_vram_table_c380` = NA1/gk `reset_ppu_init`; `vm_illegal_opcode` = NA1 `vm_op_trigger_syscall_shared`; `scroll_timer_dec` = gk `sound_channel_timer`. | row E `family` |

## Open questions (Chris)

- ~~**Three registry labels sit on operand bytes inside boot code**: move or drop?~~ **Resolved
  2026-10-23 (Chris: labels mark the start of code):** moved to the instruction their comments
  describe. GemFire `boot_init_sound_channels` `$F5D0`→`$F5D2`, `hide_sprites` `$F8D0`→`$F8D2` (the
  target of the jump at `$F8A5`), gk `boot_init_tail_start_main` `$C0A9`→`$C0A8` (the clear-loop head).
  native_floor output is unchanged. The `.mlb` files of the seven KOEI NES titles are now committed:
  na2, rot3k2, bk, GemFire and LEmp had them untracked, and ro3k and gk had none. na2's still drops 3
  same-byte names (window collisions, row N).

- ~~**nes-disasm6** (row B, deferred): still read by `asm-relabel` / `zp-harvest`?~~ **Resolved
  2026-10-23:** yes. Both read disasm6 listings (na1's `bank_NN.asm`, made with nes-disasm6 per na1
  `tools/EXTERNAL.md`). It is kept as a plugin (`machines/nes/nes_disasm6.py`). Those two readers
  belong to row A3.
- ~~**The canonical routine vocabulary (row E).**~~ **Resolved 2026-10-23 (Chris: "inference >
  tool"; `ratio_percent`, because that is what the low-level function does).** One quick inference
  pass wrote `vm/koei/nes/vocabulary.toml`; see "Row E" above. Chris also expects the sharing to be
  KOEI-specific: UW is the action-leaning outlier, and Zelda / Mappy should share little.
- **na1-snes-decompiler** has no vault pointer (it is now in the path resolver).
- Do the koei-nes and koei-snes repos get **archived** after step 5, as nes-harness was?
- UW-decompile: GitHub remote, public or private? (Its siblings GemFire and LEmp are public.)
