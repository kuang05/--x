"""真机驱动：Marlin 主机协议子集 + 传输层抽象。

协议依据（Marlin 主机接口文档 / 队列实现）：
  - 每行以 \\n 结束，可选 `N<行号> ... *<XOR 校验和>`；
  - 固件在命令**入队后**即回 `ok`，不是执行完毕；主机靠它控制发送节奏；
  - 出错回 `resend <n>` / `Error:...`，主机重发；
  - 温度用 `M105` 轮询，返回 `T:a /b B:c /d`。

本模块只做"能对接"：串口用 pyserial（若装了），没有串口时用内存回环 +
假固件跑测试。虚拟打印（printer.py）不依赖这里。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass


class Transport:
    def write(self, data: bytes) -> None: ...
    def readline(self, timeout: float = 2.0) -> bytes: ...
    def close(self) -> None: ...


class LoopbackTransport(Transport):
    """内存双队列回环：host→device、device→host，测试与演示用。"""

    def __init__(self):
        self.to_device: queue.Queue[bytes] = queue.Queue()
        self.to_host: queue.Queue[bytes] = queue.Queue()

    def write(self, data: bytes) -> None:
        self.to_device.put(data)

    def readline(self, timeout: float = 2.0) -> bytes:
        try:
            return self.to_host.get(timeout=timeout)
        except queue.Empty:
            return b""

    def close(self) -> None:
        pass


class SerialTransport(Transport):
    """pyserial 包装；未安装时给出明确错误，而不是静默降级。"""

    def __init__(self, port: str, baud: int = 115200):
        try:
            import serial  # type: ignore
        except ImportError as exc:      # pragma: no cover - 取决于环境
            raise RuntimeError("未安装 pyserial，无法连接真机；请 pip install pyserial") from exc
        self._ser = serial.Serial(port, baud, timeout=2)

    def write(self, data: bytes) -> None:
        self._ser.write(data)

    def readline(self, timeout: float = 2.0) -> bytes:
        self._ser.timeout = timeout
        return self._ser.readline()

    def close(self) -> None:
        self._ser.close()


def checksum(line: str) -> int:
    x = 0
    for ch in line:
        x ^= ord(ch)
    return x & 0xFF


@dataclass
class DriverStats:
    sent: int = 0
    ok: int = 0
    resend: int = 0
    errors: int = 0


class MarlinDriver:
    def __init__(self, transport: Transport, timeout: float = 2.0, with_checksum: bool = True):
        self.t = transport
        self.timeout = timeout
        self.with_checksum = with_checksum
        self.line_no = 0
        self.stats = DriverStats()

    def send(self, cmd: str, expect_ok: bool = True) -> list[str]:
        cmd = cmd.strip()
        if not cmd or cmd.startswith(";"):
            return []
        for attempt in range(3):
            self.line_no += 1
            payload = f"N{self.line_no} {cmd}"
            if self.with_checksum:
                payload += f"*{checksum(payload)}"
            self.t.write((payload + "\n").encode())
            self.stats.sent += 1
            if not expect_ok:
                return []
            replies: list[str] = []
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                raw = self.t.readline(self.timeout)
                if not raw:
                    break
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                replies.append(line)
                if line.startswith("ok"):
                    self.stats.ok += 1
                    return replies
                if line.lower().startswith("resend") or line.startswith("Error"):
                    self.stats.resend += 1 if line.lower().startswith("resend") else 0
                    self.stats.errors += 1 if line.startswith("Error") else 0
                    break
        raise TimeoutError(f"命令未得到 ok：{cmd}")

    # ---- 常用动作 ----
    def hello(self) -> str:
        return " | ".join(self.send("M115"))

    def home(self) -> None:
        self.send("G28")

    def preheat(self, bed_c: float | None = None, hotend_c: float | None = None) -> None:
        if bed_c is not None:
            self.send(f"M140 S{bed_c:.0f}")
        if hotend_c is not None:
            self.send(f"M104 S{hotend_c:.0f}")

    def query_temp(self) -> dict:
        replies = self.send("M105")
        for r in replies:
            if "T:" in r:
                return parse_temps(r)
        return {}

    def move(self, x=None, y=None, z=None, f=None) -> None:
        parts = ["G1"]
        for k, v in (("X", x), ("Y", y), ("Z", z), ("F", f)):
            if v is not None:
                parts.append(f"{k}{v:.3f}" if isinstance(v, float) else f"{k}{v}")
        self.send(" ".join(parts))

    def extruder(self, steps: float) -> None:
        self.send(f"G1 E{steps:.3f}")

    def stop(self) -> None:
        self.send("M112", expect_ok=False)


def parse_temps(line: str) -> dict:
    """解析 `T:25.0 /0.0 B:25.0 /0.0`，返回实际温度。"""
    out: dict[str, float] = {}
    for token in line.replace(":", ": ").split():
        pass
    import re
    m = re.search(r"T:\s*([\d.]+)", line)
    if m:
        out["hotend"] = float(m.group(1))
    m = re.search(r"B:\s*([\d.]+)", line)
    if m:
        out["bed"] = float(m.group(1))
    return out


class FakeMarlin(threading.Thread):
    """假固件：按 Marlin 行为回 ok/温度，用于在没有真机时验证驱动。"""

    def __init__(self, transport: LoopbackTransport, temp_c: float = 25.0):
        super().__init__(daemon=True)
        self.t = transport
        self.temp_c = temp_c
        self.received: list[str] = []
        self._halt = threading.Event()      # 不能叫 _stop：会盖掉 Thread._stop()

    def run(self) -> None:
        buf = b""
        while not self._halt.is_set():
            try:
                chunk = self.t.to_device.get(timeout=0.2)
            except queue.Empty:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                if "*" in line:
                    body, star = line.rsplit("*", 1)
                    try:
                        if int(star) != checksum(body):
                            self.t.to_host.put(b"Error:checksum mismatch\n")
                            self.t.to_host.put(f"resend {body.split()[0]}\n".encode())
                            continue
                    except ValueError:
                        pass
                else:
                    body = line
                self.received.append(body)
                cmd = body.split(" ", 1)[-1] if body.startswith("N") else body
                if cmd.startswith("M105"):
                    self.t.to_host.put(
                        f"T:{self.temp_c:.1f} /0.0 B:{self.temp_c:.1f} /0.0\n".encode())
                elif cmd.startswith("M115"):
                    self.t.to_host.put(b"FIRMWARE_NAME:FakeMarlin 2.1\n")
                elif cmd.startswith("M112"):
                    self.t.to_host.put(b"ok\n")
                    self._halt.set()
                self.t.to_host.put(b"ok\n")

    def join(self, timeout: float | None = None) -> None:
        self._halt.set()
        super().join(timeout)
