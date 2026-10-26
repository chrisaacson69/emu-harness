# koei-snes

> **Moved from koei-snes on 2026-10-26** (koei-snes is being retired; its toolchain moved to
> emu-harness in MIGRATION.md row F, SNES half). Kept verbatim. Paths below predate the move:
> `tools/<x>.py` = `emu_harness.vm.koei.snes.<x>`, `tools/games.toml` =
> `emu_harness/vm/koei/snes/games.toml`, `data/vm_opcode_names.toml` =
> `emu_harness/vm/koei/vm_opcode_names.toml`, `tools/gen_mlb.py` = emu-harness `tools/gen_mlb_koei_snes.py`,
> `snes-decompiler` = `emu_harness.cpu.w65c816` + `emu_harness.machines.snes`.

The shared **KOEI SNES engine** library — the reversed bytecode VM + native kernel
behind KOEI's SNES strategy titles, and the toolchain to decode any of them. The SNES
sibling of [`koei-nes`](https://github.com/chrisaacson69/koei-nes).

> **2026-10-23: the toolchain moved to emu-harness** (its MIGRATION.md row F, SNES half). The code
> lives in `emu_harness.vm.koei.snes`; the opcode table is `emu_harness/vm/koei/vm_opcode_names.toml`
> (shared with the NES side); the profiles are `emu_harness/vm/koei/snes/games.toml`; the substrate is
> `emu_harness.cpu.w65c816` + `emu_harness.machines.snes`. Every `tools/*.py` here is an alias that runs
> or imports the moved module, so the commands below work unchanged (`gen_mlb.py` runs emu-harness
> `tools/gen_mlb_koei_snes.py`). Paths below that say `tools/games.toml` / `data/vm_opcode_names.toml`
> mean those new locations.

The engine (a register-machine bytecode VM over a native 65C816 kernel, with a
resource-archive loader and a 22-byte-param-block syscall ABI) is shared across titles;
only addresses move. Per-title addresses live in `tools/games.toml`; the tools take
`--game <name>` (else `$KOEI_SNES_GAME`, else the profile whose header title matches the ROM --
there is no default, and the chosen profile is always checked against the ROM).

- **Architecture:** [`docs/engine.md`](docs/engine.md)
- **NES vs SNES comparison:** [`docs/koei-nes-vs-snes.md`](docs/koei-nes-vs-snes.md)
- **Opcode reference:** [`data/vm_opcode_names.toml`](data/vm_opcode_names.toml) — the one source of
  opcode names, control-flow classes, operand layouts and lift templates (217 live primary incl.
  the `$B7` prefix + 47 extended), read via `tools/vm_isa.py` and cross-checked against each ROM

## Dependencies
- **`snes-decompiler`** (sibling `../snes-decompiler`) — the CPU/hardware substrate
  (65C816 disassembler, LoROM/HiROM mapper, header parser, SNES register map).

## Tools (`tools/`, `py -3`)
`profile.py` (games.toml loader + ROM check) · `vm_isa.py` (the opcode-table loader + shared
decoder + operand renderer) · `vm_opcodes.py` (all-paths handler analysis → exact operand sizes,
control-flow classes, switch-layout roles and branch polarity, cross-checked) · `archive.py` (resource-archive
directory) · `listing.py` (shared listing lines) · `vm_disasm.py` (bytecode disassembler) ·
`vm_walk.py` (module walker: conflicts, dead ops, call classes) · `vm_callgraph.py` ·
`vm_semantics.py` (handler fingerprints) · `syscall_probe.py` (syscall→hardware fingerprints) ·
`native_floor.py` (native coverage / completeness) · `gen_layers.py` (layered source: asm /
bytecode `.src` of every byte / C lift of every routine — conventions in `docs/engine.md`).

```
py -3 tools/vm_opcodes.py  <rom> --game rot3k2          # ROM handlers vs the shared table
py -3 tools/vm_walk.py     <rom> --game rot3k2 --all    # every bytecode module
py -3 tools/vm_disasm.py   <rom> --start 01:8008 --base 7E:2008 --game rot3k2
py -3 tools/native_floor.py <rom> --game rot3k2 --gaps
py -3 tools/gen_layers.py  <rom> <title-repo> --game aerobiz   # src/: native.asm, <mod>.src, <mod>.c
```

## Titles
- **rot3k2** — Romance of the Three Kingdoms II (USA). First target; profile in
  `games.toml`. Title repo: `rot3k2-snes-decompiler` (ROM, syscall names, record schemas).
- **gemfire** — Gemfire (USA). Title repo: `Gemfire-snes-decompiler`.
- **aerobiz** — Aerobiz (USA). Title repo: `AeroBiz-decompiler`.

*ROMs are never committed.*
