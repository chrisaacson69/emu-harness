# koei-nes — shared reverse-engineering library for KOEI's NES engine family

> **Moved from koei-nes `docs/` on 2026-10-26** (koei-nes is being retired; its code moved to
> emu-harness in MIGRATION.md rows D-F, K, M, O). Kept verbatim. Tool paths below predate the move:
> `tools/koei_vm.py` = `emu_harness.vm.koei.nes.titles`, `tools/games.toml` =
> `emu_harness/vm/koei/nes/games.toml`, `tools/dump_data.py` = `emu_harness.vm.koei.nes.romdata`,
> `native_floor` / `bc_match` / `native_match` = `emu_harness.vm.koei.nes.native_floor` / `.match`,
> and the VM core is no longer imported from na1-decompiler.

> The engine KOEI reused across its NES strategy titles (Nobunaga's Ambition, Romance of
> the Three Kingdoms, …): MMC1 banking, CHR-RAM rendering, a battery-SRAM save layer, a
> BRK-syscall BIOS, and a bytecode VM in the fixed bank. This repo holds the **tooling
> that is shared across all of those games**, so each game repo stays thin.

## Why this exists (the architecture)

Three tiers, deliberately separated:

| Tier | Repo(s) | Holds |
|---|---|---|
| **Thesis** | the vault (`game-annotation`) | the research, cross-game findings, the LLM-grounding story |
| **Engine tooling** | **this repo** (`koei-nes`) | game-agnostic RE tools + the VM toolchain |
| **Game content** | `na1-decompiler`, `ro3k-decompiler`, … | per-game bytecode→C, wiki, and the small **patch constants** |

Games are cloned as **siblings** of this repo and resolve it by the logical name
`koei-nes` (see the vault's `.claude/local-paths.md`). A game does **not** fork the engine —
it supplies a thin profile of **version constants** (VM entry address, opcode-table address,
zero-page base, syscall-table format) and reuses the shared algorithms unchanged.

## Why we believe the share is real (measured, not assumed)

Established walking ROTK against NA1 (2026-06-22):

- **Firmware/BIOS**: byte-identical boot prologue; BIOS syscalls mostly shared (relocated +
  a one-byte zero-page insertion between versions).
- **Bytecode VM**: **same machine** — identical VM zero-page registers, identical 9-byte call
  frame, **217 of 219 opcodes byte-identical** in the same slots.
- **The whole VM decompiler transferred with ONE changed constant** (the sub-tiling marker
  `JSR <vm_entry>`), tiling every ROTK bytecode bank exactly (0 errors, 465 subs). That is the
  proof the engine — and its toolchain — is a genuinely shared component.

## Contents

> **2026-10-23: the engine moved on to emu-harness** (its MIGRATION.md rows B, C, F). The code lives
> in `emu_harness.vm.koei` (the VM toolchain + `games.toml`), `emu_harness.cpu.m6502` (the 6502
> disassembler, descent and CPU) and `emu_harness.machines.nes` (cart, bank windows). The names
> below still work as aliases: `tools/koeivm` is `emu_harness.vm.koei`, `tools/koei_vm.py` is
> `emu_harness.vm.koei.titles`, `tools/disasm6502.py` re-exports the disassembler.

- `tools/disasm6502.py` — linear-sweep 6502 disassembler (canonical-ISA grounded; self-validates
  by reproducing a known-good bank disassembly). Game-agnostic. (Alias; see above.)
- `tools/koeivm/` — **the VM/decompiler engine (the crown jewel)**: 6502 CPU + MMC1/MMC5 banking +
  hardware multiplier, the bytecode VM (disasm + decompile + the DREAM structurer), per-title
  retarget via `games.toml`. Graduated here from `na1-decompiler`'s `na1dream` (2026-06-29), then
  on to emu-harness (2026-10-23); both old names are aliases. Driven by `tools/koei_vm.py`.

## Roadmap (graduate on proven reuse, not speculation)

- ✅ **VM toolchain** — DONE: graduated into `tools/koeivm/` as the canonical home (4 titles verified
  identical: na1 b0=98, ro3k b2=58, gk b2=154, na2 b30=71). koei-nes is now self-contained; game
  repos are thin consumers.
- **`handler_match`** (cross-title relocation-invariant handler matcher) and **`subs`** (native
  subroutine finder) — generalize from hardcoded game paths to a game profile, then graduate.
- **CHR-RAM render / SRAM decode / Mesen integration** — graduate from `na1-decompiler/tools` as
  each is exercised by a second game.

## Per-game profile (the patch surface)

The set of version constants a game supplies is small and stabilizing. Observed so far:
`vm_entry` address, `vm_dispatch` address, opcode-table lo/hi addresses, syscall-table address +
encoding (`jmp`-table vs raw-address, stride), zero-page base offset. The next title added
decides whether this stays pure-config or needs a code-override hook.
