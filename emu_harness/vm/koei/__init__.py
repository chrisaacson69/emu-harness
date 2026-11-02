"""KOEI's bytecode VM: the register-machine bytecode KOEI's NES strategy titles run (MIGRATION.md row F).

Moved from koei-nes `tools/koeivm/` (born in na1-decompiler as `na1dream`, "the NA1 Dream Decompiler
core"); both old names remain as aliases of this package. The engine: the bytecode disassembler, CFG,
front-end and structurer (DREAM, with the region reducer as its fallback), the audited stack model, the VM runner that
steps the ROM's own interpreter on the 6502 core, and the title profiles (`titles`, games.toml).
CLIs live in `.cli`, gates in `.gates`. See ARCHITECTURE.md for the layout.
"""
