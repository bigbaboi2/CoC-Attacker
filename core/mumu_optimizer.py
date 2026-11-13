import os
import re
import sys
import time
import subprocess
from typing import Optional, List, Dict
from core.adb_client import ADBSocketClient

class MuMuOptimizer:
    """
    MuMu Player 12 & Android Emulator Performance Optimizer.
    - Windows process priority tuning (High / Above Normal) for zero frame drops
    - Android animation scaling disabling (0.0x) for lightning-fast UI responses
    - Screen stay-on & power lock prevention
    - Health watchdog: auto-detects crashes and restarts game cleanly
    """

    # Only optimize direct game and emulator render processes (avoid SYSTEM services)
    TARGET_PROCESSES = ["MuMuNxDevice", "MuMuNxMain", "adb", "NemuPlayer"]
    COC_PACKAGE = "com.supercell.clashofclans"
    COC_ACTIVITY = "com.supercell.titan.GameApp"

    def __init__(self, adb_client: ADBSocketClient):
        self.adb = adb_client

    def optimize_windows_priority(self) -> Dict[str, str]:
        """
        Elevate MuMu and ADB process priority in Windows to Above Normal
        to avoid micro-stuttering during background execution.
        Safely ignores SYSTEM services without permission errors.
        """
        results = {}
        try:
            import win32process
            import win32api
            import win32con

            # Fast process lookup via psutil (instant C-level iteration without spawning powershell)
            import psutil
            seen_names = set()
            target_bases = {"mumunxdevice", "mumunxmain", "adb", "nemuplayer"}

            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pname = proc.info.get("name") or ""
                    base_name = pname.lower().replace(".exe", "")
                    if base_name in target_bases:
                        pid = proc.info["pid"]
                        try:
                            handle = win32api.OpenProcess(
                                win32con.PROCESS_SET_INFORMATION | win32con.PROCESS_QUERY_INFORMATION, False, pid
                            )
                            win32process.SetPriorityClass(handle, win32process.NORMAL_PRIORITY_CLASS)
                            win32api.CloseHandle(handle)

                            friendly_name = {
                                "mumunxdevice": "MuMu Player Engine (Xử lý đồ họa 60 FPS)",
                                "mumunxmain": "MuMu Player UI (Giao diện chính)",
                                "adb": "ADB Daemon (Kênh truyền dữ liệu siêu tốc)",
                                "nemuplayer": "Nemu Player Core"
                            }.get(base_name, pname)

                            if friendly_name not in seen_names:
                                results[friendly_name] = "Đã cân bằng mức ưu tiên (Normal)"
                                seen_names.add(friendly_name)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception as e:
            results["Thông báo"] = f"Không thể chỉnh Windows Process: {e}"

        if not results:
            results["MuMu Player & ADB"] = "Đã kiểm tra tiến trình (Không cần nâng thêm)"

        return results

    def apply_android_optimizations(self) -> List[str]:
        """
        Tune Android system settings inside the emulator for maximum bot speed:
        - Disable UI animations (0.0x scale -> instant page transitions)
        - Prevent screen sleep
        """
        actions = []
        try:
            # 1. Disable animations for snappy rendering
            self.adb.shell("settings put global window_animation_scale 0")
            self.adb.shell("settings put global transition_animation_scale 0")
            self.adb.shell("settings put global animator_duration_scale 0")
            actions.append("Tắt toàn bộ hiệu ứng Animation Android (0.0x) giúp chuyển cảnh cực mượt")

            # 2. Stay awake while charging/USB
            self.adb.shell("svc power stayon true")
            self.adb.shell("settings put system screen_off_timeout 2147483647")
            actions.append("Khóa màn hình luôn sáng (chống tắt màn hình khi bot đang chạy)")

            # 3. Ensure sound/volume does not interfere
            self.adb.shell("cmd media_session volume --stream 3 --set 0")
            actions.append("Tắt âm thanh giả lập để bot chạy êm ái")

        except Exception as e:
            actions.append(f"Cảnh báo cấu hình Android: {e}")

        return actions

    def verify_screen_resolution(self) -> Dict[str, any]:
        """
        Check if emulator screen resolution is optimal (1600x900 @ 240 DPI).
        """
        w, h = self.adb.get_screen_size()
        dpi_str = self.adb.shell("wm density")
        dpi = 240
        if "Physical density:" in dpi_str:
            try:
                dpi = int(dpi_str.split("Physical density:")[1].strip().split()[0])
            except Exception:
                pass

        is_optimal = (w == 1600 and h == 900) or (w == 1280 and h == 720)
        return {
            "width": w,
            "height": h,
            "dpi": dpi,
            "is_optimal": is_optimal,
            "recommendation": "1600x900 (240 DPI)" if not is_optimal else "Optimal"
        }

    def set_optimal_resolution(self):
        """Set the base size in the emulator's natural orientation."""
        physical_size = re.search(r"Physical size:\s*(\d+)x(\d+)", self.adb.shell("wm size"))
        if physical_size is None:
            raise RuntimeError("Không đọc được kích thước gốc của MuMu qua ADB.")
        width, height = map(int, physical_size.groups())
        # Android rotates the base display when Clash of Clans requests landscape.
        size = "900x1600" if height > width else "1600x900"
        self.adb.shell(f"wm size {size}")
        self.adb.shell("wm density 240")

    def ensure_coc_running(self) -> bool:
        """
        Checks if Clash of Clans is active and foregrounded.
        Restarts if not running.
        """
        if not self.adb.is_coc_foreground():
            print("[MuMuOptimizer] Clash of Clans is not running. Launching...")
            self.adb.start_coc()
            time.sleep(5)
            return True
        return False

    def restart_game(self):
        """Clean restart of Clash of Clans."""
        print("[MuMuOptimizer] Force stopping and restarting Clash of Clans...")
        self.adb.kill_coc()
        time.sleep(2)
        self.adb.start_coc()
        time.sleep(6)
