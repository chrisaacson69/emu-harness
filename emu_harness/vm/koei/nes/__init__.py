"""KOEI's bytecode VM on the NES: the 6502-hosted engine and its toolchain -- the banked bus (cpu6502.Memory),
the VM runner that steps the ROM's own interpreter (nobunaga_vm), the bytecode disassembler, the decompiler
with its CFG / V2 reducer / DREAM structurer and stack model, the gates, and the title profiles (titles,
games.toml). The SNES host is ../snes; the instruction set both share is ../isa.py + ../vm_opcode_names.toml.
Unifying the two lifters / structurers is MIGRATION.md row G."""
