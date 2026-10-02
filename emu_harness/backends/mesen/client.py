"""Python side of the Mesen 2 bridge: launch Mesen with bridge.lua and drive it in lockstep.

Machine-agnostic. A machine layer (e.g. emu_harness.machines.nes) supplies the button names, the
memory type to read and any battery-save handling.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BRIDGE_LUA = Path(__file__).resolve().parent / "bridge.lua"


def load_config(path: Path | None = None) -> dict:
    path = path or REPO / "config.toml"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing: copy config.example.toml and set this machine's paths")
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg["mesen"] = Path(cfg["mesen"])
    cfg["home"] = Path(cfg.get("home") or cfg["mesen"].parent)
    return cfg


def save_files(home: Path, rom: Path) -> list[Path]:
    """Battery/save-RAM files Mesen keeps for this ROM (named after the ROM file)."""
    return [p for p in (home / "Saves").glob(glob_escape(rom.stem) + ".*")]


def glob_escape(s: str) -> str:
    return "".join(f"[{c}]" if c in "[]*?" else c for c in s)


class BridgeError(RuntimeError):
    pass


class Mesen:
    """One Mesen instance, driven frame by frame through bridge.lua.

    headless=True uses --testRunner: the script is attached before the first frame,
    so frame 0 is power-on. GUI mode attaches the script after the ROM has started.
    """

    def __init__(self, rom: Path, *, keys: list[str], port: int = 0, headless: bool = True,
                 run_timeout_s: int = 86400, config: dict | None = None, log_dir: Path | None = None,
                 switches: list[str] = ()):
        cfg = config or load_config()
        self.rom = Path(rom)
        self.keys = list(keys)
        self.inputs: list[int] = []          # one mask per emulated frame since power-on
        self.input_log_valid = True          # False once a savestate load breaks the frame chain
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        env = dict(os.environ, MESEN_HARNESS_PORT=str(srv.getsockname()[1]))
        args = [str(cfg["mesen"])]
        if headless:
            args += ["--testRunner", f"--timeout={run_timeout_s}"]
        # Mesen config overrides, e.g. "--Nes.RamPowerOnState=AllOnes". Only bool/enum settings
        # and numbers with a [MinMax] range apply; others are silently ignored (ConfigManager.cs).
        args += list(switches)
        args += [str(BRIDGE_LUA), str(self.rom)]
        log_dir = log_dir or REPO / "_scratch"
        log_dir.mkdir(exist_ok=True)
        self._log = open(log_dir / f"mesen_{int(time.time())}.log", "w")
        self.proc = subprocess.Popen(args, env=env, stdout=self._log, stderr=subprocess.STDOUT)
        srv.settimeout(30)
        try:
            self.conn, _ = srv.accept()
        except socket.timeout:
            self.proc.kill()
            raise BridgeError("Mesen never connected to the bridge (Lua network access enabled?)")
        finally:
            srv.close()
        self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.conn.settimeout(600)
        self._rf = self.conn.makefile("rb")
        if self.cmd("ping") != "pong":
            raise BridgeError("bad handshake")
        self.cmd("keys " + ",".join(self.keys))

    # -- wire ---------------------------------------------------------------
    def cmd(self, line: str) -> str:
        self.conn.sendall((line + "\n").encode())
        resp = self._rf.readline()
        if not resp:
            code = self.proc.poll()
            raise BridgeError(f"bridge connection lost; Mesen exit code {code}")
        resp = resp.decode().rstrip("\n")
        if resp.startswith("err "):
            raise BridgeError(resp)
        return resp

    @staticmethod
    def _frame(resp: str) -> int:
        k, _, v = resp.partition("=")
        if k != "frame":
            raise BridgeError(f"expected frame=, got {resp!r}")
        return int(v)

    # -- input (every frame goes through here: the one recorder) -------------
    def mask(self, buttons) -> int:
        m = 0
        for b in buttons:
            m |= 1 << self.keys.index(b)
        return m

    def step(self, buttons=(), frames: int = 1, port: int = 0) -> int:
        m = self.mask(buttons)
        self.inputs.extend([m] * frames)
        return self._frame(self.cmd(f"step {frames} {m} {port}"))

    def run_masks(self, masks: list[int], port: int = 0, chunk: int = 4096) -> int:
        """Apply one mask per frame. Returns the frame counter after the last one."""
        if len(self.keys) > 8:
            raise ValueError("seq packs one byte per frame; more than 8 buttons needs a wider encoding")
        f = None
        for i in range(0, len(masks), chunk):
            part = masks[i:i + chunk]
            self.inputs.extend(part)
            f = self._frame(self.cmd(f"seq {port} " + "".join(f"{m:02x}" for m in part)))
        return f if f is not None else self.frame()

    # -- state --------------------------------------------------------------
    def info(self) -> dict:
        return dict(kv.split("=", 1) for kv in self.cmd("info").split(" ", 2))

    def frame(self) -> int:
        return int(self.info()["frame"])

    def read(self, mem_type: str, addr: int, length: int = 1) -> bytes:
        return bytes.fromhex(self.cmd(f"read {mem_type} {addr} {length}"))

    def save(self) -> str:
        return self.cmd("save")

    def load(self, state: str) -> int:
        self.input_log_valid = False
        return self._frame(self.cmd(f"load {state}"))

    def trace(self, mem_type: str, addr: int, length: int, path: Path,
              flag_addr: int | None = None) -> None:
        """Append, at every endFrame from now on, 1 flag byte (flag_addr read during the frame)
        + `length` bytes from `addr` to `path`. trace_off() closes it."""
        spec = f"{mem_type} {addr} {length} {Path(path)}"
        if flag_addr is not None:
            spec += f"|{flag_addr}"
        self.cmd("trace " + spec)

    def trace_off(self) -> None:
        self.cmd("trace off")

    def input_keys(self, port: int = 0) -> list[str]:
        return self.cmd(f"inputkeys {port}").split(",")

    # -- lifecycle ------------------------------------------------------------
    def close(self) -> int | None:
        try:
            self.cmd("quit")
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            code = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = None
        self._log.close()
        return code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
