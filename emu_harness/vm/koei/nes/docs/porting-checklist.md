# Porting a new KOEI NES title — checklist + gotchas

> **Moved from koei-nes `docs/` on 2026-10-26** (koei-nes is being retired; its code moved to
> emu-harness in MIGRATION.md rows D-F, K, M, O). Kept verbatim. Tool paths below predate the move:
> `tools/koei_vm.py` = `emu_harness.vm.koei.nes.titles`, `tools/games.toml` =
> `emu_harness/vm/koei/nes/games.toml`, `tools/dump_data.py` = `emu_harness.vm.koei.nes.romdata`,
> `native_floor` / `bc_match` / `native_match` = `emu_harness.vm.koei.nes.native_floor` / `.match`,
> and the VM core is no longer imported from na1-decompiler.

> The accumulated "engine reminders." Each KOEI title is a thin patch on the shared engine,
> but the bring-up has a fixed set of steps and a few traps that cost real time when forgotten.
> Read this before standing up a new title's repo. (Companion: [shared-library.md](./shared-library.md).)

## Bring-up steps

1. **Find the fixed bank's entry vectors.** MMC1 last bank = `$C000-$FFFF`. RESET/NMI/IRQ at the
   end (`$FFFA`). RESET is `$C000` in every title seen; NMI/IRQ drift a few bytes.
2. **Locate `vm_entry`.** A bytecode bank's `$8003` does `JSR <vm_entry>`. That address is the
   ONE constant you retarget in the decompiler (`VM_ENTRY_STUB`). Everything else transfers.
3. **Validate the disassembler** against a known-good bank before trusting it (reproduce a prior
   title's committed asm, or the new title's own bank 15 cross-checked against the ROM).
4. **Auto-name the shared half.** Run `bc_match` (bytecode) and `native_match` (6502) vs a prior
   title → the shared library + VM + ALU + BIOS name themselves. Sync to the canonical names.
5. **Walk only the residue** — the game-specific subs (`<.70` matches) and the matured subsystems.
   Leaves-first, per the bank dependency order (derive it; don't assume bank 0 is the hub).

## GOTCHAS (the ones that have actually bitten)

### ⚠ Bank-scope EVERY label/enum lookup — flat loads leak across banks AND games
`$8000-$BFFF` is the switchable window: **the same CPU address means different code in different
banks** (bank0 `$8008` ≠ bank2 `$8008`), and different code in different *titles*. NA1's own toml
has three `$8003` entries. The label key must be the **18-bit PRG offset** (`bank*0x4000 +
cpu&0x3FFF`) via `[prg.bankN]` sections + `load_labels_by_prg`. `$C000-$FFFF` (fixed bank) is
address-authoritative and safe to key flat.
- **The trap (2026-06-22):** `vm_disasm` annotates call targets with names baked into the asm, and
  `vm_decompile` *trusts those annotations* when an address isn't in the passed label set. If the
  disassembler is fed a FLAT label set (e.g. `NobunagaVM.labels` = NA1's `load_labels()`), it bakes
  the wrong-bank/wrong-game name into the asm — ROTK bank-3 `$8003` rendered as NA1's `cutscene_delay`.
  **Rule: feed the disassembler the SAME bank-scoped labels as the decompiler.** Un-walked targets
  must fall back to `sub_XXXX`, never to a borrowed name.

### Enums are per-title — they don't leak, but they don't transfer either
Asset-value / cutscene-id enums (`asset-enums.toml`) make literal args readable
(`trigger_cutscene(26)` → `trigger_cutscene(CUTSCENE_BRIBE)`). They are **keyed by function name**,
so a prior title's table neither leaks INTO a new title (the names don't match — verified: ROTK's C
shows zero NA1 enum annotations) nor helps it. Consequence: a new title's syscall/cutscene args show
as **raw ints** until you build its OWN `asset-enums` (after you've named its cutscene/asset functions).
Per-title, like everything else. (The genuine flat-leak danger is **address-keyed** label/enum data —
see the bank-scoping gotcha above; function-name-keyed tables are self-protecting.)

### The decompiler's ext-op fold gap
The `$B7` ext-op handlers (32-bit math: `(a*b+9)/10`, etc.) live at **different addresses per title**,
so the certified `HOST_CALL_FOLDS` don't fire — the C silently drops the math (shows `*p = *p`).
**When the C looks too simple for the bytecode, trust the bytecode** and flag `[LOW]`. A per-title
ext-op fold table is the fix.

## Key engine mechanisms (so you recognize them)

- **BIOS = BRK syscalls.** A BRK indexes a per-title dispatch table → a handler in the fixed bank.
  ROTK uses a raw 2-byte address table (`n*2`); NA1 a `JMP` table (`n*3`). Same idea, different stride.
- **Ext-op services.** `sub_<extop_dispatch>(N, args)` wrappers ("os_service_N") route through the
  `$B7` prefix / a BRK to a numbered service. Many are thin wrappers over BIOS calls.
- **`call_in_vm_context(bank, fn_ptr, args)` = paged-data access.** It's literally
  `save_bank; set_prg_bank(bank); (*fn_ptr)(args); restore_bank`. `os_set/get_vm_context` ARE
  `set_prg_bank`/`get_current_prg_bank`. So cross-bank "calls" are mostly *data reads* via a paged
  bank + a fixed-bank reader function — not game-bank→game-bank coupling.
- **Sound** is a native, self-contained driver in its own bank, run every frame from NMI (ROTK: bank
  `$0D`). It has **0 bytecode stubs**, so the bytecode walk never sees it — walk it as native 6502.
- **Per-bank coverage is two passes:** bytecode subs *and* the native code + data tables that share
  the bank. "Bank N done" via the stub-scan means bytecode-done, not bank-done.

## The toolchain (graduated — config-driven, no per-game fork)

All title-specific values live in `tools/games.toml` (one block per title: ROM, `vm_entry`/stub,
`dispatcher`, handler tables, syscall mechanism, code banks). The shared drivers read it:

- **`tools/koei_vm.py disasm <game> <bank> [out]`** — render a bank's bytecode VM-asm (generalizes the
  old per-repo `vm_disasm_<game>.py`). Bank-scoped label loading is built in.
- **`tools/koei_vm.py emulate <game> <0xsub>`** — retarget the NA1 VM emulator to any title (just
  `DISPATCHER_ADDR` + ROM). Proven: ROTK's rice-harvest formula was byte-confirmed this way.
- **`tools/dump_data.py <game> <bank> <addr> --str|--bytes|--ptrs`** — the ASCII string / table reader.

Verified: the SAME `koei_vm.py` disassembles `na1` *and* `ro3k` and retargets the emulator for each.
**To add a title:** clone its decompiler repo as a sibling, find `vm_entry` (the JSR target at a
bytecode bank's `$8003`) and `dispatcher` (where opcode handlers jmp back — `grep 'jmp $<addr>'` the
fixed bank, the most common target), add a `games.toml` block, done — the whole toolchain works.

*Interim coupling:* the VM interpreter **core** (`na1dream.vm_disasm` / `nobunaga_vm`) is still imported
from `na1-decompiler` via the sibling convention; relocating that core into `koei-nes/` (and pointing NA1
at it too) is the one remaining graduation step. The per-repo `vm_disasm_rotk.py` / `emulator_rotk.py` are
now **superseded** by `koei_vm.py` (kept only as the working regen scripts until ro3k's pipeline is repointed).
