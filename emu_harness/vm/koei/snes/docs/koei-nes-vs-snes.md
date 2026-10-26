# KOEI's engine: NES vs SNES

> **Moved from koei-snes on 2026-10-26** (koei-snes is being retired; its toolchain moved to
> emu-harness in MIGRATION.md row F, SNES half). Kept verbatim. Paths below predate the move:
> `tools/<x>.py` = `emu_harness.vm.koei.snes.<x>`, `tools/games.toml` =
> `emu_harness/vm/koei/snes/games.toml`, `data/vm_opcode_names.toml` =
> `emu_harness/vm/koei/vm_opcode_names.toml`, `tools/gen_mlb.py` = emu-harness `tools/gen_mlb_koei_snes.py`,
> `snes-decompiler` = `emu_harness.cpu.w65c816` + `emu_harness.machines.snes`.

The same engine *design* on two machines — decoded independently on each side
(`rot3k2-decompiler` for NES, this line for SNES), then compared. The through-line is a
register-machine bytecode VM with a native kernel and a param-block syscall ABI; the
mechanisms were rebuilt for each machine's hardware.

| aspect | NES (`koei-nes`) | SNES (`koei-snes`) |
|--------|------------------|--------------------|
| CPU | 6502 (8-bit) | 65C816 (16-bit, 24-bit addr) |
| Program delivery | bytecode in **MMC5-switched** banks | **resource archive → WRAM relocation** (no banking) |
| Native kernel | fixed bank `$E000-$FFFF` | fixed bank `$00` (`$800C-$B20A`) |
| Syscall dispatch | **fake-BRK** (`PHP+JMP ($FFFE)`) | **`JSL` into the `$00:8000` ABI table** |
| Syscall param passing | **22-byte param block**, return via pointer | **22-byte param block**, work buffer `$52` |
| Syscall count | ~23-31 | **69** (richer hardware surface) |
| VM model | register machine, DP registers | register machine, DP registers |
| Opcode count | 217-219 | **217** live (256 − 39 dead: 38 at the invalid handler + `$FF`) |
| Frame-local ops | `$00-3F` matrix | `$00-3F` matrix |
| Extended prefix | `$B7` → 32-bit-math subtable | `$B7` → 32-bit-math subtable |
| Call + arg-pop opcode | `$E9` (CALL_abs_imm1) | `$E9` `call.p` (host-call = its syscall use) |
| Graphics API | PPU writes | PPU + **DMA**, Mode 7, byte-plane VRAM |
| Audio | on-CPU APU | mailboxed **SPC700** (queue → `$2140`) |

## What ported (design)
- The **VM instruction set**: frame matrix, stack, ALU, compares-as-booleans, the `$B7`
  extended 32-bit subtable, the `$E9` call-and-pop. Opcode *numbers* line up, not just shapes.
- The **syscall convention**: a 22-byte parameter block selecting a handler from a table.
- The **program/kernel split**: bytecode modules over a native kernel + a stable ABI.

## What was rebuilt (mechanism, forced by hardware)
- **Program delivery**: MMC5 bank-switching (NES has a 16-bit address bus) → a resource
  archive + WRAM relocation (the 65C816 addresses 16 MB directly; WRAM is big enough to
  hold and run a module).
- **Syscall entry**: fake-BRK → `JSL` (the 65C816 has real long call/return).
- **The hardware syscalls themselves**: entirely different silicon (SNES PPU/DMA/Mode 7/
  SPC700 vs NES PPU/APU), so the ~69 SNES syscalls are largely new services.

## Method note
Decoded each SNES handler as native code and **compared** to the NES — never derived
from it. The engine drifts slightly between titles/platforms, so the exact table is
always read from the target ROM; the NES is the cross-check that catches drift, not the
source of truth. The 218-vs-217/219 and per-opcode matches are *confirmations*.

## Game mechanics — did they change? (NES ROTK2 vs SNES port, 2026-07-08)

Cross-checked the SNES reconstruction against the NES `rot3k2-decompiler` + `koei-nes`.
Verdict: **faithful engine + architecture port with a graphical facelift; NOT a mechanics
rewrite.** Three layers:

**1. VM opcodes — byte-for-byte the same.** Every control/call op sits at the identical
opcode byte on both machines: `$E9` call.p (call + arg pop; NES CALL_abs_imm1), `$AC` call.n,
`$DD`/`$EA` call.ind, `$CF` ret, `$D5`/`$D9` switch, `$D6` jmp, `$D7`/`$D8` jnz/jz a16 (NES
JUMPT/JUMPF_abs), `$B7` 32-bit prefix, `$00-3F` frame matrix. Counts: NES **219** vs SNES
**217** live (256 minus 39 dead slots). Same instruction set, same numbers -- a bytecode
routine uses the same opcodes on both. (Names corrected 2026-09-30; see the CHANGELOG in
`data/vm_opcode_names.toml` -- the NES table's names had them right all along.)

**2. Architecture — the same shape.** NES: **bank 1 = resident `$8000` strategic library**
(`run_turn_loop`, `dispatch_command`, AI scorers, diplomacy) + command apps in banks 2-7,
21 dispatched by category (war=2, domestic=5, diplo=6, invest=7), and **bank 8 = battle
library** + apps 9-12. SNES: **root = resident shared library** + **overlay command apps**
(comroot=AI, excsub1-6 = diplomacy/officer/combat events) dispatched via `call.ind`. The
NES "bank-per-command-app" simply became "overlay-per-command-app" -- a *mechanism* swap
forced by MMC5-banking vs WRAM-overlays, over an *identical* strategic-library + dispatch
+ turn-loop design.

**3. Data model — mostly identical, one real change.**
- **Province record: stride 25 on BOTH** (NES base `$73F4`, SNES `$F424`) -- identical
  layout; count 41 (ROTK2 standard). (Corrects an earlier note that guessed NES = 49x24;
  that was NA1's fief schema, not ROTK2.)
- **Officer record GREW: NES stride 21 -> SNES stride 34.** This is the one substantive
  divergence -- the SNES officer record is 13 bytes larger. (Note: 34 == the NES *ruler*
  stride, so the SNES may have unified officer/ruler to the larger record, or added
  officer fields; a field-level diff would confirm which.)

**Where the SNES effort went:** the *facelift* -- **69 SNES syscalls vs ~31 NES** (DMA,
Mode 7, byte-plane VRAM, SPC700 audio) and the UI. Presentation was rebuilt; the strategy
engine, turn loop, command dispatch, AI evaluation, and combat structure were ported. So:
**faithful port + facelift, plus an expanded officer record.**
