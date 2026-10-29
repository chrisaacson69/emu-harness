# PHASES — the build tool's manual

How to take a disassembly project from a bare ROM to verified, named, presented source with this
library, one phase at a time. Each phase lists **the commands**, **the gate** that ends it, and **the
lessons** that apply. The design and its decisions are on the vault page `method/re-controller.md`; the
full lessons, with their sources, are on `method/re-controller-lessons.md` (each phase below names its
section there).

The phase list itself is `emu_harness/build/phase_table.toml` (keys, gates, and the library paths each gate
depends on); this manual is the prose over it, and `tests/test_build.py` holds the two together.

## How a session runs

1. **`py -3.14 -m emu_harness status <project>`**. The project keeps its state in
   `<project>/re-roadmap.toml` (template: `emu_harness/build/roadmap.template.toml`). Status marks the
   phase to start at: the first that isn't done, that is done but whose gate is **stale** (a commit
   since the gate ran touched the library that gate depends on), or that is done with an **open
   revisit** against it (the item contests the gate; earliest phase first). `--json` gives records.
2. **Read the project's entry docs** (`[project] entry` in its roadmap) before running anything.
3. **Work one phase, or one revisit.** One project per session. Inference runs sequentially; an agent
   per phase at most, never a swarm for cross-talk.
4. **Run the gate.** A gate passes only if it can fail: before trusting a pass, plant a fault and watch
   it fail.
5. **Register and hand back**: update `re-roadmap.toml` (status, the gate's result with its numbers,
   the date, the emu-harness commit from `status`, evidence paths), close or add revisits, write
   `[next] session`, and flow findings to the vault (a topic memory or a research page).

**Oracles grade, never feed.** A published disassembly, a sibling title, or an older pass of this
project grades a finished derivation; it is never the derivation's input. When they disagree, the lower
artifact (the bytes, the running CPU) decides, and it may show the reference was wrong. The aim is to
become the oracle.

**Two tracks.** *vm*: KOEI bytecode titles (profiles in `vm/koei/nes/games.toml`,
`vm/koei/snes/games.toml`). *native*: everything else. Commands marked *library* have no CLI yet: a
title tool calls them (Mappy's `code_map`, `jump_tables`, ... are the worked example), and each is a
candidate verb.

---

## 0. Classify (`classify`)

**Gate:** The one fact that dictates the method (VM or native; turn-based or real-time; is the static-runtime join cheap) is written down with its evidence.

| Track | Commands |
|---|---|
| both | NES header and mapper: *library* `machines.nes.memory.Cart`; SNES: `py -3.14 -m emu_harness.machines.snes.header <rom.sfc>` (checksum ⊕ complement = `$FFFF`, map byte) |
| vm | Fingerprint against the siblings: `py -3.14 -m emu_harness.vm.koei.nes.native_floor <title>` (a VM title shares its fixed-bank interpreter; a fetch-decode loop, a 256-entry dispatch, a per-routine trampoline) |
| native | Run the reset path on the core (*library* `cpu.m6502.cpu`, `machines.nes.bus.Bus`) and look for a frame loop with dispatch tables (real-time) or a walkable call tree (turn-based) |

**Lessons** (lessons page: *Classify*): classify before choosing a method, since a skill transports but a
plan may not; qualify by the artifacts present, never by publisher (NA1-SNES is native among VM
siblings); engine ≠ configuration (L'Empereur: NA2 engine, GemFire-era syscall vector); a broken
static↔runtime join means data-first; a bespoke VM gets our own decompiler.

## 1. Recon (`recon`)

**Gate:** Bank windows come from evidence; VM: every bytecode bank tiles with 0 errors from a walker that can fail, and the shared toolchain's load-bearing assumptions were re-derived on this title.

| Track | Commands |
|---|---|
| both | Vectors and windows: *library* `machines.nes.memory.Windows`, `window_votes` (words that land on a bank's own entries); vm: `py -3.14 -m emu_harness.vm.koei.nes.window_check <title> [-v]` checks a profile against the banks' code |
| both | Strings: *library* `passes.strings`; VM: `py -3.14 -m emu_harness.vm.koei.nes.strings <title> dump <bank>` |
| vm | Seed the profile from the closest sibling, then disassemble: `py -3.14 -m emu_harness.vm.koei.nes.titles disasm <title> <bank>`; SNES: `vm.koei.snes.vm_walk` |
| vm | ISA probes: `py -3.14 tools/koei_isa_probe.py`, `tools/koei_isa_census.py`; compare direction and signedness on the live handler: `py -3.14 -m emu_harness.vm.koei.nes.gates.compare_probe --game <title>` (`--all`) |

**Lessons** (*Recon*): "0 errors" is vacuous unless the walker can fail (ownership conflicts, dead
opcodes, a raising bind); re-derive the shared toolchain's assumptions from executing handlers on each
new title and fix the toolchain first (AeroBiz found `$D8` mis-decoded in two shipped titles); derive
operand sizes from the fetch helpers, never a hand list; read module manifests from the ROM's own
archive; verify each bank's window (six profiles had the sound bank wrong); census the fixed bank for a
second VM-entry stub (`$E2E3` is an ABI, not a dialect); recon corrections can be wrong too; a structural gate cannot see a compare's direction (round-trip and CFG both stay green while the C inverts every threshold), so probe it on the handler (NA1: 323 sites, 2026-06-04; the probe was atticked in a refactor and came back as `gates.compare_probe` in the NA1 revisit).

## 2. Code discovery (`discover`)

**Gate:** 0 walker conflicts; every jump table read in full; phantoms dropped or marked DATA.

| Track | Commands |
|---|---|
| both | Seeded descent: *library* `cpu.m6502.descent.descend`; listing: `py -3.14 -m emu_harness.cpu.m6502.disasm <rom.nes> <bank>` |
| vm | Native floor (signatures against ALL siblings): `py -3.14 -m emu_harness.vm.koei.nes.native_floor <title>` then `--check`, `--toml`; hand-read the residue |
| vm | Census every call method: trampolines, run-bank syscall, co-resident direct calls, function pointers (`py -3.14 -m emu_harness.vm.koei.nes.callgraph <title> --indegree`) |
| native | Trace → descent → idioms: *library* `cpu.m6502.coverage.trace_coverage`, then `passes.native.dispatch_tables` (`inline=` for dispatchers that read the words after their JSR), `stored_pointer_installs`, `pointer_runs` to a fixpoint |
| both | Measure the gate: *library* `NativeMap.conflicts()` after the final reach (overlaps + halts, from `Descent.overlaps` / `Descent.halts`), each passed through `passes.native.explain`; anything it cannot name is unexplained |

**Lessons** (*Code discovery*): about a third of native "residue" is phantoms (`20 xx xx` in data):
drop or mark DATA, never name; seeded descent beats a linear sweep; don't rely on one call method (UW's
"0 subsystem edges" was 34); 100% of the walk is not 100% of the program when dispatch is indirect; a
true entry carries the prologue (a header is not the sub); a label sits on an instruction start;
real-time order is trace (30%) → descent (60%) → jump-table sweep (96.7%). From the Mappy revisit
(2026-10-26): a table reader must stop at its own handler and at executed code (Mappy's `$C356` read
its last handler's `lda #$ae` as a 20th entry, inventing two routines that were then *named*); a JSR
to a dispatcher that pops its return address never returns, so the bytes after it are a table, not
code (18 handlers found); an "install" is a handler only if a `JMP (ind)` reads its variable (all 4
of Mappy's were data pointers, counters or mis-pairs); a 2-byte routine with JSR callers is usually
a fall-through entry, not a phantom: check the trace before marking DATA. A `JMP (ind)` can sit in a
shared stub reached by JSR, with the table load in the caller (Mappy's `$EA05` → `$EA46`: 63 B of
door handlers stayed data, recall 99.4%; found in naming by a Mesen write-watch on the state byte,
whose writer PC was in "data"); the fix resolves a stub at each caller's pointer-pair load. Close the
gate with a CDL over real play: executed bytes the listing does not decode are holes, by definition
(Mappy, three play logs: that was the only one).

## 3. Data, pure lane (`data`)

**Gate:** Oracle lanes ran deterministically (strings, manual schema, ~20 hot globals); schema is per title, offsets stay field+N until code proves them.

| Track | Commands |
|---|---|
| vm | `py -3.14 -m emu_harness.vm.koei.nes.strings <title> pure --toml`; `...dataxref <title> strings --toml`, `tables --toml`, `hot --unlabeled`, `ram`; `...romdata <title> records` |
| vm (SNES) | `vm.koei.snes.data_walk`, `vm.koei.snes.strings_snes` |
| native | Record fields: *library* `passes.native.field_census`; RAM use from the title's xref tool (Mappy `var_xref`) |
| both | The manual as schema oracle: `py -3.14 tools/manual_to_images.py <manual.pdf> <out_dir>` (scanned PDFs have no text layer; `.[manual]` extra) |

**Lessons** (*Data*): front-load the pure-oracle data (it makes the code legible); three lanes, ORACLE
and PROVENANCE deterministic, ANALYTICAL through propose→verify (that lane belongs to Naming); the
manual is the schema oracle for meaning, the code for layout; schemas don't transfer between sibling
engines (NA1's default mislabelled 26 BK subs); confirm a field when a data value and a code role agree;
name dedicated semantic globals, leave scratch registers generic; one or two matching observations can
be coincidence.

**When to stop** (Chris, 2026-10-26): the phase exists so the code walk can read. Readable data (text,
records) gets real names; opaque data (level, sound, graphics, jump tables) only needs a known start
and kind, and the code that reads it says what it is. It does not have to be perfect: stop when every
data region has a start and a kind. Native track: run the title's own walkers on a read-logging bus
for exact extents and text, and drive its sound engine one trigger at a time (Mappy `rom_data.py`).

## 4. Structure (`structure`)

**Gate:** Primitives probed against the live handler; VM round-trip byte-identical; the rendered C equals the ROM by value on the title's fixtures; one call graph with a leaves-first order.

| Track | Commands |
|---|---|
| both | Call graph and order: *library* `passes.callgraph` (`leaves_first`, `layers`, `bank_order`); VM: `py -3.14 -m emu_harness.vm.koei.nes.callgraph <title> --bank-order` |
| vm | Round-trip: `py -3.14 -m emu_harness.vm.koei.nes.gates.roundtrip --game <title>`; stack effects: `...gates.stack_audit`; CFG: `...gates.cfg_gate` |
| vm | Primitives against the live handler: `py -3.14 -m emu_harness.vm.koei.nes.titles emulate <title> <0xSUB> [args]` |
| vm | Rendered C against the ROM: `py -3.14 -m emu_harness.vm.koei.nes.gates.value_probe --game <title>` (the printed C of small subs, evaluated, vs `titles.run_sub`; calls run the callee on the ROM, so argument order counts; add fixtures per title) |
| native | Partition edges: *library* `passes.native.partition_edges`, `reachable` |

**Lessons** (*Structure*): verify the primitive layer before building on it (one compare fix cleared
996 sites; 323 inverted compares had passed every structural gate); ground the opcode table in three
sources; transpile the weak language to C and reason in C; a gate that passed 37 non-compilable subs
needed an emit-integrity check; unconsumed call results vanish from stack-VM C; install a gate the
moment a ground truth and a cheap check exist; every structural gate can pass while the printed C is
wrong (the compare names, the `$8B` sign, the textual inverter), so one gate must read the C itself.

## 5. Naming (`naming`)

**Gate:** Bottom-up then top-down passes done; proposals verified at a lower artifact; the guard holds; no placeholder names.

| Track | Commands |
|---|---|
| both | The walks (below): the title's prep → `workflows/{label,data,var}-walk.workflow.js` with the title's `walk-context.json` as `context` → the title's apply (writes, regenerates, guards). Deterministic half: *library* `walk.prep` (frontier, levels, clusters) and `walk.guard` (only renames; exit 2 = stop) |
| vm | Cross-title drift: `py -3.14 -m emu_harness.vm.koei.nes.match family` (proposals; the title's revisit applies them) |
| native | Routine signatures for naming: *library* `passes.native.routine_listing` |

**The walks.** Three walks name a title's symbols: **label** (anonymous routines), **data** (ROM tables and
blobs, RAM variables) and **var** (positional frame slots, VM track). Each is a loop of batches, and the
regeneration between batches is the fixpoint: new names reach every use, so the next batch reads less
anonymous code. Run leaves first, so a routine is named after its callees.

- **Target.** Name the title by its logical name and resolve its path through the vault's
  `.claude/local-paths.md`; a missing entry means ask, never guess. A title qualifies by the artifacts
  it has, not by publisher or console: a symbol table (the single source of truth), a readable layer,
  a **lower layer** for the verifier, a prep / apply pair, and a `walk-context.json` (title, layer
  paths, xref commands, caveats; NA1's is the worked example). **No lower layer, stop:** the verifier
  must read a different, lower artifact than the proposer, or the run only rubber-stamps.
- **Per batch.** (1) The title's **prep** emits the next batch as JSON (leaves first, with the named
  context as `seeds`; pass bank-specific landmarks once). (2) **Workflow**:
  `Workflow({ scriptPath: "<emu-harness>/workflows/<walk>-walk.workflow.js", args: {...prep, context:
  <walk-context.json>} })`: propose from the readable layer, then an independent verifier at the lower
  layer; it returns verdicts and writes nothing. (3) The title's **apply** writes the survivors,
  regenerates, runs `walk.guard` (pure renames, no cross-bank bleed; exit 2 = stop and inspect) and
  commits. The verifier is the check, so applying its verdicts is not a shortcut; the guard is the only
  gate on the write.
- **Data.** Three lanes, deterministic first. *Oracle*: strings, whose decoded text is the ground truth.
  *Provenance*: tables and blobs named after the named routine that reads them (`<reader>_data_<addr>`,
  vague but correct), then the blobs behind each labelled pointer table, counted by what the reader
  indexes. *Analytical*: only the residue that deserves a real name goes to `data-walk`. Done when the
  title's frontier (every operand into unlabelled data, grouped by base with its named readers; a field
  of a labelled record is not a gap) is 0.
- **Var.** Name a slot only if it has one role across the whole routine; a reused slot stays positional.
  Evidence is the routine body and caller propagation (what each caller pushes). `single: true` fuses
  propose and verify into one agent per routine: half the agents, no independent verifier, so the
  coordinator skims the verdicts before apply.
- **Native track.** The same loop, without the workflows when the routines are few: the coordinator
  reads the code, a census (the emulator's write-watch, a CDL, a warp) is the lower layer, and the
  title's guarded apply writes (Mappy: `apply_names.py`, `apply_vars.py`, `data_frontier.py`,
  `data_pointers.py`).

**Lessons** (*Naming*): the symbol table plus regeneration is multi-pass compilation backwards; leaves
first, then a top-down verify (a routine's first ops lie about its role); propose from the C, verify at
the bytecode, the root writes survivors; with no lower oracle, stop; generic-but-correct beats
specific-but-wrong; no address-keyed placeholders; never overwrite a name in apply; recursive routines
never reach the frontier, so name them directly; "did I name what I found?" is an exit item.

## 6. Presentation (`presentation`)

**Gate:** Every rung of the source ladder this track has is regenerated by the shared tools and matches what is committed.

| Track | Commands |
|---|---|
| both | Labelled listing: *library* `passes.listing`; Mesen labels: *library* `backends.mesen.labels`, `tools/gen_mlb.py`, `tools/gen_mlb_koei_snes.py`, `tools/mlb_compare.py` |
| vm | Bytecode listing `...titles disasm`; raw and DREAM C: `py -3.14 -m emu_harness.vm.koei.nes.cli.decompile_all` (`--basic` for the goto form, `--self-check`); SNES: `vm.koei.snes.gen_layers`, `gen_source` |

The ladder (vm): 6502 listing → labelled 6502 → bytecode listing → raw C → structured C. Native: listing
→ labelled listing.

**Lessons** (*Presentation*): ship the whole ladder, readable by default, raw as witness; the derived
C is a deliverable; structuring is an epic, stop at good enough (G1); a comment's value is its distance
to the oracle (`WHY:` / `AGENT:` marks); regenerate committed outputs after tool changes, and never let
a generator clobber a curated file; publish conversions and analysis, never the ROM or manual.

## 7. Assets (`assets`)

**Gate:** Renders come from ROM bytes through the library and match the live PPU; the game's own sound engine, run headless, writes what the live APU receives, write for write.

| Track | Commands |
|---|---|
| both | *library* `machines.nes.ppu`, `machines.nes.canvas` (the virtual-canvas contract) |
| both | **Sound:** `py -3.14 -m emu_harness.machines.nes.sound <adapter.py> [--wav DIR] [--json OUT]`. The title writes only an engine adapter (`machine` / `init` / `request` / `tick` and its `events`); the library taps the bus, captures, finds loops and renders (`machines.nes.apu`). Mappy: `sound_capture.py`; NA1: `nobunaga/tools/sound_engine.py`. The gate is the title's `sound_verify`: Mesen's watches replayed headless. |
| vm | `vm.koei.nes.assets.*` (`chr_extract`, `portrait`, `province_map`, `strategic_map`, `animation`, `text`); for sound, `NobunagaVM.native_syscalls` (run the audio syscalls through the ROM's kernel) and `Memory.mmc1_serial` (native code that banks itself) |

**Lessons** (*Assets*): render from ROM bytes with the real upload arguments, capture at runtime when a
static hunt fails; title tools call the library for formats, decode and palettes (there were four
palette copies, one wrong); the oracle is the live PPU. Sound: never decode a title's music format to
play it, run its engine, so a title owes only how the game drives it (Mappy: a trigger byte; NA1: a
bytecode request plus a native NMI tick); hold (how long the game requests) and loop (the stream
repeats by itself) are different facts; a stand-in the VM uses for a syscall is not the handler (NA1's
`audio_control` stub never armed a voice); rendered audio stays out of public repos.

## 8. Live verification (`live`)

**Gate:** The game's own routine runs over live RAM for every input, and a planted fault fails the check.

A source of oracles from phase 1 on, not only a last step.

| Track | Commands |
|---|---|
| both | *library* `backends.mesen` (client, `tracelog`), `machines.nes.live` (shot / write / state / watch), `machines.nes.trace`, `machines.nes.bus` (headless); `tools/replay_log.py`, `tools/trace_diff.py` |
| vm | Run the handler over loaded SRAM: `...titles emulate`; NA1's snap protocol (`/nobunaga`) |

**Lessons** (*Live verification*): run the game's own routine on the verified CPU over live RAM for
every input, then plant a fault (Zelda: 128/128 against 4 walked by hand); read data and code first,
experiment to confirm; fix the instrument, not the game; oracle tests prove reach (branch coverage, a
mutation, fail on an empty glob); a nil callback enum never fires in Mesen Lua; never capture the
screen.


## 9. Documentation (`docs`)

**Gate:** Every name-address pair a current-state page quotes matches the registry (doccheck: 0 moved, 0 renamed), the pages and the site regenerate from committed sources, and nothing published points at a retired file.

The first gate of the **companion track**: what is done *with* the decompile (mechanics write-ups, play
tools, the site) rather than the decompile itself. Hand-written pages go stale when the walk renames what
they quote; the gate checks the claims a page makes, against the registry.

| Track | Commands |
|---|---|
| both | `py -3.14 -m emu_harness.build.doccheck <project> --docs <globs>`: each `name` ($ADDR) pair is ok, inside (a citation within the routine), moved, renamed or unknown. Scope it to current-state pages (chapters, appendices, command pages); logs and theses quote old names on purpose. |

**Lessons** (*Documentation*): a page's claim that can be checked is the name-address pair; a citation
inside a routine is not a wrong address; generate what can be generated (comments from the symbol files
cannot drift); a site builder that copies but never prunes keeps retired files published.

## Project layout

Every title repo has the same shape, so the harness, the walks and a reader find things in the same
place (adopted 2026-10-29; NA1 moves to it next, Mappy and the KOEI siblings at their revisits):

```
<repo>/
  README.md  re-roadmap.toml  mesen-labels.toml (or labels.toml)  <title>.mlb
  source/    the generated ladder: asm, labelled asm, VM listing, C
  docs/      hand-written pages: chapters, appendices, command pages (the doc gate's scope)
  assets/    committed renders; sound/ gitignored (the title's music)
  tools/     the title's tools and adapters (sound_engine, sound_verify, ...)
  site/      a site generator's templates and config, for a title that publishes one
  traces/    captures, gitignored
  .github/workflows/pages.yml   a published site is built by Actions, never committed
```

Rules: a tool resolves paths from its own file, never the working directory; a site is a build output
(a committed copy duplicates the assets and keeps retired files published); the ROM, savestates and
rendered audio are never committed.
---

## Status values

| Field | Values |
|---|---|
| phase `status` | `done`, `partial`, `todo`, `n/a` (a phase the track or the project's scope doesn't have; say why in `gate`) |
| freshness (computed) | `fresh`; `stale` (a later commit touched the gate's tools, uncommitted changes there, or `toolset = ""`: ran before emu-harness); `unknown` (git could not answer) |
| revisit `status` | `open`, `closed` (with `closed = "date: where"`; never delete one) |
