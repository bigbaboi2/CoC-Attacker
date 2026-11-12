import os
import sys
import time
import socket
import random
import threading
import subprocess
import numpy as np
import cv2
from typing import Optional, Tuple, List, Dict


class BotStoppedError(Exception):
    """Raised inside a stale worker thread after the user pressed Stop/Start."""


class ADBSocketClient:
    """
    High-Performance Direct ADB Socket Client.
    Bypasses launching new adb.exe processes on every action, delivering:
    - ~300ms raw framebuffer capture (vs ~1500ms adb exec-out screencap)
    - ~30-40ms instant touch & swipe execution
    - Automatic anti-ban jitter on coordinates
    """

    device_unavailable = False

    def __init__(self, host: str = "127.0.0.1", port: int = 5037, adb_bin: Optional[str] = None):
        self.host = host
        self.port = port
        self.device_serial: Optional[str] = None
        self.adb_bin = adb_bin or self._find_adb_binary()
        # Only this worker thread may send input. Other "CoCAttackWorker" threads
        # (left over from a previous Start) are aborted on their next tap/swipe.
        self.owner_thread: Optional[object] = None
        self.control_callback = None

    def _guard_input(self):
        cur = threading.current_thread()
        if cur.name.startswith("CoCAttackWorker") and cur is not self.owner_thread:
            raise BotStoppedError()
        if cur is self.owner_thread and self.control_callback:
            if self.device_unavailable:
                raise BotStoppedError()
            self.control_callback()

    @staticmethod
    def _run_adb(args, **kwargs):
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(args, **kwargs)

    def _find_adb_binary(self) -> str:
        local_bin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "adb.exe")
        if os.path.exists(local_bin):
            return local_bin
        return "adb"

    def _ensure_server_running(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect((self.host, self.port))
                return
        except Exception:
            pass

        try:
            self._run_adb([self.adb_bin, "start-server"], check=True, capture_output=True, timeout=5)
            time.sleep(0.5)
        except Exception as e:
            print(f"[ADB] Warning: Could not start adb server: {e}")

    def _create_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10.0)
        try:
            s.connect((self.host, self.port))
        except OSError:
            s.close()
            raise
        return s

    def _send_command(self, s: socket.socket, cmd: bytes) -> bytes:
        length_str = f"{len(cmd):04x}".encode("ascii")
        s.sendall(length_str + cmd)
        resp = s.recv(4)
        return resp

    COMMON_EMULATOR_PORTS = [
        16384, 16385, 16386, 16387, 16388,
        16416, 16417, 16448, 7555, 5555, 5554, 62001, 21503
    ]

    def connect_remote(self, target: str) -> bool:
        """Connect to a remote TCP ADB device (e.g. 127.0.0.1:16384)."""
        try:
            with self._create_socket() as s:
                cmd = f"host:connect:{target}".encode("ascii")
                resp = self._send_command(s, cmd)
                if resp != b"OKAY":
                    return False
                len_bytes = s.recv(4)
                data_len = int(len_bytes.decode("ascii", errors="ignore"), 16)
                data = s.recv(data_len).decode("utf-8", errors="ignore")
                return "connected" in data or "already connected" in data
        except Exception:
            return False

    def _auto_connect_local_emulators(self):
        """Probe and auto-connect running local emulator instances (MuMu 12, MuMu 6, LDPlayer, etc.)."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as adb_probe:
                adb_probe.settimeout(0.05)
                if adb_probe.connect_ex((self.host, self.port)) != 0:
                    return
        except Exception:
            return

        ports_to_check = list(self.COMMON_EMULATOR_PORTS)
        mumu_cfg = os.environ.get("MUMU_VM_CONFIG", "")
        if os.path.exists(mumu_cfg):
            try:
                import json
                with open(mumu_cfg, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                m_port = cfg.get("vm", {}).get("nat", {}).get("port_forward", {}).get("adb", {}).get("host_port")
                if m_port:
                    m_port = int(m_port)
                    if m_port not in ports_to_check:
                        ports_to_check.insert(0, m_port)
            except Exception:
                pass

        for port in ports_to_check:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.04)
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        self.connect_remote(f"127.0.0.1:{port}")
            except Exception:
                continue

    def list_devices(self) -> List[Dict[str, str]]:
        """List attached ADB devices and their status."""
        try:
            self._auto_connect_local_emulators()
        except Exception:
            pass

        try:
            with self._create_socket() as s:
                resp = self._send_command(s, b"host:devices-l")
                if resp != b"OKAY":
                    with self._create_socket() as s_old:
                        resp = self._send_command(s_old, b"host:devices")
                        if resp != b"OKAY":
                            return []
                        len_bytes = s_old.recv(4)
                        data_len = int(len_bytes.decode("ascii", errors="ignore"), 16)
                        data = s_old.recv(data_len).decode("utf-8", errors="ignore")
                        devices = []
                        for line in data.strip().split("\n"):
                            line = line.strip()
                            if not line:
                                continue
                            parts = line.split("\t")
                            if len(parts) >= 2:
                                devices.append({"serial": parts[0].strip(), "state": parts[1].strip()})
                        return devices

                len_bytes = s.recv(4)
                data_len = int(len_bytes.decode("ascii", errors="ignore"), 16)
                data = s.recv(data_len).decode("utf-8", errors="ignore")

                devices = []
                for line in data.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        dev = {
                            "serial": parts[0].strip(),
                            "state": parts[1].strip(),
                            "product": "",
                            "model": "",
                            "device": ""
                        }
                        for token in parts[2:]:
                            if ":" in token:
                                k, v = token.split(":", 1)
                                if k in dev:
                                    dev[k] = v
                        devices.append(dev)
                return devices
        except OSError:
            self.device_unavailable = True
            return []
        except Exception:
            return []

    def connect_device(self, serial: str, *, start_server: bool = True) -> bool:
        """Set active device target."""
        if start_server:
            self._ensure_server_running()
        # A reconnect must be allowed to probe a previously disconnected
        # device; the flag will be set again if the probe fails.
        self.device_unavailable = False
        self.device_serial = serial

        if ":" in serial:
            self.connect_remote(serial)

        # Test connectivity
        try:
            res = self.shell("wm size")
            ok = "Physical size" in res
            if not ok:
                self.device_unavailable = True
            return ok
        except BotStoppedError:
            raise
        except Exception:
            self.device_unavailable = True
            return False

    def _transport_socket(self) -> socket.socket:
        if not self.device_serial:
            raise ValueError("No device serial specified. Call connect_device() first.")
        if self.device_unavailable:
            raise ConnectionError('ADB connection suspended')
        s = None
        try:
            s = self._create_socket()
            cmd = f"host:transport:{self.device_serial}".encode("ascii")
            if self._send_command(s, cmd) != b"OKAY":
                raise ConnectionError(f'ADB device {self.device_serial} is unavailable')
            return s
        except OSError:
            self.device_unavailable = True
            if s is not None:
                s.close()
            raise

    def shell(self, command: str) -> str:
        """Run adb shell command via socket."""
        self._guard_input()
        try:
            with self._transport_socket() as s:
                cmd = f"exec:{command}".encode("utf-8")
                resp = self._send_command(s, cmd)
                if resp != b"OKAY":
                    return self._shell_fallback(command)
                chunks = []
                while True:
                    data = s.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
                return b"".join(chunks).decode("utf-8", errors="ignore")
        except OSError:
            self.device_unavailable = True
            return 'ERROR: ADB device unavailable'
        except Exception:
            return self._shell_fallback(command)

    def _shell_fallback(self, command: str) -> str:
        if self.device_unavailable or not self.device_serial:
            return 'ERROR: ADB device unavailable'
        cmd = [self.adb_bin]
        if self.device_serial:
            cmd.extend(["-s", self.device_serial])
        cmd.extend(["shell", command])
        try:
            res = self._run_adb(cmd, capture_output=True, text=True, timeout=10)
            return res.stdout
        except Exception as e:
            return f"ERROR: {e}"

    def screencap(self) -> Optional[np.ndarray]:
        """
        Ultra-fast raw screencap via socket.
        Parses raw Android framebuffer into OpenCV BGR image (H, W, 3).
        """
        self._guard_input()
        if self.device_unavailable or not self.device_serial:
            return None
        try:
            with self._transport_socket() as s:
                cmd = b"exec:screencap"
                resp = self._send_command(s, cmd)
                if resp != b"OKAY":
                    return self._screencap_fallback()

                # Read header: 16 bytes (width: 4B, height: 4B, format: 4B, colorspace: 4B)
                header = bytearray(16)
                view = memoryview(header)
                pos = 0
                while pos < 16:
                    n = s.recv_into(view[pos:])
                    if not n:
                        break
                    pos += n

                if pos < 16:
                    return self._screencap_fallback()

                width = int.from_bytes(header[0:4], "little")
                height = int.from_bytes(header[4:8], "little")
                fmt = int.from_bytes(header[8:12], "little")
                expected_bytes = width * height * 4

                # Reusable buffer to minimize memory allocation and GC pauses
                if not hasattr(self, '_screencap_buf') or len(self._screencap_buf) != expected_bytes:
                    self._screencap_buf = bytearray(expected_bytes)
                buf = self._screencap_buf
                view = memoryview(buf)
                pos = 0
                while pos < expected_bytes:
                    n = s.recv_into(view[pos:])
                    if not n:
                        break
                    pos += n

                if pos < expected_bytes:
                    return self._screencap_fallback()

                # Reshape and convert RGBA -> BGR for OpenCV
                rgba = np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 4))
                bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
                return bgr
        except OSError:
            self.device_unavailable = True
            return None
        except Exception:
            # Fallback
            return self._screencap_fallback()

    def _screencap_fallback(self) -> Optional[np.ndarray]:
        if self.device_unavailable or not self.device_serial:
            return None
        try:
            cmd = [self.adb_bin]
            if self.device_serial:
                cmd.extend(["-s", self.device_serial])
            cmd.extend(["exec-out", "screencap -p"])
            res = self._run_adb(cmd, capture_output=True, timeout=5)
            if res.returncode == 0 and res.stdout:
                img = cv2.imdecode(np.frombuffer(res.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
                return img
        except Exception:
            pass
        return None

    def tap(self, x: int, y: int, jitter: int = 3, delay_after: float = 0.05):
        """
        Fast tap with subtle anti-detection coordinate jitter.
        """
        self._guard_input()
        if jitter > 0:
            x += random.randint(-jitter, jitter)
            y += random.randint(-jitter, jitter)

        cmd = f"exec:input tap {int(x)} {int(y)}".encode("ascii")
        try:
            with self._transport_socket() as s:
                self._send_command(s, cmd)
                # Read quick response ACK
                s.recv(128)
        except Exception:
            self.shell(f"input tap {int(x)} {int(y)}")

        if delay_after > 0:
            time.sleep(delay_after)

    def tap_batch(self, points, jitter=3, delay_after=0.05, interval_ms=0, before_chunk=None, step_delays_ms=None):
        """Bounded batches, with stop/pause checks and configured delay between taps."""
        interval=max(0,min(1000,int(interval_ms)))/1000.0
        if step_delays_ms is not None and len(step_delays_ms) != len(points):
            raise ValueError("One delay is required per tap")
        for offset in range(0,len(points),4):
            self._guard_input()
            if before_chunk:
                before_chunk()
            commands=[]
            for index, (px, py) in enumerate(points[offset:offset+4], start=offset):
                x=int(px)+(random.randint(-jitter,jitter) if jitter else 0)
                y=int(py)+(random.randint(-jitter,jitter) if jitter else 0)
                commands.append(f'input tap {x} {y}')
                wait = interval if step_delays_ms is None else max(0, min(1000, step_delays_ms[index])) / 1000
                if wait:
                    commands.append(f'sleep {wait:.3f}')
            command='; '.join(commands)
            with self._transport_socket() as sock:
                if self._send_command(sock,('exec:'+command).encode('ascii'))!=b'OKAY':
                    raise ConnectionError('ADB rejected tap batch')
                output=bytearray()
                while True:
                    chunk=sock.recv(4096)
                    if not chunk:break
                    output.extend(chunk)
                if output.strip():
                    raise RuntimeError('ADB tap batch reported: '+output.decode('utf-8',errors='replace'))
        self._guard_input()
        if delay_after>0:time.sleep(delay_after)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, jitter: int = 2):
        """
        Smooth swipe between two coordinates.
        """
        self._guard_input()
        if jitter > 0:
            x1 += random.randint(-jitter, jitter)
            y1 += random.randint(-jitter, jitter)
            x2 += random.randint(-jitter, jitter)
            y2 += random.randint(-jitter, jitter)

        cmd = f"exec:input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}".encode("ascii")
        try:
            with self._transport_socket() as s:
                self._send_command(s, cmd)
                s.recv(128)
        except Exception:
            self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def keyevent(self, keycode: int):
        self._guard_input()
        self.shell(f"input keyevent {keycode}")

    def get_screen_size(self) -> Tuple[int, int]:
        """Returns (width, height) of current display."""
        out = self.shell("dumpsys window displays")
        for line in out.split("\n"):
            if "cur=" in line and "app=" in line:
                # e.g.: cur=1600x900
                import re
                m = re.search(r"cur=(\d+)x(\d+)", line)
                if m:
                    return int(m.group(1)), int(m.group(2))
        # Fallback to wm size
        out = self.shell("wm size")
        import re
        m = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            # Orientation landscape if w < h
            return (max(w, h), min(w, h))
        return (1600, 900)

    def zoom_out_map(self, times: int = 2):
        """
        Thu nhỏ bản đồ (Pinch-to-zoom out) dành cho MuMu Player:
        Gửi chuỗi sự kiện cảm ứng đa điểm (multi-touch) thu 2 ngón tay lại gần nhau trên /dev/input/event4.
        """
        self._guard_input()
        single_pinch = (
            'sendevent /dev/input/event4 1 330 1; '
            'sendevent /dev/input/event4 3 47 18; sendevent /dev/input/event4 3 57 18; '
            'sendevent /dev/input/event4 3 53 210; sendevent /dev/input/event4 3 54 800; '
            'sendevent /dev/input/event4 3 47 19; sendevent /dev/input/event4 3 57 19; '
            'sendevent /dev/input/event4 3 53 678; sendevent /dev/input/event4 3 54 800; '
            'sendevent /dev/input/event4 0 0 0; usleep 12000; '
            'sendevent /dev/input/event4 3 47 18; sendevent /dev/input/event4 3 53 238; '
            'sendevent /dev/input/event4 3 47 19; sendevent /dev/input/event4 3 53 650; '
            'sendevent /dev/input/event4 0 0 0; usleep 12000; '
            'sendevent /dev/input/event4 3 47 18; sendevent /dev/input/event4 3 53 266; '
            'sendevent /dev/input/event4 3 47 19; sendevent /dev/input/event4 3 53 622; '
            'sendevent /dev/input/event4 0 0 0; usleep 12000; '
            'sendevent /dev/input/event4 3 47 18; sendevent /dev/input/event4 3 53 294; '
            'sendevent /dev/input/event4 3 47 19; sendevent /dev/input/event4 3 53 594; '
            'sendevent /dev/input/event4 0 0 0; usleep 12000; '
            'sendevent /dev/input/event4 3 47 18; sendevent /dev/input/event4 3 53 322; '
            'sendevent /dev/input/event4 3 47 19; sendevent /dev/input/event4 3 53 566; '
            'sendevent /dev/input/event4 0 0 0; usleep 12000; '
            'sendevent /dev/input/event4 3 47 18; sendevent /dev/input/event4 3 53 350; '
            'sendevent /dev/input/event4 3 47 19; sendevent /dev/input/event4 3 53 538; '
            'sendevent /dev/input/event4 0 0 0; usleep 12000; '
            'sendevent /dev/input/event4 3 47 18; sendevent /dev/input/event4 3 53 378; '
            'sendevent /dev/input/event4 3 47 19; sendevent /dev/input/event4 3 53 510; '
            'sendevent /dev/input/event4 0 0 0; usleep 12000; '
            'sendevent /dev/input/event4 3 47 18; sendevent /dev/input/event4 3 53 406; '
            'sendevent /dev/input/event4 3 47 19; sendevent /dev/input/event4 3 53 482; '
            'sendevent /dev/input/event4 0 0 0; usleep 12000; '
            'sendevent /dev/input/event4 3 47 18; sendevent /dev/input/event4 3 53 435; '
            'sendevent /dev/input/event4 3 47 19; sendevent /dev/input/event4 3 53 453; '
            'sendevent /dev/input/event4 0 0 0; usleep 12000; '
            'sendevent /dev/input/event4 3 47 18; sendevent /dev/input/event4 3 57 -1; '
            'sendevent /dev/input/event4 3 47 19; sendevent /dev/input/event4 3 57 -1; '
            'sendevent /dev/input/event4 1 330 0; sendevent /dev/input/event4 0 0 0'
        )
        for _ in range(times):
            self.shell(single_pinch)
            time.sleep(0.06)

    def is_coc_running(self) -> bool:
        """Check if Clash of Clans is running."""
        out = self.shell("pidof com.supercell.clashofclans")
        return bool(out.strip()) and all(part.isdigit() for part in out.split())

    def is_coc_foreground(self) -> bool:
        """A live process can still be behind the MuMu launcher."""
        out = self.shell("dumpsys activity activities")
        resumed = [line for line in out.splitlines()
                   if 'mResumedActivity' in line or 'topResumedActivity' in line]
        return any('com.supercell.clashofclans/' in line for line in resumed)

    def start_coc(self):
        """Launch Clash of Clans."""
        result = self.shell("am start -n com.supercell.clashofclans/com.supercell.titan.GameApp")
        if 'Error' in result or 'Exception' in result:
            self.shell("monkey -p com.supercell.clashofclans -c android.intent.category.LAUNCHER 1")

    def kill_coc(self):
        """Force stop Clash of Clans."""
        self.shell("am force-stop com.supercell.clashofclans")
