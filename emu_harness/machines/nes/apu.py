"""The NES APU as a synthesizer: a timestamped stream of register writes in, audio out.

Not a cycle-exact core: it plays what a sound engine writes (captured headless or from an emulator's
write-watch), so a title's sounds can be heard, named and exported. Pulse 1/2, triangle and noise are
modelled; the DMC is not (its registers are accepted and ignored, which is exact for a title that never
writes $4010-$4013, e.g. Mappy).

Every table and rule is from the nesdev wiki (APU Length Counter, Pulse, Sweep, Envelope, Triangle,
Noise, Frame Counter, Mixer; Cycle reference chart), read 2026-10-27:
  * length table (32 entries); noise periods (NTSC, CPU cycles); duty sequences; the 32-step triangle
  * pulse timers clock every APU cycle (2 CPU cycles), so a duty step lasts 2*(t+1) CPU cycles; the
    triangle timer clocks every CPU cycle; pulse muted when t < 8 or the sweep target > $7FF
  * frame counter (NTSC, APU cycles): 4-step quarter 3728 / 7456h / 11185 / 14914h, length 14915;
    5-step 3728 / 7456h / 11185 / (14914 none) / 18640h, length 18641; writing $4017 with bit 7 set
    clocks quarter and half at once
  * mixer: pulse_out = 95.88 / (8128 / (p1 + p2) + 100); tnd_out = 159.79 / (1 / (t/8227 + n/12241) + 100)
  * CPU clock 1,789,773 Hz; 29,780.5 CPU cycles per frame
Between two events (a register write or a frame-counter clock) every channel's parameters are fixed,
so each span is rendered vectorized, oversampled 4x and averaged down (a box filter against aliasing).
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np   # pip install emu-harness[audio]

CPU_HZ = 1_789_773
FRAME_CYCLES = 29780.5
RATE = 44_100
OVERSAMPLE = 4

LENGTH = [10, 254, 20, 2, 40, 4, 80, 6, 160, 8, 60, 10, 14, 12, 26, 14,
          12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30]
NOISE_PERIOD = [4, 8, 16, 32, 64, 96, 128, 160, 202, 254, 380, 508, 762, 1016, 2034, 4068]
DUTY = np.array([[0, 1, 0, 0, 0, 0, 0, 0], [0, 1, 1, 0, 0, 0, 0, 0],
                 [0, 1, 1, 1, 1, 0, 0, 0], [1, 0, 0, 1, 1, 1, 1, 1]], dtype=np.float64)
TRIANGLE = np.array(list(range(15, -1, -1)) + list(range(16)), dtype=np.float64)
# frame counter steps in CPU cycles (2 per APU cycle): (offset, quarter, half), and the sequence length
STEPS = {False: ([(7456, 1, 0), (14912, 1, 1), (22370, 1, 0), (29828, 1, 1)], 29830),
         True: ([(7456, 1, 0), (14912, 1, 1), (22370, 1, 0), (37280, 1, 1)], 37282)}


class Envelope:
    def __init__(self):
        self.start = False
        self.divider = 0
        self.decay = 0
        self.loop = 0
        self.const = 0
        self.vol = 0

    def quarter(self):
        if self.start:
            self.start, self.decay, self.divider = False, 15, self.vol
        elif self.divider == 0:
            self.divider = self.vol
            if self.decay:
                self.decay -= 1
            elif self.loop:
                self.decay = 15
        else:
            self.divider -= 1

    def output(self):
        return self.vol if self.const else self.decay


class Pulse:
    def __init__(self, ones_complement):
        self.ones = ones_complement            # pulse 1 negates with ones' complement
        self.env = Envelope()
        self.duty = 0
        self.length = 0
        self.timer = 0
        self.phase = 0.0
        self.sweep_on = self.negate = self.shift = self.period = 0
        self.sweep_reload = False
        self.sweep_div = 0

    def target(self):
        change = self.timer >> self.shift
        if self.negate:
            change = -change - (1 if self.ones else 0)
        return max(0, self.timer + change)

    def muted(self):
        return self.timer < 8 or self.target() > 0x7FF

    def write(self, reg, v, enabled):
        if reg == 0:
            self.duty, self.env.loop, self.env.const, self.env.vol = v >> 6, (v >> 5) & 1, (v >> 4) & 1, v & 15
        elif reg == 1:
            self.sweep_on, self.period, self.negate, self.shift = v >> 7, (v >> 4) & 7, (v >> 3) & 1, v & 7
            self.sweep_reload = True
        elif reg == 2:
            self.timer = (self.timer & 0x700) | v
        else:
            self.timer = (self.timer & 0xFF) | (v & 7) << 8
            if enabled:
                self.length = LENGTH[v >> 3]
            self.phase = 0.0                     # the sequencer restarts
            self.env.start = True

    def half(self):
        if self.length and not self.env.loop:
            self.length -= 1
        if self.sweep_div == 0 and self.sweep_on and self.shift and not self.muted():
            self.timer = self.target()
        if self.sweep_div == 0 or self.sweep_reload:
            self.sweep_div, self.sweep_reload = self.period, False
        else:
            self.sweep_div -= 1

    def render(self, dt):
        if not self.length or self.muted():
            return np.zeros(len(dt))
        steps = self.phase + dt / (2 * (self.timer + 1))     # a duty step lasts 2*(t+1) CPU cycles
        return DUTY[self.duty][np.floor(steps).astype(np.int64) % 8] * self.env.output()

    def advance(self, cycles):
        self.phase = (self.phase + cycles / (2 * (self.timer + 1))) % 8


class Triangle:
    def __init__(self):
        self.control = 0
        self.reload_value = 0
        self.linear = 0
        self.reload = False
        self.length = 0
        self.timer = 0
        self.phase = 0.0

    def write(self, reg, v, enabled):
        if reg == 0:
            self.control, self.reload_value = v >> 7, v & 0x7F
        elif reg == 2:
            self.timer = (self.timer & 0x700) | v
        elif reg == 3:
            self.timer = (self.timer & 0xFF) | (v & 7) << 8
            if enabled:
                self.length = LENGTH[v >> 3]
            self.reload = True

    def quarter(self):
        if self.reload:
            self.linear = self.reload_value
        elif self.linear:
            self.linear -= 1
        if not self.control:
            self.reload = False

    def half(self):
        if self.length and not self.control:
            self.length -= 1

    def render(self, dt):
        if not (self.length and self.linear):    # the sequencer stops: hold the last step
            return np.full(len(dt), TRIANGLE[int(self.phase) % 32])
        steps = self.phase + dt / (self.timer + 1)           # one step per t+1 CPU cycles
        return TRIANGLE[np.floor(steps).astype(np.int64) % 32]

    def advance(self, cycles):
        if self.length and self.linear:
            self.phase = (self.phase + cycles / (self.timer + 1)) % 32


class Noise:
    def __init__(self):
        self.env = Envelope()
        self.mode = 0
        self.period = NOISE_PERIOD[0]
        self.length = 0
        self.shift = 1
        self.acc = 0.0                           # CPU cycles since the last LFSR step

    def write(self, reg, v, enabled):
        if reg == 0:
            self.env.loop, self.env.const, self.env.vol = (v >> 5) & 1, (v >> 4) & 1, v & 15
        elif reg == 2:
            self.mode, self.period = v >> 7, NOISE_PERIOD[v & 15]
        elif reg == 3:
            if enabled:
                self.length = LENGTH[v >> 3]
            self.env.start = True

    def step(self, shift):
        bit = (shift ^ (shift >> (6 if self.mode else 1))) & 1
        return (shift >> 1) | (bit << 14)

    def render(self, dt):
        n = len(dt)
        if not n or not self.length:
            return np.zeros(n)
        k = np.floor((self.acc + dt) / self.period).astype(np.int64)     # LFSR steps before each sample
        bits, s = np.empty(k[-1] + 1, dtype=np.int64), self.shift
        bits[0] = s & 1
        for i in range(1, k[-1] + 1):
            s = self.step(s)
            bits[i] = s & 1
        return (1 - bits[k]) * float(self.env.output())                  # muted while bit 0 is set

    def advance(self, cycles):
        n = int((self.acc + cycles) // self.period)
        for _ in range(n):
            self.shift = self.step(self.shift)
        self.acc = self.acc + cycles - n * self.period


class APU:
    """Play a register stream: render([(cpu_cycle, addr, value), ...], seconds) -> float samples at RATE."""
    def __init__(self):
        self.p1, self.p2, self.tri, self.noise = Pulse(True), Pulse(False), Triangle(), Noise()
        self.enabled = [False] * 4
        self.five_step = False
        self.fc_origin = 0.0

    def write(self, addr, v):
        if 0x4000 <= addr <= 0x400F:
            ch = (addr - 0x4000) >> 2
            [self.p1, self.p2, self.tri, self.noise][ch].write(addr & 3, v, self.enabled[ch])
        elif addr == 0x4015:
            self.enabled = [bool(v >> i & 1) for i in range(4)]
            for ch, on in zip((self.p1, self.p2, self.tri, self.noise), self.enabled):
                if not on:
                    ch.length = 0

    def quarter(self):
        self.p1.env.quarter(), self.p2.env.quarter(), self.tri.quarter(), self.noise.env.quarter()

    def half(self):
        self.p1.half(), self.p2.half(), self.tri.half()
        if self.noise.length and not self.noise.env.loop:
            self.noise.length -= 1

    def frame_ticks(self, start, end):
        """Frame-counter clocks in (start, end] since the last $4017 write: [(cycle, quarter, half)]."""
        steps, length = STEPS[self.five_step]
        out, base = [], self.fc_origin + np.floor((start - self.fc_origin) / length) * length
        while base <= end:
            for off, q, h in steps:
                c = base + off
                if start < c <= end:
                    out.append((c, q, h))
            base += length
        return out

    def render(self, writes, seconds):
        total = int(seconds * RATE) * OVERSAMPLE        # whole output samples
        times = np.arange(total) * (CPU_HZ / (RATE * OVERSAMPLE))
        out = np.zeros(total)
        events = sorted(writes, key=lambda w: w[0])
        t, i, s = 0.0, 0, 0
        end = times[-1] if total else 0.0
        while t <= end:
            next_write = events[i][0] if i < len(events) else np.inf
            ticks = self.frame_ticks(t, min(next_write, end + 1))
            nxt = min([next_write] + [c for c, _, _ in ticks[:1]] + [end + 1])
            e = int(np.searchsorted(times, nxt, side="left"))
            if e > s:
                dt = times[s:e] - t
                p = self.p1.render(dt) + self.p2.render(dt)
                tr, no = self.tri.render(dt), self.noise.render(dt)
                pulse = np.where(p > 0, 95.88 / (8128.0 / np.maximum(p, 1e-9) + 100), 0.0)
                den = tr / 8227 + no / 12241
                tnd = np.where(den > 0, 159.79 / (1.0 / np.maximum(den, 1e-12) + 100), 0.0)
                out[s:e] = pulse + tnd
                s = e
            for ch in (self.p1, self.p2, self.tri, self.noise):     # the whole span, sampled or not
                ch.advance(nxt - t)
            t = nxt
            if ticks and ticks[0][0] == nxt:
                _, q, h = ticks[0]
                if q:
                    self.quarter()
                if h:
                    self.half()
            while i < len(events) and events[i][0] <= t:
                _, addr, v = events[i]
                if addr == 0x4017:
                    self.five_step, self.fc_origin = bool(v >> 7), t
                    if self.five_step:
                        self.quarter(), self.half()
                else:
                    self.write(addr, v)
                i += 1
        return out.reshape(-1, OVERSAMPLE).mean(axis=1)


def frames_to_cycles(stream):
    """[(frame, addr, value)] -> [(cpu_cycle, addr, value)]: a frame's writes land at its start, in order."""
    out, seen = [], {}
    for f, addr, v in stream:
        k = seen[f] = seen.get(f, -1) + 1
        out.append((f * FRAME_CYCLES + 12 * k, addr, v))
    return out


def write_wav(samples, path, gain=0.9, peak=None):
    """Float samples (the mixer's 0..~1 scale) -> 16-bit mono WAV, DC removed by a ~37 Hz high-pass that
    starts at the first sample's level (an idle triangle's DC is not a click). peak: scale so the
    largest sample is that fraction of full scale (returns the gain used)."""
    a = np.exp(-2 * np.pi * 37 / RATE)
    y = np.empty_like(samples)
    prev_x, prev_y = (samples[0] if len(samples) else 0.0), 0.0
    for n, x in enumerate(samples):                   # first-order high-pass
        prev_y = a * (prev_y + x - prev_x)
        prev_x = x
        y[n] = prev_y
    if peak is not None and np.abs(y).max() > 0:
        gain = peak / np.abs(y).max()
    pcm = np.clip(y * gain * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(Path(path)), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return gain
