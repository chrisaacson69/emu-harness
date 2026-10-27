"""The APU synthesizer against the nesdev formulas: pitch, length counter, envelope, muting."""
import numpy as np
import pytest

np = pytest.importorskip("numpy")
from emu_harness.machines.nes.apu import APU, CPU_HZ, RATE   # noqa: E402

INIT = [(0, 0x4015, 0x0F), (0, 0x4017, 0xC0)]       # 4 channels on, 5-step frame counter (Mappy's init)


def peak_hz(x):
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / RATE)[np.argmax(spec)]


def test_pulse_and_triangle_pitch():
    t = 253                                            # pulse: CPU / (16 * 254) = 440.4 Hz
    pulse = APU().render(INIT + [(10, 0x4000, 0xBF), (11, 0x4002, t & 0xFF), (12, 0x4003, 0x08 | t >> 8)], 0.5)
    assert abs(peak_hz(pulse) - CPU_HZ / (16 * (t + 1))) < 3
    tri = APU().render(INIT + [(10, 0x4008, 0xFF), (11, 0x400A, t & 0xFF), (12, 0x400B, 0x08 | t >> 8)], 0.5)
    assert abs(peak_hz(tri) - CPU_HZ / (32 * (t + 1))) < 3   # an octave below the pulse


def test_length_counter_silences_after_its_half_frames():
    # length index 0 -> 10 half-frame clocks; 5-step mode clocks half frames twice per 37282 CPU cycles
    x = APU().render(INIT + [(10, 0x4000, 0x9F), (11, 0x4002, 0xFD), (12, 0x4003, 0x00)], 0.3)
    loud = np.flatnonzero(np.abs(np.diff(x)) > 1e-3)
    expect = 10 * 37282 / 2 / CPU_HZ                   # ~0.104 s
    assert abs(loud[-1] / RATE - expect) < 0.02


def test_envelope_decays_and_short_timers_mute():
    # decaying envelope, no loop ($83: duty 2, bit 5 clear), V = 3: the level falls every 4 quarter frames, so 15 levels take ~60 of them
    x = APU().render(INIT + [(10, 0x4000, 0x83), (11, 0x4002, 0xFD), (12, 0x4003, 0x08)], 0.5)
    loud = np.flatnonzero(np.abs(np.diff(x)) > 1e-3)
    expect = 15 * 4 * 37282 / 4 / CPU_HZ               # quarter frames come 4 per 37282 cycles
    assert abs(loud[-1] / RATE - expect) < 0.03
    quiet = APU().render(INIT + [(10, 0x4000, 0xBF), (11, 0x4002, 0x05), (12, 0x4003, 0x08)], 0.1)
    assert np.ptp(quiet) == 0                          # t < 8 mutes the pulse
