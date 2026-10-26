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
| vm | ISA probes: `py -3.14 tools/koei_isa_probe.py`, `tools/koei_isa_census.py` |

**Lessons** (*Recon*): "0 errors" is vacuous unless the walker can fail (ownership conflicts, dead
opcodes, a raising bind); re-derive the shared toolchain's assumptions from executing handlers on each
new title and fix the toolchain first (AeroBiz found `$D8` mis-decoded in two shipped titles); derive
operand sizes from the fetch helpers, never a hand list; read module manifests from the ROM's own
archive; verify each bank's window (six profiles had the sound bank wrong); census the fixed bank for a
second VM-entry stub (`$E2E3` is an ABI, not a dialect); recon corrections can be wrong too.

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
a fall-through entry, not a phantom: check the trace before marking DATA.

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

**Gate:** Primitives probed against the live handler; VM round-trip byte-identical; one call graph with a leaves-first order.

| Track | Commands |
|---|---|
| both | Call graph and order: *library* `passes.callgraph` (`leaves_first`, `layers`, `bank_order`); VM: `py -3.14 -m emu_harness.vm.koei.nes.callgraph <title> --bank-order` |
| vm | Round-trip: `py -3.14 -m emu_harness.vm.koei.nes.gates.roundtrip --game <title>`; stack effects: `...gates.stack_audit`; CFG: `...gates.cfg_gate` |
| vm | Primitives against the live handler: `py -3.14 -m emu_harness.vm.koei.nes.titles emulate <title> <0xSUB> [args]` |
| native | Partition edges: *library* `passes.native.partition_edges`, `reachable` |

**Lessons** (*Structure*): verify the primitive layer before building on it (one compare fix cleared
996 sites; 323 inverted compares had passed every structural gate); ground the opcode table in three
sources; transpile the weak language to C and reason in C; a gate that passed 37 non-compilable subs
needed an emit-integrity check; unconsumed call results vanish from stack-VM C; install a gate the
moment a ground truth and a cheap check exist.

## 5. Naming (`naming`)

**Gate:** Bottom-up then top-down passes done; proposals verified at a lower artifact; the guard holds; no placeholder names.

| Track | Commands |
|---|---|
| both | The walks: vault skills `/label-walk`, `/data-walk` (provenance + analytical lanes), `/var-walk`; their deterministic half is *library* `walk.prep` (frontier, levels, clusters) and `walk.guard` (only renames; exit 2 = stop) |
| vm | Cross-title drift: `py -3.14 -m emu_harness.vm.koei.nes.match family` (proposals; the title's revisit applies them) |
| native | Routine signatures for naming: *library* `passes.native.routine_listing` |

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

**Gate:** Renders come from ROM bytes through the library and match the live PPU.

| Track | Commands |
|---|---|
| both | *library* `machines.nes.ppu`, `machines.nes.canvas` (the virtual-canvas contract) |
| vm | `vm.koei.nes.assets.*` (`chr_extract`, `portrait`, `province_map`, `strategic_map`, `animation`, `text`) |

**Lessons** (*Assets*): render from ROM bytes with the real upload arguments, capture at runtime when a
static hunt fails; title tools call the library for formats, decode and palettes (there were four
palette copies, one wrong); the oracle is the live PPU.

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

---

## Status values

| Field | Values |
|---|---|
| phase `status` | `done`, `partial`, `todo`, `n/a` (a phase the track or the project's scope doesn't have; say why in `gate`) |
| freshness (computed) | `fresh`; `stale` (a later commit touched the gate's tools, uncommitted changes there, or `toolset = ""`: ran before emu-harness); `unknown` (git could not answer) |
| revisit `status` | `open`, `closed` (with `closed = "date: where"`; never delete one) |
