# CONTEXT — koei-snes entry point

> **Moved from koei-snes on 2026-10-26** (koei-snes is being retired; its toolchain moved to
> emu-harness in MIGRATION.md row F, SNES half). Kept verbatim. Paths below predate the move:
> `tools/<x>.py` = `emu_harness.vm.koei.snes.<x>`, `tools/games.toml` =
> `emu_harness/vm/koei/snes/games.toml`, `data/vm_opcode_names.toml` =
> `emu_harness/vm/koei/vm_opcode_names.toml`, `tools/gen_mlb.py` = emu-harness `tools/gen_mlb_koei_snes.py`,
> `snes-decompiler` = `emu_harness.cpu.w65c816` + `emu_harness.machines.snes`.

> **2026-10-23: the toolchain moved to emu-harness** (its MIGRATION.md row F, SNES half). The code
> lives in `emu_harness.vm.koei.snes`; the opcode table is `emu_harness/vm/koei/vm_opcode_names.toml`
> (shared with the NES side); the profiles are `emu_harness/vm/koei/snes/games.toml`; the substrate is
> `emu_harness.cpu.w65c816` + `emu_harness.machines.snes`. Every `tools/*.py` here is an alias that runs
> or imports the moved module, so the commands below work unchanged (`gen_mlb.py` runs emu-harness
> `tools/gen_mlb_koei_snes.py`). Paths below that say `tools/games.toml` / `data/vm_opcode_names.toml`
> mean those new locations.

**What:** the shared KOEI SNES engine library — the reversed register-machine bytecode
VM + native 65C816 kernel, plus the title-agnostic toolchain to decode any KOEI SNES
title. SNES sibling of `koei-nes`. Extracted from the ROTK2-SNES reverse-engineering.

**Where it sits:** sibling of the vault and of `snes-decompiler` / the title repos.
Depends on `snes-decompiler` (CPU/hardware substrate). Title repos (`rot3k2-snes-decompiler`,
`Gemfire-snes-decompiler`, `AeroBiz-decompiler`) depend on this.

## Read order
1. This file.
2. `docs/engine.md` — the engine architecture (BIOS / resource archive / ABI / VM / syscalls) and
   the conventions of the three generated layers.
3. `docs/koei-nes-vs-snes.md` — what ported vs what was rebuilt vs the NES.
4. `data/vm_opcode_names.toml` — the opcode table: THE one source of opcode names, control-flow
   classes, operand layouts and lift templates (+ its CHANGELOG). Read only via `tools/vm_isa.py`.
5. `tools/games.toml` — per-title addresses + identity + residency; `tools/profile.py` loads them.

## State
- ✅ Native engine fully mapped: bank $00 code reachable from reset + NMI/IRQ/BRK + every ABI
  trampoline target + every VM/syscall entry (`native_floor.py`).
- ✅ Bytecode VM: 256-entry primary + 48 extended tables; **217 live primary** (incl. the `$B7`
  prefix; 39 dead: 38 at `invalid_handler` + `$FF` at +2) and **47 live extended**. Handlers are
  identical in all three titles. Names corrected 2026-09-30 against the Aerobiz recon #1
  verified table (frame-relative vs absolute addressing, complement-named compares, div/mod,
  `$D8` = `jz a16`, `$E8` = `bz' r8`, `$E9` = `call.p`, the 32-bit ext set) — see the CHANGELOG.
- ✅ Sizes derived from each ROM by all-paths handler analysis (`vm_opcodes.py`) and
  cross-checked against the table at load (`vm_isa.bind` raises on any mismatch — also on a
  profile `fetch_helpers` size or dead-slot claim the ROM contradicts, on a switch layout whose
  field order / roles the handler contradicts (`$D5` bias/n/default, `$D9` n/value/target), on a
  conditional branch whose polarity the handler contradicts (which regA value — zero / non-zero —
  makes `$D7/$D8/$E4/$E5/$E7/$E8` write the PC), and on
  a lift template whose regA/regC (L/R) reads / writes the handler contradicts — the templates are
  the lifter's register model). 0 mismatches on rot3k2, Gemfire and Aerobiz.
- ✅ No default profile: tools take `--game`, else `$KOEI_SNES_GAME`, else the ONE profile whose
  `header_title` matches the ROM header; every selection is checked against the ROM
  (`profile.check_rom`: header title + size). `gen_layers` requires an explicit profile.
- ✅ Module walker (`vm_walk.py --all`): 0 conflicts, 0 dead-op hits, 0 rejected candidates on
  all three titles — Aerobiz 22 modules / 479 routines / 83,607 insns (= the recon's reference
  walker exactly), Gemfire 18 / 670 / 51,852, rot3k2 29 / 1,358 / 75,326. Syscall stubs are
  recognised from the ROM (a native stub that `JSL`s to the profile's `abi_syscall`).
- ✅ Layered source: `.src` lists every byte of a module (walker-classified: bytecode with all
  operand values, native code decoded by recursive descent, `db` data); `.c` lifts every walked
  routine over its CFG — join-safe values, liveness-based call results (0 wrong drops on all three
  titles under an independent model), C-order arguments (arg1 = last pushed), `(word)` where a
  16-bit consumer reads a 32-bit value, explicit `(s32)` / extension casts, previews hoisted to a
  trailing comment. Syscall ids in both layers come from one dataflow over the routine CFG (every
  path's id: `sys_a ($05) | sys_b ($1E) (id by path)` where a jump brings another id), checked
  against an independent backward search on all 5,849 syscall sites of the three titles.
- ✅ Syscall tables named to hardware per title (via `snes_regs`); the 22-byte-param ABI.

## Toolchain (decode → symbol table → layered source)
- `vm_isa.py` — the ONE opcode-table loader + shared decoder + operand renderer; `vm_opcodes.py` — ROM-derived sizes/classes/operand roles.
- `archive.py` — the resource-archive directory (profile `resource_archive`) + code-module detection.
- `native_floor.py` / `vm_walk.py` / `vm_callgraph.py` — reachability, code-vs-data checks, call graph (→ `data/<mod>/routines.toml`).
- `listing.py` — one instruction → listing lines (shared by `gen_source`, `vm_walk --list`, `vm_disasm`).
- `vm_disasm.py` — linear disassembler; `syscall_probe.py` / `data_walk.py` — syscall naming + string recovery.
- `symbols.py` — a module's names + its resident and coresident modules' names (profile required).
- **Layered source (the deliverable):** `gen_layers.py <rom> <title-repo> --game g` drives all three
  layers from the ROM's archive — `gen_native.py` (assembly: native kernel), `gen_source.py`
  (bytecode: per-module `.src`), `vm_lift.py` (C: `.c`; `vm_struct.py` = goto-less view). `gen_mlb.py`
  emits Mesen labels (merging the modules here; the file format is emu-harness's shared Mesen
  writer, so it needs `py -3.14` with `pip install -e ../emu-harness`). Output lands in the title repo's `src/` (see its `src/README.md`).

## Next
- Aerobiz module walk (label-walk): proposer reads the per-module `.c`, verifier reads the `.src`.
- The ROTK2 / Gemfire re-grade below, when Chris schedules it.

## Re-grade backlog: ROTK2 / Gemfire SNES (deferred by Chris, 2026-09-30)
The 2026-09-30 toolchain fix changed what the tools output for every title. Chris decided the two
finished titles are NOT regenerated or re-named now: their repos stay as shipped (read-only), and
the toolchain only has to run correctly on their profiles (it does: 0 opcode mismatches, 0 walk
conflicts / dead ops). What a re-grade would have to redo, with blast radius:

- **Shipped `src/*.src`, `src/*.c`, `*.mlb` are stale** (both repos; produced by the old ISA,
  reproducible byte-for-byte from koei-snes HEAD 16d8cd8):
  - `$D8` decoded as `brz r8`: every `$D8` target is wrong and the address high byte decodes as a
    phantom instruction. Counted over the modules with a shipped `.src` (rot3k2 29, Gemfire 11): the
    walker reaches 2,821 / 1,354 `jz` sites; the shipped listings print 2,791 / 1,353 of them (the
    rest fell inside an earlier desync), 2,780 / 1,346 with a wrong target, and 2,846 / 1,355 phantom
    lines start inside them (method: the round-2 `count_d8.py` — reachable `$D8` by `vm_walk`, an old
    line at the same address = in sync, its `; -> $XXXX` vs the a16, old lines at +1/+2 = phantoms);
  - renamed opcodes in every listing (the CHANGELOG mapping: compares, div/mod, `$BD`, `$DE/$DF`,
    `$E9`, frame-vs-absolute operands, the ext set);
  - `$E8` (a switch before) / ext `$18/$19` (sizes 2 and 0 before) desync the rest of a line run;
  - `.c`: call arguments were in push order (arg1 = the LAST-pushed word), call results were dropped
    without control flow, values leaked across labels in address order, and the 800-instruction
    guard cut long routines; no `(word)` truncation marks (rot3k2 56 / Gemfire 7 sites), signed
    32-bit ops not cast `(s32)`;
  - syscalls a jump reaches with another id: rot3k2 root `$4D3F` and hex `$8CEC` (`$05` / `$1E`),
    Gemfire sensou `$8BA6` (`$26` / `$50`) and open `$5FD6` (`$05` / `$26`) — the shipped `.src`
    names only the stub, the shipped `.c` one syscall (sensou.c `sys_scroll_path_a(...)`, which is
    `sys_scroll_path_b` when arg5 != 0);
  - the old `.src` was a linear sweep: data decoded as instructions, no operand values.
- **Routine names inferred from the old misreads** need re-checking (D8 desync, inverted compares,
  `$E9` "hostcall"). Concrete case: rot3k2 comroot `$899B eval_officer_action_899B` — its description
  says it calls build_prov_list / select_officer / copy_officer_state; with the correct `jz` it is a
  pure leaf (a linked-list sum ending at `ret $89C6`); comroot leaves go 12 → 15.
- **"Globals" keyed on frame offsets** (named from f16 operands of `$81-$88/$A0-$A3/$DE`, which are
  frame-relative): rot3k2 and Gemfire root/globals.toml `0xFEFE hdma_write_ptr` (rot3k2 97 sites,
  Gemfire 23), rot3k2 `0xFEBB vram_write_pos` (10 sites, all f16 ldA/stA), Gemfire `0xFF00
  battle_grid` (keeps its a16 uses, loses its frame-operand hits). These label frame locals, not WRAM.
- **Gemfire's 7 never-decompiled bytecode modules** — `open`, `sntanm1-5`, `sendisp` are JMP-header +
  `JSR $3287` code modules (79 routines, 14,089 insns by `vm_walk --all`; 0 conflicts / dead ops),
  contradicting Gemfire `src/README.md` ("strings-only" / "data blobs"). gen_layers lists them as not
  generated (no `data/<name>/routines.toml`).
- **Missing coresident maps:** no `[rot3k2.coresident]` / `[gemfire.coresident]`, so 1,053 rot3k2 and
  235 Gemfire call sites land outside resident + self (`vm_walk.py --all --calls`): rot3k2
  excsub1-6 / csovl1-7 → comroot, hsovl1-6 / hsovl51/53 → hex, hsovl51-52 → hsovl5; Gemfire
  comusr1/comusr2/comcmp → command, sntanm1-5/sendisp → sensou (~90 rot3k2 sites hit no routine).
- **Profile values:** Gemfire `run_vm_entry = $A340` is the loop head; the routine prologue is `$A2FC`
  (the `$8000` ABI target, = rot3k2 `$9A4C` / Aerobiz `$A29B`) — so gemfire.mlb puts `vm_run_entry`
  and `vm_interp_loop` on the same address. rot3k2 `interpreter_loop = $9A8F` points at the PLB one
  byte before the loop head (`vm_opcodes.loop_addrs` accepts both spellings in one place).
- **rot3k2 `data/{sdata,cbmdata1,hpoint,endm}/DATA.md` triage commands** (`vm_callgraph --start
  01:FC35 --base 7E:0003 --scan 0xE13A --unnamed` etc.) no longer run: the CLIs are archive-bound and
  refuse a `--start` outside a code module. The conclusion still holds — `archive.py --code` does
  not list those blobs — so the DATA.md files should cite that instead.
- **gemfire.mlb native labels** in the shipped file carry rot3k2 addresses (PRG 1A8F/1A4C/C1D/1636,
  old gen_mlb hard-coded rot3k2); regenerating gives Gemfire's (2340/2340/E64/1B64).
- **Native stub decode is conservative after calls** (`gen_source.native_walk`): decoding continues past
  a `JSR`/`JSL` only when the callee's exit M/X widths are unique (REP/SEP, PHP/PLP per path, nested
  calls). rot3k2 `hex` has 4 such stops (`$C02A`, `$C4D0`, `$C57D`, `$C6BC`): helper `$C060` branches
  into the preceding routine's shared `PLP; RTL` epilogue at `$C055`, whose flags were pushed by a
  caller the static walk cannot see, and the unknown propagates to `$C00A`/`$C526`/`$C48B`. Those
  regions list as `db` (as before the round-2 native decoder); Aerobiz and Gemfire decode unchanged.
