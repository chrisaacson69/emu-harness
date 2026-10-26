# The KOEI shared function library (cross-title)

> **Moved from koei-nes `docs/` on 2026-10-26** (koei-nes is being retired; its code moved to
> emu-harness in MIGRATION.md rows D-F, K, M, O). Kept verbatim. Tool paths below predate the move:
> `tools/koei_vm.py` = `emu_harness.vm.koei.nes.titles`, `tools/games.toml` =
> `emu_harness/vm/koei/nes/games.toml`, `tools/dump_data.py` = `emu_harness.vm.koei.nes.romdata`,
> `native_floor` / `bc_match` / `native_match` = `emu_harness.vm.koei.nes.native_floor` / `.match`,
> and the VM core is no longer imported from na1-decompiler.

> Measured finding (NA1 ↔ ROTK, 2026-06-22): KOEI maintained a **shared standard library** of
> low-level functions reused **byte-identical-modulo-relocation** across titles, and reinvented
> the game-logic layer each game. The bytecode VM, the BIOS, and these library functions are the
> engine; officers-vs-daimyo gameplay is bespoke per title.

## How it was proven (and how to apply it to the next game)

`bc_match.py` (a cross-title bytecode differ; currently in `ro3k-decompiler/tools/`, graduation
candidate) signature-matches VM bytecode subs between two ROMs by **opcode-mnemonic sequence**
(operand-sensitive only for immediates + RAM; ROM addresses normalized, so relocation doesn't
matter). A shared-library function scores ~1.0 regardless of where it or its callees live; a
game-specific function has no good match.

**Workflow for a new KOEI title:** decompile its bytecode (`vm_disasm_rotk`/`vm_decompile_rotk`
retargeted by the new game's `vm_entry` stub), then `bc_match new_bank_vm.asm na1_bank_vm.asm`.
The high-confidence matches are the shared library → **sync the new game's names to the canonical
names below**; the `<.70` residue is that game's own logic to walk fresh. This turns each prior
title's analysis into the next title's labels for free.

## Canonical shared-library names (NA1 = the established vocabulary)

Verified shared across NA1 + ROTK (1.00 / long matches). Use these names in every title:

| Canonical | What it is | NA1 | ROTK (orig name before sync) |
|---|---|---|---|
| `strlen` | string length | ✓ | was `str_len` |
| `atoi_decimal` | ASCII decimal → int | ✓ | was `parse_decimal_atoi` |
| `count_decimal_digits` | base-10 digit count | ✓ | was `decimal_digit_count` |
| `fill_bytes` | `memset` | ✓ | was `mem_fill_byte` |
| `min_word` / `max_word` | signed min/max | ✓ | was `min_signed`/`max_signed` |
| `format_string` | `printf`/`sprintf` (`%w.p`, C/D/S) | ✓ | was `format_string_into_buffer_and_print` |
| `char_classify` | ASCII → tile/charset index | ✓ | was `ascii_to_tile_index` |
| `math32_3arg` | 32-bit `a*b/c` ext-op helper | ✓ | was `mul32_field_helper` |
| `rng_mod` | random in `[0,n)` | ✓ | was `rng_below` |
| `delay_loop` | busy-wait frame delay | ✓ | was `busy_wait_delay` |
| `wait_button_edge` | block until keypress | ✓ | was `wait_keyup_then_block_for_keypress` |

Ext-op service wrappers (the BIOS-call layer), resolved by the same match:
`ppu_upload_block_wrap`, `palette_write_wrap`, `palette_swap_wrap`, `call_bank_wrap`,
`syscall16_sram_wrap`, `rng_next_wrapper`, `ppu_fill_rect_wrap`, `ppu_copy_rect_wrap`.

**Shared-but-evolved** (0.7–0.9 — same skeleton, per-title tweaks; keep the descriptive name + a
cross-ref, don't blind-rename): `printf` itself (0.84 over 175 opcodes), the multi-digit number
spinner (`cursor_nav_loop`, 0.75/236), `char_advance_width`, the `toupper`/`islower` char-class pair.

## The native 6502 layer is shared *even more* (`native_match.py`)

The same comparison on bank-15 **native** code (the 6502 sibling of `bc_match`, reusing
`handler_match`'s relocation-invariant signature) found **38 of 54 native subs shared (≥.90)** —
an even higher rate than the bytecode library. The **VM interpreter + the 32-bit ALU are
byte-identical** (`udiv16`/`udiv32`/`sdiv*`, `cmp32_*`, `vm_bitfield_read/write`, `make_bitmask`,
`vm_fetch_*`, `vm_pop_operand16`, `vm_call_native`), so a new title's whole bignum library + VM
engine names itself from NA1. Most PPU/kernel helpers match too (`set_prg_bank`, `controller_poll`,
the `ppu_rect_attr_*` family, `mul_xy`, nmi on/off).

**The divergence is localized — and it *is* the maturation story:** ROTK's timed **scroll engine**
has no NA1 counterpart (<.40), `wait_vblank` diverges because ROTK pumps banked sound from it (.72,
NA1's is native), boot differs (.54), and the `$0080` ZP-shift drops the affected handlers a notch.
So "less agreement" turned out to mean: the *core* is shared, and disagreement is concentrated
exactly in the subsystems we already knew evolved.

## The headline

Per game so far: **native engine + BIOS + VM + ALU + the bytecode library = shared** (a stable
platform); **gameplay + a few matured subsystems (scroll, banked sound) = reinvented**. Doing
titles chronologically, each new game's diff against the platform is small and legible — and
`bc_match` + `native_match` make the shared half (bytecode *and* native) name itself.

## The animation/cutscene engine is shared too (relocated, lightly evolved)

`bc_match` of ROTK bank-10 vs NA1 bank-14 (both the cutscene VM): **`draw_sprite_grid` ($801D) = 1.00**
and **`cutscene_delay` ($8003) = 1.00** (byte-identical primitives), the **scene driver ($80AD) = 0.75**
(same skeleton, ROTK evolved the script interpreter). So the animation engine is the *same engine*,
moved bank 14->10, with only its driver tweaked -- the draw primitives are verbatim. Per-title retarget:
NA1 cutscene-id $7FCB + table $AF80; ROTK cutscene_scene_id $7FB7 + cutscene_record_table $8574 (stride 5).
Another instance of "shared core, evolved subsystem" -- the cutscene system joins the VM/BIOS/ALU/library
as inherited platform, with the bespoke layer being *which scenes are scripted*, not *how they draw*.
