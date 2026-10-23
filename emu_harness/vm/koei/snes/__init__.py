"""KOEI's bytecode VM on the SNES: the 65C816-hosted engine (resource-archive modules, JSL syscall ABI),
the walker, lifter and layered-source generator. Moved from koei-snes `tools/` (MIGRATION.md row F, SNES
half); the opcode table it binds against each ROM is `../vm_opcode_names.toml`, shared with the NES side."""
