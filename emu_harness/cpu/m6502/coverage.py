"""Execution coverage: run a program on the 6502 core and record what it touched (row R).

The "all data by default" inversion: every byte is data until execution touches it. Running from
reset with a periodic interrupt (an NES NMI each frame, say) gives the code bytes the run reached,
the direct JSR/JMP targets, and each JMP (ind) site's resolved targets: the jump tables a static
sweep can't follow. Lifted from Mappy's trace_codegraph (game-annotation).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .descent import Window
from .disasm import MODE_LEN, OPS


@dataclass
class Coverage:
    code: bytearray                                   # 1 per window byte executed (opcode or operand)
    edges: dict = field(default_factory=dict)         # site offset -> {JSR / JMP abs targets}
    indirect: dict = field(default_factory=dict)      # JMP (ind) site offset -> {resolved targets}
    steps: int = 0
    ticks: int = 0
    halt: str | None = None


def trace_coverage(cpu, win: Window, *, ticks: int, tick_every: int, max_steps: int,
                   tick=None) -> Coverage:
    """Step `cpu` until `ticks` periodic interrupts have fired (one per `tick_every` steps; `tick`
    defaults to cpu.nmi) or `max_steps`. Window offsets key the results, so mirrors fold together.
    JMP (ind) resolves with the 6502 page-wrap quirk."""
    mem = cpu.mem
    tick = tick or cpu.nmi
    cov = Coverage(bytearray(len(win.buf)))
    since = 0
    while cov.ticks < ticks and cov.steps < max_steps:
        pc = cpu.pc
        op = mem.read(pc)
        ilen = 1 + MODE_LEN.get(OPS[op][1], 0) if op in OPS else 1
        if pc in win:
            n = win.offset(pc)
            for k in range(ilen):
                cov.code[(n + k) % len(cov.code)] = 1      # wraps inside the window, as the PRG mirrors
            if op in (0x20, 0x4C):
                cov.edges.setdefault(n, set()).add(mem.read_word(pc + 1) & 0xFFFF)
            elif op == 0x6C:
                ptr = mem.read_word(pc + 1)
                tgt = mem.read(ptr) | (mem.read((ptr & 0xFF00) | ((ptr + 1) & 0xFF)) << 8)
                cov.indirect.setdefault(n, set()).add(tgt & 0xFFFF)
        try:
            cpu.step()
        except NotImplementedError as e:
            cov.halt = f"[missing opcode] {e}  (op ${op:02X} at ${pc:04X})"
            break
        except Exception as e:
            cov.halt = f"[halt] {type(e).__name__}: {e} at ${pc:04X}"
            break
        cov.steps += 1
        since += 1
        if since >= tick_every:
            tick()
            since = 0
            cov.ticks += 1
    return cov
