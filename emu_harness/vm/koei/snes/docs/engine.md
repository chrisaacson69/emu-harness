# The KOEI SNES engine

> **Moved from koei-snes on 2026-10-26** (koei-snes is being retired; its toolchain moved to
> emu-harness in MIGRATION.md row F, SNES half). Kept verbatim. Paths below predate the move:
> `tools/<x>.py` = `emu_harness.vm.koei.snes.<x>`, `tools/games.toml` =
> `emu_harness/vm/koei/snes/games.toml`, `data/vm_opcode_names.toml` =
> `emu_harness/vm/koei/vm_opcode_names.toml`, `tools/gen_mlb.py` = emu-harness `tools/gen_mlb_koei_snes.py`,
> `snes-decompiler` = `emu_harness.cpu.w65c816` + `emu_harness.machines.snes`.

The shared engine behind KOEI's SNES strategy titles — a **register-machine bytecode
VM** driven by a native 65C816 kernel. First reversed from *Romance of the Three Kingdoms II
(USA)*, then *Gemfire* and *Aerobiz*; the opcode handlers are relocation-invariant identical
in all three (303/304, the one difference being the loop-return constant the `$B7` dispatcher
pushes). This doc is the title-agnostic architecture; per-title addresses live in
`tools/games.toml`. It is the SNES sibling of the `koei-nes` engine.

## Layers (outermost = hardware)

1. **BIOS / boot** — native, fixed bank `$00`. RESET → native-mode + PPU/DMA/CGRAM/OAM
   init, then load the first resident module into WRAM and enter it.
2. **Resource archive + WRAM overlays** — a named-blob directory in bank `$00` at the
   profile's `resource_archive` (rot3k2/Gemfire `$F800`, Aerobiz `$F000`): 16-byte rows
   `[name: 8 ASCII][bank: u16][addr: u16][len: u32]`, ending at the first all-zero row (no row
   cap — Aerobiz has 223 rows; its `AMAP` blob is longer than 64 KiB). Parsed by
   `tools/archive.py`. Blobs are block-copied into WRAM on demand. A bytecode module's WRAM
   base is its header `JMP base+3`, followed by its first routine's trampoline. Modules share
   fixed WRAM **overlay slots** — the segmented-memory equivalent of MMC5 bank-switching.
   The **resident** modules (profile `resident_modules`: `root` in rot3k2/Gemfire; `COMMON` +
   `ALNMAIN` in Aerobiz) stay loaded; an overlay may also call modules **co-loaded** with it
   (`[<game>.coresident]`, e.g. an Aerobiz `$D000` command app → `COMMAND`, `DIORAMA`).
3. **The ABI** — a `JMP`-trampoline table at `abi_table` (`$00:8000`, up to `reset`): the VM
   services the modules call via `JSL` — **run-VM** (`$8000`), **syscall** (`$8003`, the profile's
   `abi_syscall`), **ext-op** ×2 (Gemfire adds a fifth, a syscall twin). `native_floor.py` seeds
   every one of them.
4. **The bytecode VM** — `interpreter_loop`: `SEP #$20; LDY #0; LDA [$06],Y` (the opcode at PC)
   `; REP #$20; INC $06; AND #$FF; PHA; ASL; TAX; LDA primary_table,X; STA $00; PLX; JMP ($0000)`
   — so a handler starts with Y=0, m16/x16 and X = the opcode (restored by the `PLX`). 256-entry
   `primary_table`: **217 live opcodes + 39 dead** (38 slots at `invalid_handler`, a `BRK`, and `$FF`
   at `invalid_handler+2`, also a `BRK`). Opcode `$B7` = prefix → `ext_interpreter` → the 48-entry
   `extended_table` (47 live 32-bit ops; entry `$00` is null, bytes ≥ `$30` index past it).
5. **Per-routine trampoline** — every bytecode routine starts with a 5-byte native stub
   (`routine_sig`, e.g. `JSR $1C10` + a signed frame-size word); the body is at
   `routine_body_offset` (+5). Calls target the stub; the stub enters `run_vm_entry`.
6. **Syscalls** — `syscall_dispatch` reads the first byte of a 22-byte parameter block
   into a work buffer, then `JSR (syscall_table, X)` — the native hardware/game-state API.
   Bytecode reaches it through `call.p` to a native stub in a resident module whose first
   instruction is `JSL $00:abi_syscall` (Aerobiz COMMON `$1C14`, Gemfire root `$328B`, rot3k2 root
   `$23EA` — recognised from the ROM by `vm_walk.Space.is_syscall_stub`), with the syscall id pushed
   LAST (= the stub's arg1, param-block byte 0 / `$52`; the parameters follow from `$54`).

## The VM

Register machine, state in the direct page: **regA `$0A`, regC `$0E`, FP `$04`, PC `$06-$08`**
(24-bit: fetched with `[$06],Y`), scratch pointer `$00-$02`; 32-bit **L `$0A-$0D`** (low word
= regA), **R `$0E-$11`** (low word = regC), **W `$12-$15`** (mul32 high half / udiv32
remainder). There is **no independent regB** — `$0C-$0D` is L's high word / scratch. The
operand stack is the 65C816 stack (`PHA/PLA`).

Calls: arguments are pushed right to left, so **arg1 is the LAST-pushed word** — the callee reads
it at FP+`$0F` (the run-VM prologue pulls its return addresses, pushes `$00-$09`, sets FP = SP;
FP+`$0B`/`$0D` hold the VM PC and the return address). `call.p` pops its argument bytes after the
call (cdecl caller cleanup); the result comes back in regA; the callee clobbers regC.

The opcode table (`data/vm_opcode_names.toml`) is the one source for every opcode's name,
control-flow class, operand layout and lift template; `tools/vm_isa.py` is its only reader.
Families:

- `$00-3F` frame matrix (load regA / load regC / store regA / push; slot n = FP-23..-1 locals,
  FP+$0F..+$15 args) · `$40-7F` four nibble-immediate ops (`ldA/ldC/push/addA #n`)
- `$81-88` **frame-relative** word load/store/push (`f8`/`f16`: FP+n+1 if n<0, FP+n+4 if n≥0)
  · `$89-90` immediates · `$A0-A3` frame-relative byte ops · `$A4-AB` **absolute** (bank `$7E`)
  word/byte ops · `$DE/$DF` `leaA/leaC` (frame address, not a load)
- ALU `regA = regA OP regC`: `add sub and or xor`, `mul16`, `sdiv udiv` (quotients), `smod umod`
  (remainders), `shl shr sar` (by regC); unary `inc dec neg not lnot shl1`
- compares → 0/1: `eq ne lt.s le.s gt.s ge.s lt.u le.u gt.u ge.u` (`$C2`-`$C9`: the old names
  were each op's complement)
- `$AE/AF` `sp_adj` · `$E0-E2` bitfield extract/insert (`ldbf.s ldbf.u stbf #i16`)
- control: `jmp` / `jz` / `jnz` **a16** (`$D6-$D8`), `bra` / `bnz` / `bz` **r8** (`$E3-$E5`, second
  copies `$E6-$E8`), `switch` `$D5` (jump table: bias s16, n, default, n targets) and
  `switch2` `$D9` (case list: n, n × {value, target}, default)
- calls: `$AC` `call.n a16` (no argument pop), `$E9` `call.p a16,s8` (call, then pop s8 argument
  bytes — cdecl caller cleanup; also the syscall form), `$DD` `call.ind` / `$EA` `call.ind2`
  (via regA; the latter pops s8) · `$CF` `ret` (SP=FP, pull 10 bytes → `$00-$09`, `JML [$0000]`)
- `$B7` → the extended 32-bit set (`mul32 sdiv32 udiv32 smod32 umod32 add32 sub32 …`, loads /
  stores of L/R by frame offset or absolute address, `ldL/ldR #i32`)

**Operand sizes are derived, never trusted:** `vm_opcodes.py` abstractly executes every
handler along all paths (known-flag branches pruned, fetch-helper callees entered, PC
arithmetic tracked — `$D8`'s `BEQ`/`BNE` pair, ext `$18/$19`'s `ADC #3` with the carry from
`CPY #4`), and `vm_isa.bind()` raises if any derived size or class disagrees with the table, if
the profile's `fetch_helpers` sizes / `invalid_handler` slots disagree with the ROM, if a switch
layout's field order disagrees with how the handler uses each operand word (`$D5`: +0 added to
regA, +2 compared, +4 stored to the PC; `$D9`: +0 the count, each pair's first word compared and
second stored to the PC), if a conditional branch's polarity disagrees with its handler (each path
through the handler's `LDA $0A` / `BEQ`·`BNE` records whether regA was zero; the paths that write the
PC must be the ones the table's taken condition names — `jz`/`bz` on regA == 0, `jnz`/`bnz` on
regA != 0), or if an op's lift template disagrees with the register bytes its handler
reads / writes on any path (the template must write every register the handler writes, read every
register it reads first, and claim no write the handler never does — e.g. `$B1` leaves the popped
pointer in regC, sdiv/smod leave |regC|, the 32-bit shifts count R down). `bind()` needs the profile
explicitly and checks it is this ROM's title.

## Profiles

`tools/games.toml`, one block per title. There is **no default profile**: a tool uses `--game`,
else `$KOEI_SNES_GAME`, else the ONE profile whose `header_title` equals the ROM's internal header
title (announced on stderr); and whichever is chosen must match the ROM (`profile.check_rom`:
header title + `rom_bytes`) — the wrong title fails loudly instead of decoding with another
title's addresses. `gen_layers.py` (it writes a deliverable) requires `--game` / `$KOEI_SNES_GAME`.

## The generated layers (`gen_layers.py` → the title repo's `src/`)

- **`native.asm`** — the bank-`$00` kernel (`gen_native.py`), labelled from `data/native/routines.toml`
  + the profile's VM-spine keys.
- **`<module>.src`** (`gen_source.py`) — every byte of the module, classified by the walker (never a
  linear sweep through data): the `JMP base+3` header; per routine a label, its trampoline
  (`JSR $1C10, dw $FFE8` = the frame-size word) and its reachable bytecode; the module's native
  65C816 code — the stubs bytecode calls and the trampolines' JSR target, decoded by recursive
  descent (both arms of every branch, jumps and JSR/JSL callees inside the module, per-path M/X), so
  a helper's whole body is listed, with its in-module callees labelled `native_XXXX`; everything
  else as `db` rows (NUL-terminated strings as `"text",0`). A bytecode line is
  `$addr  raw bytes  instruction ; annotation`, the instruction with
  EVERY operand value: frame operands `local5 [fp-$0B]` (the variable + its FP offset), immediates
  `#$1C69` / `#-$1`, absolute addresses and jump/branch targets `$20FA` (r8 branches as their
  target), `call.p $1169,6` (target, popped bytes), a switch's header and one row per table entry.
  Annotations: callee names; for a call to the syscall stub the id the operand stack holds there
  on EVERY path into the call (a dataflow over the routine CFG, never the instruction that merely
  precedes the call in address order): `syscall_bridge: sys_name ($id)`, or
  `sys_a ($05) | sys_b ($1E) (id by path)` when a jump into the call brings a different id, or
  `(id = computed value)`; symbols; `"string previews"` for a16 / i16 operands (below); the
  variable of a `$00-$3F` frame-slot op.
  A **string preview** is shown for an address `strings.toml` names (any visible module), else only
  for a NUL-terminated ASCII run in the module's OWN data that starts there (the byte before is NUL
  or not text — or at most 2 printable bytes of preceding binary data, e.g. `1E 32` before `"%s, "`),
  outside decoded code, and not a word table (≥ 6 bytes whose odd bytes are equal). A value that
  lands mid-string (text back to a NUL or a longer text run), in another module or in a switch
  table gets none — name such strings in `strings.toml` to preview them.
- **`<module>.c`** (`vm_lift.py`) — EVERY walked routine, named or not (`sub_XXXX`), lifted over its
  control-flow graph. Conventions:
  - `word name(word arg1, ...)`: argN = the frame word at FP+`$0F`+2(N-1); arg1 = the last-pushed
    argument. Calls print their arguments in that C order. A syscall stub call prints as the named
    syscall with the id removed (`sys_res_open(name)`) when every path pushes the same id (the same
    dataflow as the `.src`); otherwise `syscall_bridge[s6](...)` with `s6` assigned on each edge.
  - `ret` = regA: a call whose result is read on SOME path afterwards is `ret = f(...)`, else
    `f(...)` (liveness over the CFG incl. switch targets). After any call, `ret` is only read once
    assigned again.
  - At a join (a label reached from two or more places, a loop head) values are named: `ret`
    (regA), `regC`, `s0..sN` (operand-stack entries, e.g. an argument pushed before a branch); every
    edge into the join assigns them. `tN` = a copy taken because a later store or call would change
    what the expression reads.
  - Widths and signedness: `memb[]` / `mem32[]` = byte / 32-bit access, `mem[]` = word; `&x` = an
    address (lea); casts on an operation's operands give its signedness — `(u16)` / `(u32)`
    unsigned, `(s32)` signed 32-bit (a 16-bit operation without a cast is signed); `(u32)(u16)x` /
    `(s32)(s16)x` are the zero / sign extensions (`zext32` / `sign32`), so a C reader applying the
    usual conversions reads every operation with the VM's signedness; `$0001E240` (8 digits) = a
    32-bit constant.
  - `(word)(...)` = a 16-bit consumer (a 16-bit op, a 2-byte push / argument, a 16-bit store, a
    `jz`/`bz` test, a switch selector) reading only the low word of a 32-bit value: one written, on
    some path, by an extended op whose handler also wrote the high word (`$0C-$0D` / `$10-$11`, from
    the ROM). The 32-bit compares and `test32` leave a 16-bit 0/1; primary handlers that touch
    `$0C-$0D` (sdiv, smod, the bitfield ops) use it as scratch. A call result's width is not known.
  - String previews (the `.src` rule) are hoisted to ONE trailing comment per statement —
    `f(msg_BE0A, x);  /* msg_BE0A = "%3d0K" */` — so expressions and argument lists never contain
    comment text (a script may split arguments on `,`).
- **`vm_struct.py`** — a goto-less (approximate, follow-node) view of one routine from the same
  blocks; not part of the deliverable.

## Tools (this repo; depend on `snes-decompiler` for the CPU + `games.toml` for addresses)

| tool | does |
|------|------|
| `profile.py` / `minitoml.py` | loads a title profile from `games.toml` (addresses, identity, residency); selection + ROM check |
| `vm_isa.py` | THE loader of the shared opcode table + the shared decoder + operand renderer; `bind(rom, P)` cross-checks the ROM |
| `vm_opcodes.py` | derives every handler's operand size, control-flow class, operand roles, register effects and branch polarity from the ROM (all paths) |
| `archive.py` | the resource-archive directory + code-module detection |
| `vm_walk.py` | module walker: trampoline routines, bounds, conflicts, dead ops, call classes (syscall stubs from the ROM) |
| `vm_callgraph.py` | per-module call graph: leaves, forest, naming frontier |
| `listing.py` | one instruction → listing lines (values + annotations; syscall ids from vm_lift's dataflow; the string-preview rule), shared by gen_source / vm_walk / vm_disasm |
| `vm_disasm.py` | linear bytecode disassembler (named opcodes) |
| `vm_semantics.py` | fingerprint handlers (reg/stack/ALU) for naming |
| `syscall_probe.py` | fingerprint syscall handlers by hardware touched (via `snes_regs`) |
| `native_floor.py` | walk all native engine code in bank `$00`, report coverage |
| `gen_layers.py` → `gen_native.py` / `gen_source.py` / `vm_lift.py` (`vm_struct.py`) | layered source: asm / bytecode / C |

## Porting to a new KOEI SNES title

Add a `games.toml` block: the header title, find the `$8000` ABI (and its syscall slot), the
interpreter loop + its 256-entry table, the `$B7` extended table, the syscall table (after the
22-byte param copy), the fetch helpers, the archive directory, the routine trampoline signature,
and the resident (and co-loaded) modules. Then run `vm_opcodes.py` — it derives every opcode from
the new ROM and fails loudly if any drifted from the shared table (`data/vm_opcode_names.toml`);
only addresses should change.

## Reference — goto-free structuring (DREAM)
`tools/vm_struct.py` does follow-node structuring (~65-75% goto-less; loops remain). The
path to 100% goto-free is DREAM's *reaching-condition* structuring (not node splitting /
tail duplication). Paper + synthesis: vault `research/reverse-engineering/dream-goto-free-
structuring.md`; local PDF `Vault/raw/papers/dream_ndss2015.pdf`. Key: DREAM is the
authors' own successor to REcompile's node-splitting (the $XXXX.0/.1 shared-tail-copy
idea) -- reaching conditions emit a shared tail once, guarded by its condition, no copies.
