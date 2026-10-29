"""The sound lane (machines.nes.sound) and the two VM pieces it needed: the MMC1 serial port and the
native syscall path. The NA1 test runs only where the ROM is (games.toml resolves it)."""
import pytest

from emu_harness.machines.nes import sound
from emu_harness.vm.koei.nes.cpu6502 import Memory


class FakeBus:
    def __init__(self):
        self.ram = bytearray(0x800)

    def write(self, addr, val):
        if addr < 0x800:
            self.ram[addr] = val


class FakeEngine:
    """Writes one note per frame on a 150-frame cycle once requested; the request arms it."""
    events = {"tune": (3, 1, "loop"), "blip": (1, 1)}

    def machine(self):
        self.bus, self.armed, self.n = FakeBus(), 0, 0
        return self.bus

    def init(self):
        self.bus.write(0x4015, 0x0F)
        self.bus.write(0x4014, 0x02)            # OAM DMA: not an APU register
        self.bus.write(0x4016, 0x01)            # controller strobe: not an APU register

    def request(self, r):
        self.armed = r

    def tick(self):
        if self.armed == 1 and self.n >= 20:
            return
        if self.armed:
            self.bus.write(0x4002, self.n % 150)
            self.n += 1


def test_tap_records_apu_registers_only_and_passes_writes_through():
    rec, bus = sound.Recorder(), FakeBus()
    sound.tap(bus, rec)
    rec.frame = 7
    for a in (0x4000, 0x4014, 0x4016, 0x4017, 0x0010):
        bus.write(a, 0x55)
    assert rec.writes == [(7, 0x4000, 0x55), (7, 0x4017, 0x55)]
    assert bus.ram[0x10] == 0x55


def test_capture_find_loop_and_last_change():
    s = sound.capture(FakeEngine(), 3, 400)
    assert s[0] == (0, 0x4015, 0x0F) and s[1] == (1, 0x4002, 0)
    assert sound.find_loop(s) == (1, 150)
    blip = sound.capture(FakeEngine(), 1, 400)
    assert sound.last_change(blip) == 20 and sound.find_loop(blip) is None


def test_mmc1_serial_port_selects_the_prg_bank_and_is_opt_in():
    mem = Memory(bytes(16 + 16 * 0x4000))

    def load(bank, addr=0xFFF0):
        for i in range(5):
            mem.write(addr, (bank >> i) & 1)

    load(10)
    assert mem.bank_lo == 0                     # off by default: the VM runners set banks themselves
    mem.mmc1_serial = True
    load(10)
    assert mem.bank_lo == 10
    mem.write(0xFFF0, 1)
    mem.write(0xFFF0, 0x80)                     # bit 7 resets the shift register mid-load
    load(3)
    assert mem.bank_lo == 3
    load(5, addr=0x8000)                        # the control register: not modelled, bank unchanged
    assert mem.bank_lo == 3


def _na1():
    from emu_harness.vm.koei.nes import titles
    try:
        return titles.make_vm("na1")[0]
    except (FileNotFoundError, KeyError, OSError) as e:
        pytest.skip(f"NA1 ROM not here: {e}")


def _request_song_0(vm):
    vm.mem.switch_bank(10)
    vm.mem.write(0x6F4D, 1)                     # audio_wait_gate
    vm.mem.write_word(0x7FE5, 0)                # sound_request_id
    vm.vm_fp, vm.vm_sp, vm.vm_pc = 0x05FF, 0x05F9, 0x8008
    vm.cpu.pc = vm.DISPATCHER_ADDR
    vm.run_until_outermost_return(max_ops=200_000)


def test_na1_song_request_through_the_kernel_arms_three_voices():
    """play_audio_by_id(0) loads voices 1-3 from $8617 (stream pointers $8090 / $80D9 / $8110) and
    arms them: audio_control mode 1 sets $0720 + voice. The Python stand-in for audio_control does
    not, so the native path is what the sound lane needs."""
    vm = _na1()
    vm.native_syscalls = {0x09, 0x0A}
    _request_song_0(vm)
    m = vm.mem
    assert [m.read(0x0720 + i) for i in range(5)] == [0, 1, 1, 1, 0]
    assert [m.read_word(0x0726 + 3 * v) for v in (1, 2, 3)] == [0x8090, 0x80D9, 0x8110]


def test_na1_emulated_audio_load_voice_reads_the_pointer_as_one_word():
    vm = _na1()
    _request_song_0(vm)                         # the Python layer: audio_load_voice EMU, control STUB
    assert [vm.mem.read_word(0x0726 + 3 * v) for v in (1, 2, 3)] == [0x8090, 0x80D9, 0x8110]
