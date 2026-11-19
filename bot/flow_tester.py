"""Isolated flow test runner with detailed logging and dry-run simulation guards."""
import copy
import re
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from bot.apprentice import ApprenticeAssigner
from bot.army_upgrades import ArmyUpgrader
from bot.battle import BattleSearcher
from bot.builder_base import BuilderBaseManager
from bot.builder_battle import Troop
from bot.combat import CombatExecutor
from bot.farm_finish import FarmFinishController, ResourceCapacityReader
from bot.new_buildings import NewBuildingBuyer
from bot.suggested_upgrades import SuggestedUpgrader
from bot.treasury import TreasuryCollector
from bot.village import VillageManager
from bot.walls import WallUpgrader
from config.settings import (
    BUILDING_CATALOG,
    BUILDING_KEY_ALIASES,
    BUILDING_KEY_TO_CATEGORY,
    BotSettings,
)
from core.adb_client import ADBSocketClient, BotStoppedError
from core.attack_plan import strategy_points
from core.fixed_attack import FixedAttackPlan
from core.ultra_attack import UltraAttackPlan
from core.village_state import VillageStateManager
from core.vision import VisionEngine


class DryRunReached(BotStoppedError):
    """Raised when simulation reaches the target stop step without clicking confirm/spend."""

    def __init__(self, stage: str, info: Dict[str, Any], frame: Optional[np.ndarray] = None):
        super().__init__(stage)
        self.stage = stage
        self.info = dict(info or {})
        self.frame = frame


@dataclass
class FlowTestResult:
    flow_id: str
    flow_name: str
    status: str  # PASS, WARN, FAIL, STOPPED
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)
    elapsed_sec: float = 0.0
    mode: str = "scan_only"
    logs: List[str] = field(default_factory=list)


FLOW_DEFINITIONS: Tuple[Dict[str, Any], ...] = (
    {
        "id": "collect_resources",
        "name": "Thu hoạch tài nguyên (Mỏ Vàng / Dầu / Dầu đen)",
        "description": "Chạy riêng luồng thu hoạch mỏ từ đầu đến khi xong và xác minh kết quả.",
        "modes": ("real", "scan_only"),
        "default_mode": "real",
    },
    {
        "id": "check_max_storage",
        "name": "Check kho Max & Hoàn tất Farm",
        "description": "Đọc tài khoản, bấm kiểm tra popup dung lượng kho tối đa, đọc số dư và tính % kho.",
        "modes": ("real", "scan_only"),
        "default_mode": "real",
    },
    {
        "id": "upgrade_buildings",
        "name": "Check Nâng cấp công trình (Đầy đủ Setting)",
        "description": "Kiểm tra số thợ, bộ lọc, danh sách công trình, ưu tiên nâng và giả lập tới bước Confirm.",
        "modes": ("until_confirm", "scan_only", "real"),
        "default_mode": "until_confirm",
    },
    {
        "id": "upgrade_walls",
        "name": "Check Nâng tường (Làng chính)",
        "description": "Kiểm tra ngưỡng, dung lượng kho, chọn tường và giả lập tới bước Confirm.",
        "modes": ("until_confirm", "scan_only", "real"),
        "default_mode": "until_confirm",
    },
    {
        "id": "collect_treasury",
        "name": "Check Treasury (Kho bạc Clan Castle)",
        "description": "Đọc số dư kho bạc từ HUD popup, tìm Clan Castle và thu hoặc giả lập.",
        "modes": ("scan_only", "real"),
        "default_mode": "scan_only",
    },
    {
        "id": "upgrade_army",
        "name": "Check Nâng cấp quân (Phòng nghiên cứu)",
        "description": "Kiểm tra Lab rảnh, áp dụng lọc quân/phép/xe và giả lập tới nút Confirm.",
        "modes": ("until_confirm", "scan_only", "real"),
        "default_mode": "until_confirm",
    },
    {
        "id": "buy_new_buildings",
        "name": "Check Mua công trình mới (New)",
        "description": "Quét công trình New trong menu thợ, kiểm tra vùng đặt và giả lập không bấm tích xanh.",
        "modes": ("until_confirm", "scan_only", "real"),
        "default_mode": "until_confirm",
    },
    {
        "id": "assign_apprentice",
        "name": "Check Giao Phụ tá thợ xây",
        "description": "Mở menu thợ, kiểm tra Phụ tá rảnh và giả lập tới nút Confirm.",
        "modes": ("until_confirm", "scan_only", "real"),
        "default_mode": "until_confirm",
    },
    {
        "id": "main_combat",
        "name": "Combat làng chính (Tìm nhà, Soi, Thả quân)",
        "description": "Test riêng luồng đánh làng chính: mở Tìm trận, soi nhà/lọc loot, quét deck & điểm thả, hoặc đánh hoàn chỉnh 1 trận.",
        "modes": ("until_confirm", "scan_only", "real"),
        "default_mode": "until_confirm",
    },
    {
        "id": "builder_combat",
        "name": "Combat làng đêm (Viền đỏ, Stage 1 & Stage 2)",
        "description": "Test riêng luồng đánh Làng đêm: kiểm tra nút Find Now/đội quân, phân tích nền (Hang Đá/Rừng Cỏ), vẽ vùng thả an toàn & viền đỏ, hoặc đánh hoàn chỉnh Stage 1 & Stage 2.",
        "modes": ("until_confirm", "scan_only", "real"),
        "default_mode": "until_confirm",
    },
    {
        "id": "builder_walls",
        "name": "Check Nâng tường (Làng đêm)",
        "description": "Kiểm tra số dư Builder Gold/Elixir, điều kiện mở lượt, thợ rảnh, quét bảng thợ/Layout Editor, chọn nhóm tường & số lượng tới bước Confirm (KHÔNG bấm trả tiền ở giả lập).",
        "modes": ("until_confirm", "scan_only", "real"),
        "default_mode": "until_confirm",
    },
)

FLOW_BY_ID: Dict[str, Dict[str, Any]] = {item["id"]: item for item in FLOW_DEFINITIONS}

MODE_LABELS: Dict[str, str] = {
    "scan_only": "Giả lập: Chỉ quét & phân tích (không mở nâng)",
    "until_confirm": "Giả lập: Mở tới nút Confirm (KHÔNG bấm nâng)",
    "real": "Thực thi thật: Chạy từ đầu đến xong (CÓ bấm)",
}


class _DryRunADBProxy:
    """Wrap an ADBSocketClient to intercept confirm/spend taps during simulation."""

    def __init__(self, real_adb: ADBSocketClient, should_intercept: Callable[[int, int], Optional[Tuple[str, Dict[str, Any]]]]):
        self._real = real_adb
        self._should_intercept = should_intercept
        self.last_frame: Optional[np.ndarray] = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def screencap(self, *args, **kwargs):
        frame = self._real.screencap(*args, **kwargs)
        if frame is not None:
            self.last_frame = frame
        return frame

    def tap(self, x: int, y: int, *args, **kwargs):
        hit = self._should_intercept(int(x), int(y))
        if hit is not None:
            stage, info = hit
            info = dict(info)
            info["intercepted_tap"] = (int(x), int(y))
            raise DryRunReached(stage, info, frame=self.last_frame)
        return self._real.tap(x, y, *args, **kwargs)


class _DiagnosticSuggestedUpgrader(SuggestedUpgrader):
    """SuggestedUpgrader exposing parsed row diagnostics via shared _parse_menu_rows."""

    def scan_rows_with_diagnostics(self, screen: np.ndarray, words, start=50, end=795, custom_mode=False):
        return self._parse_menu_rows(screen, words, start=start, end=end, custom_mode=custom_mode)


class FlowTestRunner:
    """Executes isolated CoCAttack module flows against ADB/Vision with detailed logging."""

    RES_NAMES_VI = {
        "gold": "Vàng",
        "elixir": "Dầu hồng",
        "dark_elixir": "Dầu đen",
    }

    def __init__(
        self,
        adb: ADBSocketClient,
        vision: VisionEngine,
        settings: BotSettings,
        log_cb: Optional[Callable[[str], None]] = None,
        frame_cb: Optional[Callable[[np.ndarray, str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ):
        self.adb = adb
        self.vision = vision
        self.settings = settings
        self._external_log_cb = log_cb
        self._frame_cb = frame_cb
        self.stop_event = stop_event or threading.Event()
        self._logs: List[str] = []

        # Hook checkpoint on ADB so stopping the test aborts immediately.
        self.adb.control_callback = self._checkpoint

    def _checkpoint(self):
        if self.stop_event.is_set() or getattr(self.adb, "device_unavailable", False):
            raise BotStoppedError()

    def log(self, msg: str):
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{stamp}] {msg}"
        self._logs.append(line)
        if callable(self._external_log_cb):
            self._external_log_cb(line)

    def publish_frame(self, frame: Optional[np.ndarray], caption: str = ""):
        if frame is not None and callable(self._frame_cb):
            self._frame_cb(frame, caption)

    @staticmethod
    def fmt_duration(sec: int) -> str:
        if not isinstance(sec, int) or sec <= 0:
            return "không rõ"
        d, rem = divmod(sec, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        parts = []
        if d:
            parts.append(f"{d}d")
        if h:
            parts.append(f"{h}h")
        if m:
            parts.append(f"{m}m")
        if s and not d:
            parts.append(f"{s}s")
        return " ".join(parts) or f"{sec}s"

    @staticmethod
    def annotate_collectors(frame: np.ndarray, ready: List[Dict], full_red: List[Dict]) -> np.ndarray:
        canvas = frame.copy()
        for item in ready:
            pt = (int(item["x"]), int(item["y"]))
            cv2.circle(canvas, pt, 24, (40, 230, 90), 3)
            cv2.putText(
                canvas,
                f"{item['resource']} {item.get('score', 0):.2f}",
                (max(5, pt[0] - 40), max(20, pt[1] - 28)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (40, 230, 90),
                2,
            )
        for item in full_red:
            pt = (int(item["x"]), int(item["y"]))
            cv2.circle(canvas, pt, 24, (60, 70, 255), 3)
            cv2.putText(
                canvas,
                f"FULL:{item['resource']}",
                (max(5, pt[0] - 45), max(20, pt[1] - 28)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (60, 70, 255),
                2,
            )
        return canvas

    @staticmethod
    def annotate_point(frame: Optional[np.ndarray], point: Tuple[int, int], label: str, color=(40, 230, 90)) -> Optional[np.ndarray]:
        if frame is None:
            return None
        canvas = frame.copy()
        x, y = int(point[0]), int(point[1])
        cv2.rectangle(canvas, (max(0, x - 95), max(0, y - 35)), (min(canvas.shape[1] - 1, x + 95), min(canvas.shape[0] - 1, y + 35)), color, 3)
        cv2.drawMarker(canvas, (x, y), color, cv2.MARKER_CROSS, 24, 2)
        cv2.putText(canvas, label, (max(10, x - 110), max(28, y - 44)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        return canvas

    def _new_village(self, adb_client=None) -> Tuple[VillageStateManager, VillageManager]:
        state = VillageStateManager()
        village = VillageManager(adb_client or self.adb, self.vision, self.settings, village_state=state)
        return state, village

    def _new_builder(self, adb_client=None) -> Tuple[VillageStateManager, VillageManager, BuilderBaseManager]:
        state, village = self._new_village(adb_client=adb_client)
        builder = BuilderBaseManager(
            adb_client or self.adb,
            self.vision,
            self.settings,
            village,
            village_state=state,
        )
        return state, village, builder

    def _ensure_home_screen(self, village: VillageManager, auto_close_popups: bool = True) -> Optional[np.ndarray]:
        self._checkpoint()
        screen = village.adb.screencap()
        if screen is None:
            self.log("[ADB] ❌ Không chụp được màn hình từ giả lập.")
            return None
        self.publish_frame(screen, "Ảnh chụp ban đầu")
        if hasattr(screen, "shape") and screen.shape[:2] != (900, 1600):
            self.log(f"[Màn hình] ⚠️ Độ phân giải hiện tại là {screen.shape[1]}x{screen.shape[0]} (chuẩn là 1600x900).")
        if village.is_in_main_village(screen):
            self.log("[Màn hình] ✅ Đã xác nhận đang ở Làng chính.")
            return screen
        if auto_close_popups:
            self.log("[Màn hình] Chưa thấy Làng chính, thử đóng popup/hộp thoại đang mở...")
            for attempt in range(3):
                self._checkpoint()
                if village.close_popups_and_dialogs(screen):
                    time.sleep(0.45)
                    screen = village.adb.screencap()
                    self.publish_frame(screen, f"Sau khi đóng popup ({attempt + 1})")
                    if screen is not None and village.is_in_main_village(screen):
                        self.log("[Màn hình] ✅ Đã về Làng chính sau khi đóng popup.")
                        return screen
                else:
                    break
        self.log("[Màn hình] ❌ Màn hình hiện tại không phải Làng chính (1600x900).")
        return None

    def _cleanup_open_dialogs(self, village: VillageManager, upgrader: Optional[SuggestedUpgrader] = None):
        """Close any upgrade dialog or builder menu left open by simulation."""
        try:
            ui = upgrader.ui if upgrader is not None else WallUpgrader(village)
            for _ in range(3):
                frame = self.adb.screencap()
                if frame is None:
                    break
                if upgrader is not None:
                    up_close = upgrader.upgrade_close_button(frame)
                    if up_close:
                        self.adb.tap(up_close[0], up_close[1], jitter=0, delay_after=0.35)
                        continue
                cancel = NewBuildingBuyer.cancel_button(frame)
                if cancel:
                    self.adb.tap(cancel[0], cancel[1], jitter=0, delay_after=0.35)
                    continue
                if village.is_in_main_village(frame) and not ui.is_menu_open(frame):
                    return
                if ui.is_menu_open(frame):
                    if upgrader is not None:
                        upgrader.close_menu(frame)
                    else:
                        ui.close_dialog()
                    time.sleep(0.3)
                    continue
                close = self.vision.find_close_button(frame)
                if close:
                    self.adb.tap(close[0], close[1], jitter=0, delay_after=0.35)
                    continue
                break
        except BotStoppedError:
            raise
        except Exception as exc:
            self.log(f"[Dọn dẹp] Cảnh báo khi đóng dialog giả lập: {exc}")

    def run_flow(self, flow_id: str, mode: Optional[str] = None, options: Optional[Dict[str, Any]] = None) -> FlowTestResult:
        self._logs = []
        self.stop_event.clear()
        self.adb.control_callback = self._checkpoint
        flow_meta = FLOW_BY_ID.get(flow_id)
        if flow_meta is None:
            return FlowTestResult(flow_id=flow_id, flow_name=flow_id, status="FAIL", summary="Luồng test không tồn tại.", mode=mode or "scan_only")
        effective_mode = mode or flow_meta["default_mode"]
        opts = dict(options or {})
        started = time.monotonic()

        self.log("=" * 64)
        self.log(f"🚀 BẮT ĐẦU TEST: {flow_meta['name']}")
        self.log(f"⚙️ Chế độ: {MODE_LABELS.get(effective_mode, effective_mode)}")
        self.log("=" * 64)

        try:
            if flow_id == "collect_resources":
                res = self._run_collect_resources(effective_mode, opts)
            elif flow_id == "check_max_storage":
                res = self._run_check_max_storage(effective_mode, opts)
            elif flow_id == "upgrade_buildings":
                res = self._run_upgrade_buildings(effective_mode, opts)
            elif flow_id == "upgrade_walls":
                res = self._run_upgrade_walls(effective_mode, opts)
            elif flow_id == "collect_treasury":
                res = self._run_collect_treasury(effective_mode, opts)
            elif flow_id == "upgrade_army":
                res = self._run_upgrade_army(effective_mode, opts)
            elif flow_id == "buy_new_buildings":
                res = self._run_buy_new_buildings(effective_mode, opts)
            elif flow_id == "assign_apprentice":
                res = self._run_assign_apprentice(effective_mode, opts)
            elif flow_id == "main_combat":
                res = self._run_main_combat(effective_mode, opts)
            elif flow_id == "builder_combat":
                res = self._run_builder_combat(effective_mode, opts)
            elif flow_id == "builder_walls":
                res = self._run_builder_walls(effective_mode, opts)
            else:
                res = FlowTestResult(flow_id=flow_id, flow_name=flow_meta["name"], status="FAIL", summary="Chưa hỗ trợ luồng này.", mode=effective_mode)
        except BotStoppedError:
            elapsed = round(time.monotonic() - started, 2)
            self.log(f"⏹ Đã dừng bài test theo yêu cầu sau {elapsed:.2f}s.")
            return FlowTestResult(
                flow_id=flow_id,
                flow_name=flow_meta["name"],
                status="STOPPED",
                summary="Bài test đã bị dừng giữa chừng bởi người dùng.",
                elapsed_sec=elapsed,
                mode=effective_mode,
                logs=list(self._logs),
            )
        except Exception as exc:
            elapsed = round(time.monotonic() - started, 2)
            self.log(f"❌ Lỗi ngoại lệ khi chạy test: {type(exc).__name__}: {exc}")
            return FlowTestResult(
                flow_id=flow_id,
                flow_name=flow_meta["name"],
                status="FAIL",
                summary=f"Lỗi ngoại lệ: {type(exc).__name__}: {exc}",
                elapsed_sec=elapsed,
                mode=effective_mode,
                logs=list(self._logs),
            )

        res.elapsed_sec = round(time.monotonic() - started, 2)
        res.logs = list(self._logs)
        self.log("-" * 64)
        self.log(f"🏁 KẾT QUẢ [{res.status}] ({res.elapsed_sec:.2f}s): {res.summary}")
        self.log("=" * 64)
        return res

    # ------------------------------------------------------------------
    # 1. Thu hoạch tài nguyên
    # ------------------------------------------------------------------
    def _run_collect_resources(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        _, village = self._new_village()
        auto_popups = bool(options.get("auto_close_popups", True))
        auto_zoom = bool(options.get("auto_zoom", True))
        annotate = bool(options.get("annotate", True))

        screen = self._ensure_home_screen(village, auto_close_popups=auto_popups)
        if screen is None:
            return FlowTestResult("collect_resources", FLOW_BY_ID["collect_resources"]["name"], "FAIL", "Không ở màn hình Làng chính 1600x900.", mode=mode)

        before_res = self.vision.read_village_resources(screen)
        self.log(
            "[Số dư trước] "
            + " | ".join(
                f"{self.RES_NAMES_VI[r]}: {before_res[r]:,}" if before_res.get("valid", {}).get(r) else f"{self.RES_NAMES_VI[r]}: chưa đọc rõ"
                for r in ("gold", "elixir", "dark_elixir")
            )
        )

        # Reset cooldown so test always executes immediately
        village.last_collect_time = None
        ready, full_red = village.find_collectors(screen, include_full=True)
        self.log(f"[Quét ban đầu] Thấy {len(ready)} bong bóng sẵn sàng, {len(full_red)} bong bóng đầy kho (nền đỏ).")
        for item in ready:
            self.log(f"   • Sẵn sàng: {self.RES_NAMES_VI.get(item['resource'], item['resource'])} tại ({item['x']}, {item['y']}), score={item['score']:.3f}")
        for item in full_red:
            self.log(f"   • Đầy kho (nền đỏ): {self.RES_NAMES_VI.get(item['resource'], item['resource'])} tại ({item['x']}, {item['y']}), score={item['score']:.3f}")

        if not ready and not full_red and auto_zoom and mode == "scan_only":
            self.log("[Quét] Chưa thấy bong bóng; thử zoom out bản đồ và quét lại (giả lập vẫn an toàn).")
            self.adb.zoom_out_map(times=2)
            screen = self.adb.screencap()
            if screen is not None and village.is_in_main_village(screen):
                ready, full_red = village.find_collectors(screen, include_full=True)
                self.log(f"[Quét sau zoom] Thấy {len(ready)} bong bóng sẵn sàng, {len(full_red)} bong bóng đầy kho (nền đỏ).")

        if annotate and isinstance(screen, np.ndarray):
            self.publish_frame(self.annotate_collectors(screen, ready, full_red), "Kết quả nhận diện mỏ tài nguyên")

        ready_counts = {r: sum(1 for x in ready if x["resource"] == r) for r in ("gold", "elixir", "dark_elixir")}
        full_counts = {r: sum(1 for x in full_red if x["resource"] == r) for r in ("gold", "elixir", "dark_elixir")}

        if mode == "scan_only":
            summary = (
                f"[Giả lập] Quét thấy {len(ready)} mỏ sẵn sàng "
                f"(Vàng {ready_counts['gold']}, Dầu {ready_counts['elixir']}, Đen {ready_counts['dark_elixir']}) | "
                f"Đầy kho nền đỏ: {len(full_red)}."
            )
            return FlowTestResult(
                "collect_resources",
                FLOW_BY_ID["collect_resources"]["name"],
                "PASS",
                summary,
                details={"ready": ready_counts, "full_red": full_counts, "before_resources": before_res},
                mode=mode,
            )

        self.log("[Thu hoạch] Bắt đầu chạy VillageManager.collect_resources() từ đầu đến xong...")
        verified_count = village.collect_resources(screen, log_callback=self.log)
        after_screen = self.adb.screencap()
        after_ready, after_full = ([], [])
        after_res = {}
        if after_screen is not None and village.is_in_main_village(after_screen):
            after_ready, after_full = village.find_collectors(after_screen, include_full=True)
            after_res = self.vision.read_village_resources(after_screen)
            if annotate:
                self.publish_frame(self.annotate_collectors(after_screen, after_ready, after_full), "Sau khi thu hoạch xong")
            else:
                self.publish_frame(after_screen, "Sau khi thu hoạch xong")
            self.log(
                "[Số dư sau] "
                + " | ".join(
                    f"{self.RES_NAMES_VI[r]}: {after_res[r]:,}" if after_res.get("valid", {}).get(r) else f"{self.RES_NAMES_VI[r]}: chưa đọc rõ"
                    for r in ("gold", "elixir", "dark_elixir")
                )
            )
        else:
            self.log("[Thu hoạch] ⚠️ Màn hình sau thu hoạch không còn là Làng chính.")
            return FlowTestResult(
                "collect_resources",
                FLOW_BY_ID["collect_resources"]["name"],
                "FAIL",
                f"Thu hoạch dừng giữa chừng (đã xác minh {verified_count} mỏ nhưng màn hình rời Làng chính).",
                details={"verified_count": verified_count, "ready_before": ready_counts},
                mode=mode,
            )

        summary = (
            f"Hoàn tất thu hoạch: đã thu & xác minh {verified_count} mỏ | "
            f"Còn lại: {len(after_ready)} sẵn sàng, {len(after_full)} đầy kho (nền đỏ)."
        )
        return FlowTestResult(
            "collect_resources",
            FLOW_BY_ID["collect_resources"]["name"],
            "PASS",
            summary,
            details={
                "verified_count": verified_count,
                "ready_before": ready_counts,
                "full_before": full_counts,
                "ready_after": len(after_ready),
                "full_after": len(after_full),
                "before_resources": before_res,
                "after_resources": after_res,
            },
            mode=mode,
        )

    # ------------------------------------------------------------------
    # 2. Check kho Max & Hoàn tất Farm
    # ------------------------------------------------------------------
    def _run_check_max_storage(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        _, village = self._new_village()
        res_mode = options.get("farm_finish_resources", "gold_elixir")
        target_pct = max(90, min(100, int(options.get("farm_finish_target_percent", 100))))
        bypass_cache = bool(options.get("bypass_cache", True))
        verify_acc = bool(options.get("verify_account", True))
        verify_twice = bool(options.get("verify_twice", True))

        if res_mode == "all_three":
            resources = ("gold", "elixir", "dark_elixir")
        elif res_mode in ("gold", "elixir"):
            resources = (res_mode,)
        else:
            resources = ("gold", "elixir")

        screen = self._ensure_home_screen(village)
        if screen is None:
            return FlowTestResult("check_max_storage", FLOW_BY_ID["check_max_storage"]["name"], "FAIL", "Không ở màn hình Làng chính 1600x900.", mode=mode)

        acc_key = None
        if verify_acc:
            self.log("[Bước 1] Xác minh tài khoản (#tag / legacy header)...")
            acc_key = village.establish_account(screen, self.log)
            self.log(f"[Bước 1] Kết quả định danh tài khoản: {acc_key or 'KHÔNG XÁC MINH ĐƯỢC'}")
            screen = self.adb.screencap()
            if screen is None or not village.is_in_main_village(screen):
                return FlowTestResult("check_max_storage", FLOW_BY_ID["check_max_storage"]["name"], "FAIL", "Không quay lại được Làng chính sau khi đọc hồ sơ.", mode=mode)
        else:
            header = self.vision.read_player_profile_header(screen)
            acc_key = (header or {}).get("account_key") or "test_session_account"
            village._session_legacy_key = acc_key
            village.current_account_key = acc_key
            self.log(f"[Bước 1] Bỏ qua mở Profile; dùng mã tạm: {acc_key}")

        if bypass_cache:
            self.log("[Bước 2] Xóa bộ nhớ đệm dung lượng kho để ép đọc mới từ game...")
            if acc_key and acc_key in village.account_capacities:
                village.account_capacities.pop(acc_key, None)

        reader = ResourceCapacityReader(village)
        capacities = None
        if mode == "scan_only":
            self.log("[Bước 2] Chế độ Giả lập (không tap HUD): kiểm tra dung lượng từ bộ nhớ / TH.")
            capacities = village.get_account_capacities(resources, acc_key=acc_key)
            if capacities is None:
                th = village.get_account_th_level(acc_key)
                if th in village.TH_MAX_STORAGE_CAPACITIES:
                    capacities = {r: village.TH_MAX_STORAGE_CAPACITIES[th][r] for r in resources}
                    self.log(f"[Bước 2] Dùng bảng dung lượng theo TH {th}: {capacities}")
        else:
            self.log(f"[Bước 2] Chạy ResourceCapacityReader.read({resources}) — tap thanh tài nguyên để đọc OCR popup...")
            capacities = reader.read(resources, self.log)

        fresh = self.adb.screencap()
        if fresh is not None:
            self.publish_frame(fresh, "Màn hình làng sau khi đọc dung lượng kho")

        if capacities is None:
            return FlowTestResult(
                "check_max_storage",
                FLOW_BY_ID["check_max_storage"]["name"],
                "FAIL",
                "Không đọc được dung lượng tối đa của kho (OCR popup hoặc định danh tài khoản thất bại).",
                details={"account": acc_key, "resources": resources},
                mode=mode,
            )

        self.log("[Bước 3] Đọc số dư tài nguyên hiện tại trên HUD...")
        readings = self.vision.read_village_resources(fresh)
        valid = readings.get("valid", {})
        percentages: Dict[str, float] = {}
        missing: List[str] = []
        summary_parts: List[str] = []

        for r in resources:
            cap = capacities.get(r)
            val = readings.get(r) if valid.get(r) is True else None
            if val is None or not cap:
                summary_parts.append(f"{self.RES_NAMES_VI[r]}: lỗi đọc")
                missing.append(r)
                self.log(f"   ❌ {self.RES_NAMES_VI[r]}: số dư={val}, max={cap}")
            else:
                pct = round(val * 100.0 / cap, 2)
                percentages[r] = pct
                reached = (val * 100 >= cap * target_pct)
                mark = "✅ ĐẠT" if reached else "⏳ CHƯA ĐẠT"
                if not reached:
                    missing.append(r)
                summary_parts.append(f"{self.RES_NAMES_VI[r]} {val:,}/{cap:,} ({pct:.1f}%)")
                self.log(f"   • {self.RES_NAMES_VI[r]}: {val:,} / {cap:,} ({pct:.2f}%) -> {mark} mục tiêu {target_pct}%")

        farm_action = None
        if verify_twice and mode == "real" and res_mode != "all_three":
            self.log("[Bước 4] Kiểm tra FarmFinishController.observe_home() 2 lần liên tiếp...")
            ctrl = FarmFinishController(village)
            ctrl.capacity_reader = reader
            # Ensure test settings temporarily enable farm_finish for the controller check
            old_ff = self.settings.get("farm_finish_enabled", False)
            old_res = self.settings.get("farm_finish_resources", "gold_elixir")
            old_pct = self.settings.get("farm_finish_target_percent", 100)
            try:
                self.settings.data["farm_finish_enabled"] = True
                self.settings.data["farm_finish_resources"] = res_mode
                self.settings.data["farm_finish_target_percent"] = target_pct
                act1 = ctrl.observe_home(self.settings, fresh, self.log)
                time.sleep(0.25)
                scr2 = self.adb.screencap()
                act2 = ctrl.observe_home(self.settings, scr2, self.log)
                farm_action = (act1, act2)
                self.log(f"[Bước 4] observe_home lần 1='{act1}', lần 2='{act2}' | Status: {ctrl.status}")
            finally:
                self.settings.data["farm_finish_enabled"] = old_ff
                self.settings.data["farm_finish_resources"] = old_res
                self.settings.data["farm_finish_target_percent"] = old_pct

        all_valid = all(valid.get(r) is True for r in resources)
        if not all_valid:
            return FlowTestResult(
                "check_max_storage",
                FLOW_BY_ID["check_max_storage"]["name"],
                "FAIL",
                "Đọc được Max kho nhưng chưa đọc hợp lệ số dư hiện tại: " + " | ".join(summary_parts),
                details={"account": acc_key, "capacities": capacities, "readings": readings},
                mode=mode,
            )

        reached_all = (len(missing) == 0)
        headline = f"Đạt {target_pct}% kho" if reached_all else f"Chưa đạt {target_pct}% kho"
        return FlowTestResult(
            "check_max_storage",
            FLOW_BY_ID["check_max_storage"]["name"],
            "PASS",
            f"[{headline}] " + " | ".join(summary_parts),
            details={
                "account": acc_key,
                "capacities": capacities,
                "readings": {r: readings.get(r) for r in resources},
                "percentages": percentages,
                "target_percent": target_pct,
                "reached": reached_all,
                "missing": missing,
                "farm_action": farm_action,
            },
            mode=mode,
        )

    # ------------------------------------------------------------------
    # 3. Check Nâng cấp công trình (đầy đủ setting + giả lập)
    # ------------------------------------------------------------------
    def _log_building_settings(self, options: Dict[str, Any]):
        s = self.settings
        custom = bool(s.get("auto_upgrade_buildings", False))
        prio = s.get("building_upgrade_priority", "time_min")
        spec = s.get("building_upgrade_specific_target", "")
        wait_mode = s.get("building_upgrade_wait_mode", "target_cost")
        res_cfg = s.get("building_upgrade_resources") or {}
        cat_cfg = s.get("building_upgrade_categories") or {}
        wl = s.get("building_upgrade_whitelist") or {}
        wl_on = sum(1 for v in wl.values() if v)
        self.log("[Cấu hình Nâng công trình]")
        self.log(
            f"   • Tùy chọn nâng (auto_upgrade_buildings): {custom} | Khóa Town Hall: {s.get('lock_townhall_upgrade', True)}"
        )
        self.log(
            f"   • Số thợ duy trì: {s.get('reserve_builders', 0)} | Giả lập tường Max: {bool(options.get('walls_confirmed_maxed', False))}"
        )
        self.log(
            f"   • Ưu tiên: {prio}" + (f" (Mục tiêu: {spec})" if prio == "specific" else "")
            + f" | Khi thiếu tiền: {wait_mode}"
            + (f" ({s.get('farm_finish_target_percent', 100)}%)" if wait_mode == "full_storage" else "")
        )
        self.log(
            f"   • Bộ lọc menu: {s.get('building_upgrade_use_menu_filter', True)} | "
            f"Supercharge: {s.get('building_upgrade_allow_supercharge', True)}"
        )
        self.log(
            f"   • Tài nguyên: Vàng={res_cfg.get('gold', True)}, Dầu={res_cfg.get('elixir', True)}, Đen={res_cfg.get('dark_elixir', True)}"
        )
        self.log(
            "   • Nhóm: "
            + ", ".join(f"{k}={cat_cfg.get(k, True)}" for k in ("traps", "resource", "defense", "army", "heroes"))
            + f" | Whitelist bật: {wl_on}/{len(wl)}"
        )
        if custom:
            for cat_key, items in BUILDING_CATALOG.items():
                if not cat_cfg.get(cat_key, True):
                    continue
                enabled_in_cat = [b_k for b_k, _, _ in items if wl.get(b_k, True)]
                if not enabled_in_cat:
                    self.log(
                        f"   ⚠️ CẢNH BÁO: Nhóm '{cat_key}' đang BẬT nhưng 0/{len(items)} mục được tích trong Danh sách chi tiết (Whitelist)!"
                    )

    def _run_upgrade_buildings(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        self._log_building_settings(options)

        walls_maxed = bool(options.get("walls_confirmed_maxed", False))
        virtual_rich = bool(options.get("virtual_rich_wallet", False))
        force_scan = bool(options.get("force_scan_when_no_free", False))
        ignore_wait_block = bool(options.get("ignore_wait_mode_block", False))

        intercept_state = {"upgrader": None}

        def should_intercept(x: int, y: int):
            if mode != "until_confirm":
                return None
            up = intercept_state["upgrader"]
            if up is not None and up.pending is not None:
                return ("confirm", {"candidate": dict(up.pending), "confirm_point": (x, y)})
            return None

        adb_used = _DryRunADBProxy(self.adb, should_intercept) if mode == "until_confirm" else self.adb
        _, village = self._new_village(adb_client=adb_used)
        village.walls_confirmed_maxed = walls_maxed

        screen = self._ensure_home_screen(village)
        if screen is None:
            return FlowTestResult("upgrade_buildings", FLOW_BY_ID["upgrade_buildings"]["name"], "FAIL", "Không ở màn hình Làng chính 1600x900.", mode=mode)

        upgrader = _DiagnosticSuggestedUpgrader(village)
        intercept_state["upgrader"] = upgrader

        wallet = self.vision.read_village_resources(screen)
        self.log(
            "[Ví hiện tại] "
            + " | ".join(
                f"{self.RES_NAMES_VI[r]}: {wallet[r]:,}" if wallet.get("valid", {}).get(r) else f"{self.RES_NAMES_VI[r]}: lỗi đọc"
                for r in ("gold", "elixir", "dark_elixir")
            )
        )
        free = upgrader.observe_home_builders(screen)
        reserve = 0 if walls_maxed else int(self.settings.get("reserve_builders", 0))
        self.log(f"[Thợ HUD] Thợ thường rảnh: {free} | Mức duy trì hiệu lực: {reserve}")

        if free is None:
            return FlowTestResult("upgrade_buildings", FLOW_BY_ID["upgrade_buildings"]["name"], "FAIL", "Chưa đọc được số thợ rảnh trên HUD làng chính.", mode=mode)

        needed_builders = max(0, free - reserve)
        if needed_builders <= 0:
            if mode == "scan_only" and force_scan:
                self.log("[Thợ] ⚠️ Số thợ rảnh <= mức duy trì, nhưng bật tùy chọn 'Vẫn mở menu quét' -> giả lập 1 thợ cần giao để test bộ lọc.")
                needed_builders = 1
            else:
                return FlowTestResult(
                    "upgrade_buildings",
                    FLOW_BY_ID["upgrade_buildings"]["name"],
                    "WARN",
                    f"Không đủ thợ rảnh vượt mức duy trì ({free} <= {reserve}); bot không giao nâng công trình.",
                    details={"free_builders": free, "reserve": reserve},
                    mode=mode,
                )

        custom_mode = upgrader.custom_mode_enabled()
        wait_mode = self.settings.get("building_upgrade_wait_mode", "target_cost")
        if custom_mode and wait_mode == "full_storage":
            res_cfg = self.settings.get("building_upgrade_resources") or {"gold": True, "elixir": True}
            check_res = tuple(r for r in ("gold", "elixir") if res_cfg.get(r, True)) or ("gold", "elixir")
            target_pct = max(90, min(100, int(self.settings.get("farm_finish_target_percent", 100))))
            self.log(f"[Chế độ full_storage] Kiểm tra dung lượng kho cho {check_res} (mục tiêu {target_pct}%)...")
            if mode == "scan_only":
                caps = village.get_account_capacities(check_res)
                if caps is None:
                    th = village.get_account_th_level()
                    if th in village.TH_MAX_STORAGE_CAPACITIES:
                        caps = {r: village.TH_MAX_STORAGE_CAPACITIES[th][r] for r in check_res}
            else:
                acc = village.establish_account(screen, self.log)
                caps = ResourceCapacityReader(village).read(check_res, self.log) if acc else None
                screen = adb_used.screencap()
                if screen is not None and village.is_in_main_village(screen):
                    wallet = self.vision.read_village_resources(screen)
            if caps is None:
                msg = "Chưa đọc được dung lượng kho ở chế độ full_storage."
                if not (mode == "scan_only" and ignore_wait_block):
                    return FlowTestResult("upgrade_buildings", FLOW_BY_ID["upgrade_buildings"]["name"], "WARN", msg, mode=mode)
                self.log(f"[Chế độ full_storage] ⚠️ {msg} Tiếp tục mở menu do bật bỏ qua chặn.")
            else:
                missing_full = [
                    r for r in check_res
                    if wallet.get("valid", {}).get(r) is True and wallet.get(r, 0) * 100 < caps[r] * target_pct
                ]
                for r in check_res:
                    cur = wallet.get(r, 0)
                    pct = round(cur * 100.0 / caps[r], 1) if caps[r] else 0.0
                    self.log(f"   • {self.RES_NAMES_VI[r]}: {cur:,}/{caps[r]:,} ({pct}%)")
                if missing_full:
                    msg = f"Kho chưa đạt {target_pct}% ({', '.join(missing_full)}); luồng thật sẽ đợi farm đầy kho."
                    if not (mode == "scan_only" and ignore_wait_block):
                        return FlowTestResult(
                            "upgrade_buildings",
                            FLOW_BY_ID["upgrade_buildings"]["name"],
                            "WARN",
                            msg,
                            details={"capacities": caps, "missing": missing_full},
                            mode=mode,
                        )
                    self.log(f"[Chế độ full_storage] ⚠️ {msg} Tiếp tục mở menu do bật bỏ qua chặn.")

        # --- MODE 1: SCAN ONLY (no upgrade dialog tapped) ---
        if mode == "scan_only":
            return self._run_upgrade_buildings_scan_only(
                village, upgrader, wallet, needed_builders, custom_mode, virtual_rich, mode
            )

        # --- MODE 2: UNTIL CONFIRM (stop cleanly right before final confirm tap) ---
        if mode == "until_confirm":
            try:
                self.log("[Giả lập tới Confirm] Chạy SuggestedUpgrader.step() thật với bộ chặn Confirm...")
                result = upgrader.step(self.log, maintain_reserve=not walls_maxed)
                self._cleanup_open_dialogs(village, upgrader)
                status = result.get("status", "blocked")
                reason = result.get("reason", "")
                if status == "needs_resources":
                    return FlowTestResult(
                        "upgrade_buildings",
                        FLOW_BY_ID["upgrade_buildings"]["name"],
                        "WARN",
                        f"[Chưa đủ tiền để tới Confirm] {reason}",
                        details=result,
                        mode=mode,
                    )
                if status == "done":
                    return FlowTestResult("upgrade_buildings", FLOW_BY_ID["upgrade_buildings"]["name"], "PASS", reason, details=result, mode=mode)
                return FlowTestResult("upgrade_buildings", FLOW_BY_ID["upgrade_buildings"]["name"], "FAIL", reason, details=result, mode=mode)
            except DryRunReached as reached:
                cand = reached.info.get("candidate", {})
                pt = reached.info.get("confirm_point", (0, 0))
                upgrader.pending = None
                annotated = self.annotate_point(reached.frame, pt, f"CONFIRM: {cand.get('name', '?')}")
                if annotated is not None:
                    self.publish_frame(annotated, f"Giả lập dừng tại nút Confirm: {cand.get('name', '?')}")
                self.log(
                    f"[GIẢ LẬP] ✅ Đã mở tới Dialog Confirm & nhận diện nút xác nhận tại {pt} cho "
                    f"'{cand.get('name')}' ({cand.get('cost', 0):,} {cand.get('resource')}). "
                    "ĐÃ CHẶN CLICK — KHÔNG BẤM NÂNG CẤP!"
                )
                self._cleanup_open_dialogs(village, upgrader)
                return FlowTestResult(
                    "upgrade_buildings",
                    FLOW_BY_ID["upgrade_buildings"]["name"],
                    "PASS",
                    f"Giả lập thành công tới nút Confirm tại {pt}: {cand.get('name')} ({cand.get('cost', 0):,} {cand.get('resource')}) — KHÔNG bấm nâng.",
                    details={"candidate": cand, "confirm_point": pt},
                    mode=mode,
                )
            finally:
                upgrader.pending = None
                self._cleanup_open_dialogs(village, upgrader)

        # --- MODE 3: REAL EXECUTION ---
        self.log("[Thực thi thật] Gọi SuggestedUpgrader.step(maintain_reserve=True)...")
        result = upgrader.step(self.log, maintain_reserve=not walls_maxed)
        after = self.adb.screencap()
        if after is not None:
            self.publish_frame(after, f"Sau thực thi nâng công trình ({result.get('status')})")
        status_map = {"progress": "PASS", "done": "PASS", "needs_resources": "WARN"}
        return FlowTestResult(
            "upgrade_buildings",
            FLOW_BY_ID["upgrade_buildings"]["name"],
            status_map.get(result.get("status"), "FAIL"),
            result.get("reason", ""),
            details=result,
            mode=mode,
        )

    def _run_upgrade_buildings_scan_only(
        self,
        village: VillageManager,
        upgrader: _DiagnosticSuggestedUpgrader,
        wallet: Dict[str, Any],
        needed_builders: int,
        custom_mode: bool,
        virtual_rich: bool,
        mode: str,
    ) -> FlowTestResult:
        try:
            self.log("[Menu thợ] Đang mở menu thợ...")
            if not upgrader.ui.ensure_menu_open():
                return FlowTestResult("upgrade_buildings", FLOW_BY_ID["upgrade_buildings"]["name"], "FAIL", "Không mở được menu thợ.", mode=mode)

            scr1 = self.adb.screencap()
            scr2 = self.adb.screencap()
            free1 = upgrader.regular_builders(scr1)
            free2 = upgrader.regular_builders(scr2)
            self.log(f"[Menu thợ] Đếm thợ thường (2 ảnh): {free1}, {free2}")

            if custom_mode and self.settings.get("building_upgrade_use_menu_filter", True):
                upgrader._active_log = self.log
                try:
                    ok_filter = upgrader.apply_menu_filter_if_needed()
                finally:
                    upgrader._active_log = None
                if not ok_filter:
                    return FlowTestResult("upgrade_buildings", FLOW_BY_ID["upgrade_buildings"]["name"], "FAIL", "Chưa xác minh được bộ lọc tài nguyên/giá trong menu thợ.", mode=mode)

            screen = self.adb.screencap()
            words = upgrader.words(screen, (1170, 50, 430, 755))
            top_headers = [y for text, _, y in words if any(k in text.lower() for k in upgrader._HEADER_KEYWORDS) and y < 430]
            if top_headers:
                self.log(f"[Menu thợ] Đã ở đầu danh sách (tiêu đề tại y={min(top_headers)}).")
            else:
                prev = None
                for step_up in range(1, 7):
                    upgrader.check()
                    self.log(f"[Menu thợ] ⬆️ Chưa thấy tiêu đề đầu danh sách -> cuộn lên (lần {step_up}/6)...")
                    upgrader.ui.scroll_menu("up", fast=True)
                    screen = self.adb.screencap()
                    words = upgrader.words(screen, (1170, 50, 430, 755))
                    headers = [y for text, _, y in words if any(k in text.lower() for k in upgrader._HEADER_KEYWORDS) and y < 430]
                    sig = upgrader.ui.menu_signature(words)
                    if headers or sig == prev:
                        self.log(f"[Menu thợ] {'Đã thấy tiêu đề tại y=' + str(min(headers)) if headers else '⏹ Chạm đỉnh menu.'}")
                        break
                    prev = sig

            self.publish_frame(screen, "Menu thợ sau khi mở & áp dụng bộ lọc")
            headers = [y for text, _, y in words if any(k in text.lower() for k in upgrader._HEADER_KEYWORDS)]
            start = min(headers) if headers else 45

            ongoing = upgrader._extract_ongoing_jobs_from_words(screen, words, start) if start > 55 else []
            if ongoing:
                self.log(f"[Thợ đang xây] Phát hiện {len(ongoing)} tiến trình:")
                for job in ongoing:
                    self.log(f"   🔨 {job['name']} — còn {job['duration_str']} ({job['duration_sec']}s)")

            collected = []
            seen = set()
            all_diag = []
            priority = self.settings.get("building_upgrade_priority", "time_min") if custom_mode else "suggested"
            max_pages = 5 if custom_mode else 4

            def _body_sig(page_words, scan_start):
                body = [w for w in page_words if w[2] >= scan_start]
                return upgrader.ui.menu_signature(body if body else page_words)

            prev_sig = None
            for page in range(max_pages):
                if page > 0:
                    upgrader.check()
                    self.log(f"[Quét] ⬇️ Cuộn xuống trang {page}/{max_pages - 1}...")
                    upgrader.ui.scroll_menu("down", fast=True)
                    screen = self.adb.screencap()
                    words = upgrader.words(screen, (1170, 50, 430, 755))
                    page_headers = [y for text, _, y in words if any(k in text.lower() for k in upgrader._HEADER_KEYWORDS)]
                    start_y = min(page_headers) if page_headers else 45
                else:
                    start_y = start

                ends = [y for text, _, y in words if not custom_mode and "other upgrade" in text.lower() and y > start_y]
                end_y = min(ends) if ends else 795
                accepted, diag_rows = upgrader.scan_rows_with_diagnostics(screen, words, start=start_y, end=end_y, custom_mode=custom_mode)
                self.log(f"[Quét trang {page}] Đọc được {len(diag_rows)} dòng công trình ({len(accepted)} hợp lệ):")
                for d in diag_rows:
                    sc_tag = " ⚡Supercharge" if d.get("supercharge") else ""
                    dur_txt = self.fmt_duration(d.get("duration_sec", 0))
                    cost_txt = f"{d['cost']:,}" if d.get("cost") else "?"
                    res_txt = d.get("resource") or "?"
                    if d["allowed"]:
                        self.log(f"   ✅ {d['name']} | {cost_txt} {res_txt} | {dur_txt}{sc_tag}")
                    else:
                        self.log(f"   🚫 {d['name']} | {cost_txt} {res_txt} | {dur_txt}{sc_tag} -> Loại: {d['reject_reason']}")
                    all_diag.append(dict(d, page=page))
                if not diag_rows:
                    raw_tokens = [f"{t}@{py}" for t, _px, py in words if py >= start_y]
                    self.log(f"   [OCR thô trang {page}]: {', '.join(raw_tokens[:16]) or '(trống)'}")

                for r in accepted:
                    dedup = (upgrader.canonical_key(r["name"]), r["cost"], r["resource"])
                    if dedup not in seen:
                        seen.add(dedup)
                        collected.append(dict(r, page=page))

                sig = _body_sig(words, start_y)
                if sig == prev_sig or (not custom_mode and ends):
                    if sig == prev_sig and page > 0:
                        self.log("[Quét] ⏹ Danh sách không thay đổi sau khi cuộn xuống -> dừng quét.")
                    break
                prev_sig = sig
                if custom_mode and priority == "specific" and collected:
                    break

            if not collected:
                return FlowTestResult(
                    "upgrade_buildings",
                    FLOW_BY_ID["upgrade_buildings"]["name"],
                    "WARN",
                    f"Quét {len(all_diag)} dòng nhưng không có công trình nào khớp bộ lọc/whitelist hiện tại.",
                    details={"scanned_rows": all_diag, "ongoing": ongoing},
                    mode=mode,
                )

            ranked = upgrader.sort_candidates(collected, priority=priority)
            self.log(f"[Xếp hạng theo '{priority}'] Thứ tự ưu tiên ({len(ranked)} ứng viên):")
            for idx, c in enumerate(ranked[:10], 1):
                self.log(f"   #{idx}: {c['name']} | {c['cost']:,} {c['resource']} | {self.fmt_duration(c.get('duration_sec', 0))} (trang {c.get('page', 0)})")

            eval_wallet = (
                {"gold": 99_999_999, "elixir": 99_999_999, "dark_elixir": 9_999_999, "valid": {"gold": True, "elixir": True, "dark_elixir": True}}
                if virtual_rich else wallet
            )
            if virtual_rich:
                self.log("[Ví ảo 99M] Đang lập kế hoạch bằng ví giả lập 99,999,999 để kiểm tra phân công đa thợ.")

            sim_wallet = {
                "gold": eval_wallet.get("gold", 0) if eval_wallet.get("valid", {}).get("gold", True) else 0,
                "elixir": eval_wallet.get("elixir", 0) if eval_wallet.get("valid", {}).get("elixir", True) else 0,
                "dark_elixir": eval_wallet.get("dark_elixir", 0) if eval_wallet.get("valid", {}).get("dark_elixir", True) else 0,
            }
            plan = []
            for b_idx in range(1, needed_builders + 1):
                if b_idx - 1 >= len(ranked):
                    break
                c = ranked[b_idx - 1]
                avail = sim_wallet.get(c["resource"], 0)
                if avail >= c["cost"]:
                    sim_wallet[c["resource"]] -= c["cost"]
                    plan.append(f"[{b_idx}] {c['name']} ({c['cost']:,} {c['resource']}) ✅ Đủ")
                    self.log(f"   🔹 Thợ {b_idx}/{needed_builders}: {c['name']} ({c['cost']:,} {c['resource']}) -> ✅ Đủ tiền (còn {sim_wallet[c['resource']]:,})")
                else:
                    deficit = c["cost"] - avail
                    plan.append(f"[{b_idx}] {c['name']} ({c['cost']:,} {c['resource']}) ⏳ Thiếu {deficit:,}")
                    self.log(f"   🔸 Thợ {b_idx}/{needed_builders}: {c['name']} ({c['cost']:,} {c['resource']}) -> ⏳ Thiếu {deficit:,}")

            top = ranked[0]
            summary = f"[Giả lập quét] Tìm thấy {len(ranked)}/{len(all_diag)} mục hợp lệ. Mục tiêu #1: {top['name']} ({top['cost']:,} {top['resource']}, {self.fmt_duration(top.get('duration_sec', 0))})."
            return FlowTestResult(
                "upgrade_buildings",
                FLOW_BY_ID["upgrade_buildings"]["name"],
                "PASS",
                summary,
                details={"ranked": ranked, "scanned_rows": all_diag, "plan": plan, "ongoing": ongoing},
                mode=mode,
            )
        finally:
            self._cleanup_open_dialogs(village, upgrader)

    # ------------------------------------------------------------------
    # 4. Check Nâng tường
    # ------------------------------------------------------------------
    def _run_upgrade_walls(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        intercept_state = {"upgrader": None}

        def should_intercept(x: int, y: int):
            if mode != "until_confirm":
                return None
            w = intercept_state["upgrader"]
            if w is not None and getattr(w, "_transaction_submitted", False):
                return ("wall_confirm", {"confirm_point": (x, y)})
            return None

        adb_used = _DryRunADBProxy(self.adb, should_intercept) if mode == "until_confirm" else self.adb
        _, village = self._new_village(adb_client=adb_used)
        wall_upgrader = WallUpgrader(village)
        intercept_state["upgrader"] = wall_upgrader

        screen = self._ensure_home_screen(village)
        if screen is None:
            return FlowTestResult("upgrade_walls", FLOW_BY_ID["upgrade_walls"]["name"], "FAIL", "Không ở màn hình Làng chính 1600x900.", mode=mode)

        readings = self.vision.read_village_resources(screen)
        gold = readings.get("gold", 0) if readings.get("valid", {}).get("gold") else 0
        elixir = readings.get("elixir", 0) if readings.get("valid", {}).get("elixir") else 0
        free_builders = wall_upgrader.builders(screen)
        details = wall_upgrader.threshold_details()
        self.log(f"[Tường] Vàng: {gold:,} | Dầu: {elixir:,} | Thợ thường rảnh: {free_builders}")
        for rname, item in details.items():
            self.log(f"   • Ngưỡng {rname}: cấu hình={item['configured']:,}, hiệu lực={item['effective']:,}, max_kho={item['capacity']}")

        if mode == "scan_only":
            eligible = [r for r, item in details.items() if {"gold": gold, "elixir": elixir}.get(r, 0) >= item["effective"]]
            self.log(f"[Giả lập] Tài nguyên đạt ngưỡng nâng tường: {eligible or 'Không có'}")
            return FlowTestResult(
                "upgrade_walls",
                FLOW_BY_ID["upgrade_walls"]["name"],
                "PASS",
                f"[Giả lập] Vàng {gold:,}, Dầu {elixir:,}, Thợ rảnh {free_builders} | Đạt ngưỡng: {eligible or 'Chưa đạt'}.",
                details={"gold": gold, "elixir": elixir, "free_builders": free_builders, "thresholds": details, "eligible": eligible},
                mode=mode,
            )

        if mode == "until_confirm":
            try:
                old_auto = self.settings.get("auto_upgrade_walls", True)
                self.settings.data["auto_upgrade_walls"] = True
                ok = wall_upgrader.run_cycle(screen, gold, elixir, log_callback=self.log)
                self._cleanup_open_dialogs(village)
                status = village.wall_result.get("status", "unknown")
                reason = village.wall_status
                return FlowTestResult(
                    "upgrade_walls",
                    FLOW_BY_ID["upgrade_walls"]["name"],
                    "WARN" if not ok else "PASS",
                    f"[{status}] {reason}",
                    details=village.wall_result,
                    mode=mode,
                )
            except DryRunReached as reached:
                pt = reached.info.get("confirm_point", (0, 0))
                wall_upgrader._transaction_submitted = False
                village._pending_wall_transaction = None
                annotated = self.annotate_point(reached.frame, pt, "WALL CONFIRM (GIẢ LẬP)")
                if annotated is not None:
                    self.publish_frame(annotated, "Giả lập dừng tại nút Confirm nâng tường")
                self.log(f"[GIẢ LẬP] ✅ Đã mở tới hộp thoại Confirm nâng tường tại {pt} — KHÔNG BẤM NÂNG!")
                self._cleanup_open_dialogs(village)
                return FlowTestResult(
                    "upgrade_walls",
                    FLOW_BY_ID["upgrade_walls"]["name"],
                    "PASS",
                    f"Giả lập thành công tới nút Confirm nâng tường tại {pt} — KHÔNG bấm nâng.",
                    details={"confirm_point": pt},
                    mode=mode,
                )
            finally:
                wall_upgrader._transaction_submitted = False
                village._pending_wall_transaction = None
                self.settings.data["auto_upgrade_walls"] = old_auto
                self._cleanup_open_dialogs(village)

        ok = wall_upgrader.run_cycle(screen, gold, elixir, log_callback=self.log)
        after = self.adb.screencap()
        if after is not None:
            self.publish_frame(after, "Sau khi nâng tường")
        return FlowTestResult(
            "upgrade_walls",
            FLOW_BY_ID["upgrade_walls"]["name"],
            "PASS" if ok else "WARN",
            f"[{village.wall_result.get('status')}] {village.wall_status}",
            details=village.wall_result,
            mode=mode,
        )

    # ------------------------------------------------------------------
    # 5. Check Treasury (Clan Castle)
    # ------------------------------------------------------------------
    def _run_collect_treasury(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        _, village = self._new_village()
        bypass_cache = bool(options.get("bypass_cache", True))
        screen = self._ensure_home_screen(village)
        if screen is None:
            return FlowTestResult("collect_treasury", FLOW_BY_ID["collect_treasury"]["name"], "FAIL", "Không ở màn hình Làng chính 1600x900.", mode=mode)

        collector = TreasuryCollector(village)
        if bypass_cache:
            village._hud_treasury_cache.clear()
            village._treasury_next_check = 0.0

        if mode == "scan_only":
            self.log("[Treasury] Chế độ Giả lập: đọc số dư Treasury từ HUD popup (không mở Clan Castle)...")
            balances = collector.read_balances(self.log)
            fresh = self.adb.screencap()
            if fresh is not None:
                self.publish_frame(fresh, "Sau khi đọc HUD Treasury")
            if balances is None:
                return FlowTestResult("collect_treasury", FLOW_BY_ID["collect_treasury"]["name"], "FAIL", "Không đọc được số dư Treasury từ HUD popup.", mode=mode)
            summary = "Số dư Treasury: " + " | ".join(f"{self.RES_NAMES_VI[r]}: {balances.get(r, 0):,}" for r in ("gold", "elixir", "dark_elixir"))
            return FlowTestResult("collect_treasury", FLOW_BY_ID["collect_treasury"]["name"], "PASS", summary, details={"balances": balances}, mode=mode)

        old_enabled = self.settings.get("auto_collect_treasury", False)
        try:
            self.settings.data["auto_collect_treasury"] = True
            acted = collector.run(self.log)
            after = self.adb.screencap()
            if after is not None:
                self.publish_frame(after, "Sau khi chạy luồng Treasury")
            return FlowTestResult(
                "collect_treasury",
                FLOW_BY_ID["collect_treasury"]["name"],
                "PASS",
                f"Đã chạy xong luồng Treasury (acted={acted}, last_result={collector.memory.get('last_result', 'n/a')}).",
                details={"acted": acted, "memory": dict(collector.memory)},
                mode=mode,
            )
        finally:
            self.settings.data["auto_collect_treasury"] = old_enabled

    # ------------------------------------------------------------------
    # 6. Check Nâng quân (Lab)
    # ------------------------------------------------------------------
    def _run_upgrade_army(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        intercept_state = {"army": None}

        def should_intercept(x: int, y: int):
            if mode != "until_confirm":
                return None
            a = intercept_state["army"]
            if a is not None and a.pending is not None:
                return ("army_confirm", {"candidate": dict(a.pending), "confirm_point": (x, y)})
            return None

        adb_used = _DryRunADBProxy(self.adb, should_intercept) if mode == "until_confirm" else self.adb
        _, village = self._new_village(adb_client=adb_used)
        screen = self._ensure_home_screen(village)
        if screen is None:
            return FlowTestResult("upgrade_army", FLOW_BY_ID["upgrade_army"]["name"], "FAIL", "Không ở màn hình Làng chính 1600x900.", mode=mode)

        upgrader = SuggestedUpgrader(village)
        army = ArmyUpgrader(upgrader)
        army.log = self.log
        intercept_state["army"] = army

        if mode == "scan_only":
            try:
                if not upgrader.ui.ensure_menu_open():
                    return FlowTestResult("upgrade_army", FLOW_BY_ID["upgrade_army"]["name"], "FAIL", "Không mở được menu thợ.", mode=mode)
                adb_used.tap(1335, 850, jitter=0, delay_after=0.5)
                scr = adb_used.screencap()
                self.publish_frame(scr, "Tab Army Upgrades")
                slot, footer = army.read_research_slot(scr)
                self.log(f"[Lab] Trạng thái phòng nghiên cứu: {slot} (OCR: {footer!r})")
                if slot in ("busy", "goblin"):
                    return FlowTestResult("upgrade_army", FLOW_BY_ID["upgrade_army"]["name"], "PASS", f"Lab đang bận ({slot}).", details={"slot": slot, "footer": footer}, mode=mode)
                if slot is None:
                    return FlowTestResult("upgrade_army", FLOW_BY_ID["upgrade_army"]["name"], "FAIL", f"Chưa đọc chắc trạng thái Lab (OCR: {footer!r}).", mode=mode)
                if not army.configure_filter():
                    return FlowTestResult("upgrade_army", FLOW_BY_ID["upgrade_army"]["name"], "FAIL", "Không áp dụng được bộ lọc Army Upgrades.", mode=mode)
                scr = adb_used.screencap()
                words = upgrader.words(scr, (1170, 50, 430, 755))
                rows = army.rows(scr, words)
                self.publish_frame(scr, "Danh sách quân có thể nâng")
                for r in rows:
                    self.log(f"   • {r['name']} | {r['cost']:,} {r['resource']}")
                return FlowTestResult(
                    "upgrade_army",
                    FLOW_BY_ID["upgrade_army"]["name"],
                    "PASS" if rows else "WARN",
                    f"[Giả lập] Lab rảnh; thấy {len(rows)} mục trang đầu.",
                    details={"slot": slot, "rows": rows},
                    mode=mode,
                )
            finally:
                self._cleanup_open_dialogs(village, upgrader)

        if mode == "until_confirm":
            try:
                res = army.step()
                status_map = {"done": "PASS", "idle": "PASS", "needs_resources": "WARN"}
                return FlowTestResult(
                    "upgrade_army",
                    FLOW_BY_ID["upgrade_army"]["name"],
                    status_map.get(res.get("status"), "FAIL"),
                    res.get("reason", ""),
                    details=res,
                    mode=mode,
                )
            except DryRunReached as reached:
                cand = reached.info.get("candidate", {})
                pt = reached.info.get("confirm_point", (0, 0))
                army.pending = None
                annotated = self.annotate_point(reached.frame, pt, f"ARMY CONFIRM: {cand.get('name', '?')}")
                if annotated is not None:
                    self.publish_frame(annotated, "Giả lập dừng tại nút Confirm nâng quân")
                self.log(f"[GIẢ LẬP] ✅ Đã mở tới nút Confirm nâng quân tại {pt} cho '{cand.get('name')}' ({cand.get('cost', 0):,} {cand.get('resource')}) — KHÔNG BẤM!")
                return FlowTestResult(
                    "upgrade_army",
                    FLOW_BY_ID["upgrade_army"]["name"],
                    "PASS",
                    f"Giả lập thành công tới nút Confirm nâng quân tại {pt}: {cand.get('name')} ({cand.get('cost', 0):,} {cand.get('resource')}).",
                    details={"candidate": cand, "confirm_point": pt},
                    mode=mode,
                )
            finally:
                army.pending = None
                self._cleanup_open_dialogs(village, upgrader)

        res = army.step()
        after = self.adb.screencap()
        if after is not None:
            self.publish_frame(after, "Sau thực thi nâng quân")
        status_map = {"progress": "PASS", "idle": "PASS", "needs_resources": "WARN"}
        return FlowTestResult("upgrade_army", FLOW_BY_ID["upgrade_army"]["name"], status_map.get(res.get("status"), "FAIL"), res.get("reason", ""), details=res, mode=mode)

    # ------------------------------------------------------------------
    # 7. Check Mua công trình mới (New)
    # ------------------------------------------------------------------
    def _run_buy_new_buildings(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        intercept_state = {"buyer": None}

        def should_intercept(x: int, y: int):
            if mode != "until_confirm":
                return None
            b = intercept_state["buyer"]
            if b is not None and b.pending:
                return ("new_confirm", {"target": dict(b.target or {}), "confirm_point": (x, y)})
            return None

        adb_used = _DryRunADBProxy(self.adb, should_intercept) if mode == "until_confirm" else self.adb
        _, village = self._new_village(adb_client=adb_used)
        screen = self._ensure_home_screen(village)
        if screen is None:
            return FlowTestResult("buy_new_buildings", FLOW_BY_ID["buy_new_buildings"]["name"], "FAIL", "Không ở màn hình Làng chính 1600x900.", mode=mode)

        upgrader = SuggestedUpgrader(village)
        buyer = NewBuildingBuyer(upgrader)
        intercept_state["buyer"] = buyer
        free = upgrader.observe_home_builders(screen)
        reserve = int(self.settings.get("reserve_builders", 0))
        self.log(f"[New] Thợ rảnh: {free} | Duy trì: {reserve}")
        if free is None:
            return FlowTestResult("buy_new_buildings", FLOW_BY_ID["buy_new_buildings"]["name"], "FAIL", "Không đọc được số thợ rảnh.", mode=mode)

        if mode == "scan_only":
            try:
                if not upgrader.ui.ensure_menu_open():
                    return FlowTestResult("buy_new_buildings", FLOW_BY_ID["buy_new_buildings"]["name"], "FAIL", "Không mở được menu thợ.", mode=mode)
                wallet = self.vision.read_village_resources(screen)
                cand, has_new = buyer.seek_new(free, reserve, wallet=wallet)
                scr = adb_used.screencap()
                if scr is not None:
                    self.publish_frame(scr, "Kết quả quét công trình New")
                if cand:
                    self.log(f"[New] ✅ Phát hiện công trình mới: {cand['name']} ({cand['cost']:,} {cand['resource']})")
                    return FlowTestResult(
                        "buy_new_buildings",
                        FLOW_BY_ID["buy_new_buildings"]["name"],
                        "PASS",
                        f"[Giả lập] Tìm thấy công trình New: {cand['name']} ({cand['cost']:,} {cand['resource']}).",
                        details={"candidate": cand, "has_new": has_new},
                        mode=mode,
                    )
                return FlowTestResult(
                    "buy_new_buildings",
                    FLOW_BY_ID["buy_new_buildings"]["name"],
                    "PASS",
                    f"[Giả lập] Không thấy công trình New phù hợp (has_new={has_new}).",
                    details={"has_new": has_new},
                    mode=mode,
                )
            finally:
                self._cleanup_open_dialogs(village, upgrader)

        if mode == "until_confirm":
            try:
                res = buyer.step(self.log, free, reserve)
                status_map = {"idle": "PASS", "needs_resources": "WARN"}
                return FlowTestResult("buy_new_buildings", FLOW_BY_ID["buy_new_buildings"]["name"], status_map.get(res.get("status"), "FAIL"), res.get("reason", ""), details=res, mode=mode)
            except DryRunReached as reached:
                pt = reached.info.get("confirm_point", (0, 0))
                target = reached.info.get("target", {})
                buyer.pending = False
                annotated = self.annotate_point(reached.frame, pt, f"ĐẶT NEW: {target.get('name', '?')}")
                if annotated is not None:
                    self.publish_frame(annotated, "Giả lập dừng tại nút tích xanh đặt công trình New")
                self.log(f"[GIẢ LẬP] ✅ Đã mở tới vị trí đặt & tích xanh tại {pt} cho '{target.get('name')}' — HỦY ĐẶT!")
                return FlowTestResult(
                    "buy_new_buildings",
                    FLOW_BY_ID["buy_new_buildings"]["name"],
                    "PASS",
                    f"Giả lập thành công tới nút tích xanh đặt '{target.get('name')}' tại {pt} — Đã hủy an toàn.",
                    details={"target": target, "confirm_point": pt},
                    mode=mode,
                )
            finally:
                buyer.pending = False
                self._cleanup_open_dialogs(village, upgrader)

        res = buyer.step(self.log, free, reserve)
        after = self.adb.screencap()
        if after is not None:
            self.publish_frame(after, "Sau khi mua công trình mới")
        status_map = {"progress": "PASS", "idle": "PASS", "needs_resources": "WARN"}
        return FlowTestResult("buy_new_buildings", FLOW_BY_ID["buy_new_buildings"]["name"], status_map.get(res.get("status"), "FAIL"), res.get("reason", ""), details=res, mode=mode)

    # ------------------------------------------------------------------
    # 8. Check Phụ tá thợ xây
    # ------------------------------------------------------------------
    def _run_assign_apprentice(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        intercept_state = {"apprentice": None, "confirm_pt": None}

        def should_intercept(x: int, y: int):
            if mode != "until_confirm":
                return None
            pt = intercept_state.get("confirm_pt")
            if pt is not None and abs(x - pt[0]) <= 5 and abs(y - pt[1]) <= 5:
                return ("apprentice_confirm", {"confirm_point": (x, y)})
            return None

        adb_used = _DryRunADBProxy(self.adb, should_intercept) if mode == "until_confirm" else self.adb
        _, village = self._new_village(adb_client=adb_used)
        screen = self._ensure_home_screen(village)
        if screen is None:
            return FlowTestResult("assign_apprentice", FLOW_BY_ID["assign_apprentice"]["name"], "FAIL", "Không ở màn hình Làng chính 1600x900.", mode=mode)

        class _TestApprentice(ApprenticeAssigner):
            def confirm_button(self, scr):
                hit = super().confirm_button(scr)
                if hit is not None:
                    intercept_state["confirm_pt"] = (int(hit[0]), int(hit[1]))
                return hit

        app = _TestApprentice(village)
        intercept_state["apprentice"] = app

        if mode == "scan_only":
            try:
                if not app.ui.ensure_menu_open():
                    return FlowTestResult("assign_apprentice", FLOW_BY_ID["assign_apprentice"]["name"], "FAIL", "Không mở được menu thợ.", mode=mode)
                scr, badge = app.wait_for(app.ready_badge)
                self.publish_frame(scr or adb_used.screencap(), "Kiểm tra badge Phụ tá thợ xây")
                if badge:
                    return FlowTestResult("assign_apprentice", FLOW_BY_ID["assign_apprentice"]["name"], "PASS", f"[Giả lập] Phát hiện Phụ tá đang rảnh tại {badge}.", details={"badge": badge}, mode=mode)
                return FlowTestResult("assign_apprentice", FLOW_BY_ID["assign_apprentice"]["name"], "PASS", "[Giả lập] Không thấy Phụ tá thợ xây rảnh.", details={"badge": None}, mode=mode)
            finally:
                self._cleanup_open_dialogs(village)

        old_app = self.settings.get("auto_assign_apprentice", False)
        try:
            self.settings.data["auto_assign_apprentice"] = True
            app.next_check = 0.0
            if mode == "until_confirm":
                try:
                    app.run(screen, self.log)
                    return FlowTestResult(
                        "assign_apprentice",
                        FLOW_BY_ID["assign_apprentice"]["name"],
                        "PASS",
                        "Đã chạy kiểm tra Phụ tá (Phụ tá đang bận hoặc chưa tới bước Confirm).",
                        mode=mode,
                    )
                except DryRunReached as reached:
                    pt = reached.info.get("confirm_point", (0, 0))
                    annotated = self.annotate_point(reached.frame, pt, "APPRENTICE CONFIRM")
                    if annotated is not None:
                        self.publish_frame(annotated, "Giả lập dừng tại nút Confirm Phụ tá")
                    self.log(f"[GIẢ LẬP] ✅ Đã tới nút Xác nhận Phụ tá tại {pt} — KHÔNG BẤM!")
                    return FlowTestResult(
                        "assign_apprentice",
                        FLOW_BY_ID["assign_apprentice"]["name"],
                        "PASS",
                        f"Giả lập thành công tới nút Xác nhận Phụ tá tại {pt} — KHÔNG bấm.",
                        details={"confirm_point": pt},
                        mode=mode,
                    )
                finally:
                    self._cleanup_open_dialogs(village)

            acted = app.run(screen, self.log)
            after = self.adb.screencap()
            if after is not None:
                self.publish_frame(after, "Sau khi giao Phụ tá")
            return FlowTestResult("assign_apprentice", FLOW_BY_ID["assign_apprentice"]["name"], "PASS", f"Hoàn tất kiểm tra Phụ tá (acted={acted}).", mode=mode)
        finally:
            self.settings.data["auto_assign_apprentice"] = old_app

    # ------------------------------------------------------------------
    # 9. Combat làng chính (Tìm nhà, Soi, Thả quân)
    # ------------------------------------------------------------------
    @staticmethod
    def annotate_combat_plan(
        frame: np.ndarray,
        troop_points: List[Tuple[int, int]],
        spell_points: Optional[List[Tuple[int, int]]] = None,
        cards: Optional[Dict[str, Any]] = None,
    ) -> np.ndarray:
        canvas = frame.copy()
        for idx, pt in enumerate(troop_points):
            x, y = int(pt[0]), int(pt[1])
            cv2.circle(canvas, (x, y), 10, (40, 235, 90), 2)
            cv2.putText(canvas, str(idx + 1), (x + 8, max(16, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 235, 90), 1)
        for pt in spell_points or []:
            x, y = int(pt[0]), int(pt[1])
            cv2.drawMarker(canvas, (x, y), (255, 110, 240), cv2.MARKER_DIAMOND, 14, 2)
        if isinstance(cards, dict):
            for group, color in (("troops", (40, 235, 90)), ("spells", (255, 110, 240)), ("heroes", (60, 200, 255)), ("siege", (0, 170, 255))):
                for c in cards.get(group, []):
                    cx, cy = int(c.get("x", 0)), int(c.get("y", 0))
                    if cx > 0 and cy > 0:
                        cv2.rectangle(canvas, (max(0, cx - 28), max(0, cy - 36)), (min(canvas.shape[1] - 1, cx + 28), min(canvas.shape[0] - 1, cy + 36)), color, 2)
        return canvas

    def _safe_exit_scouting_home(self, village: VillageManager, combat: CombatExecutor) -> bool:
        """Leave enemy scouting before any deployment taps so no army/trophies are spent."""
        self.log("[Combat] Rút khỏi màn hình soi nhà (chưa thả quân) về Làng chính...")
        screen = self.adb.screencap()
        if screen is not None and village.is_in_main_village(screen):
            return True
        end = self.vision.match_template(screen, "1star", 0.80, roi=(15, 600, 230, 120))
        if not end:
            end = self.vision.match_template(screen, "surrender", 0.75, roi=(15, 600, 230, 120))
        if end:
            self.adb.tap(end[0], end[1], jitter=0, delay_after=0.6)
        else:
            combat._surrender_and_exit(self.log)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            self._checkpoint()
            frame = self.adb.screencap()
            if frame is None:
                time.sleep(0.3)
                continue
            if village.is_in_main_village(frame):
                self.publish_frame(frame, "Đã rút về Làng chính an toàn")
                self.log("[Combat] ✅ Đã quay về Làng chính an toàn (không mất quân).")
                return True
            home = combat._home_button(frame)
            if home:
                self.adb.tap(home[0], home[1], jitter=0, delay_after=1.0)
                continue
            okay = self.vision.match_template(frame, "okay", 0.75, roi=(750, 450, 450, 250))
            if okay:
                self.adb.tap(okay[0], okay[1], jitter=0, delay_after=0.5)
                continue
            time.sleep(0.35)
        self.log("[Combat] ⚠️ Hết thời gian chờ về Làng chính sau khi rút.")
        return False

    def _wait_return_home_after_battle(self, village: VillageManager, combat: CombatExecutor) -> bool:
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            self._checkpoint()
            frame = self.adb.screencap()
            if frame is None:
                time.sleep(0.4)
                continue
            if village.is_in_main_village(frame):
                self.publish_frame(frame, "Đã về Làng chính sau trận")
                return True
            home = combat._home_button(frame)
            if home:
                self.adb.tap(home[0], home[1], jitter=0, delay_after=1.2)
                continue
            if village.close_popups_and_dialogs(frame):
                continue
            time.sleep(0.45)
        return False

    def _log_combat_settings(self, ignore_loot_filter: bool):
        s = self.settings
        self.log("[Cấu hình Combat]")
        self.log(
            f"   • Chiến thuật: {s.get('attack_strategy', 'perimeter')} | "
            f"Điểm rải: {s.get('attack_spread_points', 16)} | Delay thả: {s.get('troop_drop_delay_ms', 80)}ms"
        )
        self.log(
            f"   • Lọc Loot ({s.get('loot_filter_mode', 'legacy')}): "
            f"Vàng>={s.get('min_gold', 600000):,}, Dầu>={s.get('min_elixir', 600000):,}, "
            f"Đen>={s.get('min_dark_elixir', 4000):,} | Bỏ qua lọc={ignore_loot_filter}"
        )
        self.log(
            f"   • Tướng: K={s.get('use_king', True)}, Q={s.get('use_queen', True)}, W={s.get('use_warden', True)}, "
            f"RC={s.get('use_champion', True)}, MP={s.get('use_prince', True)}, DD={s.get('use_luke', True)} | "
            f"Skill={s.get('trigger_hero_abilities', True)} ({s.get('hero_ability_delay_sec', 5)}s) | "
            f"Siege={s.get('use_siege', True)} ({s.get('siege_deploy_mode', 'always')})"
        )
        self.log(
            f"   • Kết thúc ({s.get('end_condition_mode', 'any')}): "
            f"Sao={s.get('end_on_stars', True)}({s.get('star_target', 1)}*), "
            f"Time={s.get('end_on_time', True)}({s.get('battle_timeout_sec', 120)}s), "
            f"Loot={s.get('end_on_loot', False)}({s.get('loot_target_percent', 70)}%), "
            f"Phá hủy={s.get('end_on_destruction', False)}({s.get('destruction_target_percent', 50)}%)"
        )

    def _analyze_scouting_base(self, screen: np.ndarray, combat: CombatExecutor, strategy: str) -> Dict[str, Any]:
        """Zoom out, scan deck cards, and compute planned deployment points without dropping troops."""
        if strategy == FixedAttackPlan.KEY:
            serial = getattr(self.adb, "device_serial", None) or self.settings.get("device_serial")
            profile = FixedAttackPlan.load(self.settings, serial)
            if profile is None:
                return {"error": f"Chưa có hồ sơ hiệu chuẩn Rải cố định cho thiết bị {serial}."}
            combat.zoom_out_map(profile["zoom"])
            scr = self.adb.screencap()
            cards = combat.scan_deck_cards(scr, self.log)
            blocks = FixedAttackPlan.blocks(profile, cards, early_spells=self.settings.get("spell_early_with_troops", True))
            troop_pts = [pt for b in blocks if b.get("kind") != "spells" for pt in b.get("points", [])]
            spell_pts = [pt for b in blocks if b.get("kind") == "spells" for pt in b.get("points", [])]
            return {"screen": scr, "cards": cards, "troop_points": troop_pts, "spell_points": spell_pts, "effective": strategy}

        combat.zoom_out_map(2)
        scr = self.adb.screencap()
        cards = combat.scan_deck_cards(scr, self.log)
        troops = cards.get("troops", [])
        spells = cards.get("spells", [])
        heroes = cards.get("heroes", [])
        siege = cards.get("siege", [])
        self.log(
            f"[Deck] Quân: {[(c['name'], c.get('count')) for c in troops]} | "
            f"Phép: {[(c['name'], c.get('count')) for c in spells]} | "
            f"Tướng: {[c['name'] for c in heroes]} | Siege: {[c.get('name') for c in siege]}"
        )
        point_count = int(self.settings.get("attack_spread_points", 16))
        if troops:
            combat.select_slot_pos(troops[0]["x"], troops[0]["y"])
            scr = self.adb.screencap()
        req_pts = max(32, point_count) if strategy in ("resource_dragons", "ultra_fast") else point_count
        base_points = combat.planner.points(scr, req_pts)
        if troops and not base_points:
            base_points = combat._find_drop_points_after_pan(req_pts, troops[0])
            if base_points:
                scr = self.adb.screencap()
        effective = self.settings.get("resource_attack_fallback", "perimeter") if strategy == "resource_dragons" else strategy
        if strategy == "ultra_fast":
            planned = UltraAttackPlan.perimeter_points(base_points, point_count) if hasattr(UltraAttackPlan, "perimeter_points") else base_points
        elif effective in ("perimeter", "four_corners", "smart_wave", "hybrid"):
            planned = strategy_points(base_points, "four_corners" if effective == "hybrid" else effective, 0)
        else:
            planned = base_points
        spell_pts = [UltraAttackPlan.spell_points([pt], 1, self.settings)[0] for pt in base_points[:4]] if (spells and base_points) else []
        return {"screen": scr, "cards": cards, "troop_points": planned, "spell_points": spell_pts, "base_points": base_points, "effective": effective}

    def _run_main_combat(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        ignore_loot = bool(options.get("ignore_loot_filter", False))
        return_home = bool(options.get("return_home_after_scan", True))
        self._log_combat_settings(ignore_loot)

        state, village = self._new_village()
        battle = BattleSearcher(self.adb, self.vision, self.settings)
        combat = CombatExecutor(
            self.adb,
            self.vision,
            self.settings,
            screen_callback=lambda f: self.publish_frame(f, "Live Combat"),
            village_state=state,
        )

        strategy_cfg = self.settings.get("attack_strategy", "perimeter")
        if strategy_cfg == FixedAttackPlan.KEY:
            serial = getattr(self.adb, "device_serial", None) or self.settings.get("device_serial")
            if FixedAttackPlan.load(self.settings, serial) is None:
                return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "FAIL", f"Chiến thuật Rải cố định chưa có hiệu chuẩn cho {serial}.", mode=mode)

        screen = self.adb.screencap()
        if screen is None:
            return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "FAIL", "Không chụp được màn hình từ giả lập.", mode=mode)
        self.publish_frame(screen, "Màn hình ban đầu")

        phase = self.vision.battle_phase(screen)
        is_menu, menu_btn = self.vision.is_attack_menu_open(screen)
        is_home = village.is_in_main_village(screen)
        self.log(f"[Trạng thái] phase={phase}, is_home={is_home}, attack_menu_open={is_menu}")

        if mode == "scan_only":
            if phase == "scouting":
                should_attack, loot = battle.evaluate_target(screen)
                self.log(
                    f"[Soi nhà hiện tại] Vàng={loot.get('gold', 0):,} | Dầu={loot.get('elixir', 0):,} | "
                    f"Đen={loot.get('dark_elixir', 0):,} -> Đạt ngưỡng={should_attack}"
                )
                chosen = combat.choose_attack_strategy()
                plan = self._analyze_scouting_base(screen, combat, chosen)
                if plan.get("error"):
                    return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "FAIL", plan["error"], mode=mode)
                annotated = self.annotate_combat_plan(plan["screen"], plan["troop_points"], plan["spell_points"], plan["cards"])
                self.publish_frame(annotated, f"Giả lập soi nhà: {len(plan['troop_points'])} điểm thả")
                return FlowTestResult(
                    "main_combat",
                    FLOW_BY_ID["main_combat"]["name"],
                    "PASS",
                    f"[Giả lập soi nhà] Đạt ngưỡng={should_attack}, đọc được {len(plan['cards']['troops'])} quân, "
                    f"{len(plan['cards']['spells'])} phép, {len(plan['troop_points'])} điểm thả.",
                    details={"loot": loot, "should_attack": should_attack, "troop_points": len(plan["troop_points"])},
                    mode=mode,
                )

            if not is_home and not is_menu:
                screen = self._ensure_home_screen(village)
                if screen is None:
                    return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "FAIL", "Không ở Làng chính hoặc màn hình Soi nhà.", mode=mode)
            if not battle.open_attack_menu(log_callback=self.log):
                return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "FAIL", "Không mở được menu Tấn công.", mode=mode)
            menu_scr = self.adb.screencap()
            _, btn_pos = self.vision.is_attack_menu_open(menu_scr)
            if btn_pos:
                self.publish_frame(self.annotate_point(menu_scr, btn_pos, "FIND MATCH (GIA LAP)"), "Đã mở Menu Tấn công (giả lập dừng)")
            else:
                self.publish_frame(menu_scr, "Menu Tấn công")
            return FlowTestResult(
                "main_combat",
                FLOW_BY_ID["main_combat"]["name"],
                "PASS",
                f"[Giả lập] Đã mở & xác minh Menu Tấn công (nút Tìm trận tại {btn_pos}) — KHÔNG bấm tìm trận.",
                details={"find_match_button": btn_pos},
                mode=mode,
            )

        # Both until_confirm and real may start from Home, Attack Menu, Scouting, or active Combat
        enemy_screen = screen if phase == "scouting" else None
        loot: Dict[str, Any] = {}
        should_attack = False

        if phase == "combat":
            if mode == "until_confirm":
                return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "WARN", "Trận đánh đang diễn ra sẵn trên màn hình; không thể chạy giả lập trước thả quân.", mode=mode)
            self.log("[Combat] Phát hiện trận đang diễn ra -> tiếp tục theo dõi tới khi kết thúc...")
            result = combat.monitor_existing_battle(self.log, lambda f: self.publish_frame(f, "Live Combat"))
            self._wait_return_home_after_battle(village, combat)
            return FlowTestResult(
                "main_combat",
                FLOW_BY_ID["main_combat"]["name"],
                "PASS" if result.get("completed") else "WARN",
                f"Theo dõi xong trận: {result.get('stars', 0)} sao, {result.get('duration', 0):.1f}s ({result.get('reason', '')}).",
                details=result,
                mode=mode,
            )

        if enemy_screen is None:
            if not is_home and not is_menu:
                screen = self._ensure_home_screen(village)
                if screen is None:
                    return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "FAIL", "Không ở Làng chính để mở tìm trận.", mode=mode)
            if not battle.open_attack_menu(log_callback=self.log):
                return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "FAIL", "Không mở được Menu Tấn công.", mode=mode)
            if not battle.start_find_match(log_callback=self.log):
                return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "FAIL", "Không bấm được Tìm trận.", mode=mode)

            max_skips = max(1, int(self.settings.get("max_search_skips", 50)))
            skips = 0
            while skips < max_skips:
                self._checkpoint()
                cand_scr = battle.wait_for_enemy_base(timeout_sec=40.0, log_callback=self.log)
                if cand_scr is None or not self.vision.is_enemy_base(cand_scr):
                    return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "FAIL", "Hết thời gian chờ hoặc mất màn hình căn cứ đối thủ.", mode=mode)
                self.publish_frame(cand_scr, f"Soi nhà #{skips + 1}")
                should_attack, loot = battle.evaluate_target(cand_scr)
                self.log(
                    f"[Soi Nhà #{skips + 1}] Vàng: {loot.get('gold', 0):,} | Dầu: {loot.get('elixir', 0):,} | "
                    f"Hắc dầu: {loot.get('dark_elixir', 0):,} -> Đạt ngưỡng={should_attack}"
                )
                enemy_screen = cand_scr
                if should_attack or ignore_loot:
                    if ignore_loot and not should_attack:
                        self.log("[Test] Đang bật 'Bỏ qua lọc tài nguyên' -> chọn luôn nhà này.")
                    break
                skips += 1
                if skips >= max_skips:
                    self.log(f"[Search] Đã chạm giới hạn {max_skips} lần Next.")
                    break
                if not battle.click_next(log_callback=self.log):
                    self.log("[Search] ⚠️ Chưa xác minh chuyển nhà mới sau Next.")
                    break
                time.sleep(0.8)

            if mode == "real" and not should_attack and not ignore_loot:
                self._safe_exit_scouting_home(village, combat)
                return FlowTestResult(
                    "main_combat",
                    FLOW_BY_ID["main_combat"]["name"],
                    "WARN",
                    f"Đã soi {skips} nhà nhưng chưa gặp nhà đạt ngưỡng loot -> đã rút về làng an toàn.",
                    details={"skips": skips, "last_loot": loot},
                    mode=mode,
                )
        else:
            should_attack, loot = battle.evaluate_target(enemy_screen)
            self.log(
                f"[Soi nhà hiện tại] Vàng: {loot.get('gold', 0):,} | Dầu: {loot.get('elixir', 0):,} | "
                f"Hắc dầu: {loot.get('dark_elixir', 0):,} -> Đạt ngưỡng={should_attack}"
            )

        if mode == "until_confirm":
            chosen = combat.choose_attack_strategy()
            plan = self._analyze_scouting_base(enemy_screen, combat, chosen)
            if plan.get("error"):
                if return_home:
                    self._safe_exit_scouting_home(village, combat)
                return FlowTestResult("main_combat", FLOW_BY_ID["main_combat"]["name"], "FAIL", plan["error"], mode=mode)
            annotated = self.annotate_combat_plan(plan["screen"], plan["troop_points"], plan["spell_points"], plan["cards"])
            self.publish_frame(annotated, f"Giả lập Combat ({chosen}): {len(plan['troop_points'])} điểm thả (KHÔNG thả)")
            self.log(
                f"[GIẢ LẬP] ✅ Đã lập kế hoạch thả quân chiến thuật '{chosen}': "
                f"{len(plan['cards']['troops'])} loại quân, {len(plan['cards']['spells'])} phép, "
                f"{len(plan['cards']['heroes'])} tướng, {len(plan['troop_points'])} điểm thả. KHÔNG THẢ QUÂN!"
            )
            if return_home:
                self._safe_exit_scouting_home(village, combat)
            return FlowTestResult(
                "main_combat",
                FLOW_BY_ID["main_combat"]["name"],
                "PASS",
                f"Giả lập Combat OK ({chosen}): đọc {len(plan['cards']['troops'])} quân, {len(plan['troop_points'])} điểm thả hợp lệ — KHÔNG thả quân.",
                details={"strategy": chosen, "loot": loot, "cards": plan["cards"], "troop_points": len(plan["troop_points"])},
                mode=mode,
            )

        # Real combat execution
        self.log("[Combat] ⚡ Bắt đầu thực thi trận đánh hoàn chỉnh...")
        options_attack = {"approved_scout": enemy_screen} if strategy_cfg == FixedAttackPlan.KEY else {}
        result = combat.execute_full_attack(
            log_callback=self.log,
            screen_callback=lambda f: self.publish_frame(f, "Live Combat"),
            initial_loot=loot,
            **options_attack,
        )
        self._wait_return_home_after_battle(village, combat)
        gained = result.get("loot_gained", {})
        summary = (
            f"Combat xong: {result.get('stars', 0)} sao | {result.get('duration', 0):.1f}s | "
            f"Cướp: +{gained.get('gold', 0):,} Vàng, +{gained.get('elixir', 0):,} Dầu, +{gained.get('dark_elixir', 0):,} Đen "
            f"({result.get('reason', '')})"
        )
        return FlowTestResult(
            "main_combat",
            FLOW_BY_ID["main_combat"]["name"],
            "PASS" if result.get("completed") else "WARN",
            summary,
            details=result,
            mode=mode,
        )

    # ------------------------------------------------------------------
    # 10. Combat Làng Đêm (Builder Base)
    # ------------------------------------------------------------------
    @staticmethod
    def annotate_builder_battlefield(
        frame: np.ndarray,
        safe_mask: Optional[np.ndarray],
        approach: Optional[Tuple[Any, ...]],
        cards: List[Tuple[str, Tuple[int, int]]],
        slot_statuses: List[Dict[str, Any]],
        drop_points: List[Tuple[str, Tuple[int, int]]],
    ) -> np.ndarray:
        canvas = frame.copy()
        if isinstance(safe_mask, np.ndarray) and safe_mask.shape[:2] == canvas.shape[:2]:
            overlay = canvas.copy()
            overlay[safe_mask] = (40, 220, 40)
            canvas = cv2.addWeighted(canvas, 0.72, overlay, 0.28, 0)

        if approach is not None:
            point, _, inward, _ = approach
            px, py = int(point[0]), int(point[1])
            inv = np.asarray(inward, dtype=float)
            tip = (int(round(px + 55 * inv[0])), int(round(py + 55 * inv[1])))
            cv2.circle(canvas, (px, py), 10, (255, 255, 255), -1)
            cv2.circle(canvas, (px, py), 8, (0, 140, 255), -1)
            cv2.arrowedLine(canvas, (px, py), tip, (255, 255, 0), 3)

        for idx, (kind, pt) in enumerate(drop_points):
            x, y = int(pt[0]), int(pt[1])
            cv2.circle(canvas, (x, y), 6, (40, 230, 255), -1)
            cv2.putText(canvas, f"{idx+1}:{kind[:5]}", (x + 8, max(20, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 230, 255), 1)

        for info in slot_statuses:
            cx, cy = info["card"]
            st = info["status"]
            color = (40, 230, 90) if st == "deployed" else ((40, 220, 255) if st == "waiting" else (160, 160, 160))
            cv2.rectangle(canvas, (max(0, cx - 44), max(0, cy - 68)), (min(canvas.shape[1] - 1, cx + 44), min(canvas.shape[0] - 1, cy + 56)), color, 2)
            label = f"{info['kind'][:6]}:{st}"
            cv2.putText(canvas, label, (max(2, cx - 44), max(18, cy - 72)), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1)
        return canvas

    def _log_builder_combat_settings(self, auto_switch: bool, close_menu: bool):
        s = self.settings
        self.log("[Cấu hình Combat Làng Đêm]")
        self.log(
            f"   • Combo: {s.get('builder_base_combo', 'current')} | "
            f"Tiếp viện 1: {s.get('builder_base_reinforcement_1', 'current')} | "
            f"Tiếp viện 2: {s.get('builder_base_reinforcement_2', 'current')}"
        )
        self.log(
            f"   • Vẫn bấm Attack khi chướng ngại={bool(s.get('builder_base_obstacle_attack', False))} | "
            f"Tự sang Làng Đêm={auto_switch} | Đóng menu sau quét={close_menu}"
        )

    def _detect_builder_stage(self, screen: np.ndarray, builder: BuilderBaseManager) -> int:
        words = builder.walls.texts(screen, (530, 5, 540, 110), scale=1)
        title = "".join(ch for ch in "".join(words).lower() if ch.isalnum())
        if "stage2" in title:
            return 2
        if builder.match(screen, ("1starbuilder_white",), (1200, 550, 395, 185), .82):
            return 2
        return 1

    def _analyze_builder_battlefield(
        self,
        screen: np.ndarray,
        builder: BuilderBaseManager,
        allow_reveal: bool = False,
    ) -> Dict[str, Any]:
        battle = builder.battle
        stage = self._detect_builder_stage(screen, builder)
        battle.stage = stage
        battle.rejected_entries = []
        battle.safe_mask = None
        threshold = .72 if stage == 2 else .80
        cards = battle.cards(screen, threshold=threshold)
        approach = battle.entry(screen)
        source = "viền_đỏ" if approach else None
        if not approach and allow_reveal:
            fresh = battle.reveal_boundary(screen, cards=cards)
            if fresh is not None and battle.phase(fresh) in ("scouting", "combat"):
                screen = fresh
                approach = battle.entry(screen)
                if approach:
                    source = "reveal_viền_đỏ"
        if not approach:
            approach = battle.terrain_entry(screen)
            if approach:
                source = "địa_hình"

        slot_statuses: List[Dict[str, Any]] = []
        drop_points: List[Tuple[str, Tuple[int, int]]] = []
        p = d = inv = None
        if approach is not None:
            point, direction, inward, _ = approach
            p = np.asarray(point, dtype=float)
            d = np.asarray(direction, dtype=float)
            inv = np.asarray(inward, dtype=float)

        for idx, (kind, card) in enumerate(cards):
            troop = Troop(kind, card, 0.0)
            count = None if kind in ("machine", "copter") else battle.stack_count(screen, card)
            if count is not None:
                status = "0x" if count == 0 else f"{count}x"
                waiting = count > 0
            else:
                state = battle.slot_state(screen, troop)
                if stage == 2 and state == "deployed":
                    status = "survivor_waiting"
                    waiting = True
                else:
                    status = state
                    waiting = state == "waiting"
            slot_statuses.append({"kind": kind, "card": card, "status": status, "count": count})
            if waiting and p is not None:
                spacing = (0, -15, 15, -25, 25)[idx % 5]
                depth = 9 if kind in battle.FRONTLINE else 17
                raw_drop = tuple(int(round(v)) for v in p + spacing * d - depth * inv)
                drop_pt = battle.resolve_drop_point(raw_drop, p, d, inv, depth=depth)
                drop_points.append((kind, drop_pt))

        threats = battle.detect_anti_bat_threats(screen) if hasattr(battle, "detect_anti_bat_threats") else {"air_bombs": [], "roasters": []}
        info = getattr(battle, "last_battlefield_info", None) or {}
        return {
            "screen": screen,
            "stage": stage,
            "is_cave": info.get("is_cave"),
            "segments": info.get("segments", 0),
            "source": source,
            "approach": approach,
            "cards": cards,
            "slot_statuses": slot_statuses,
            "drop_points": drop_points,
            "safe_mask": battle.safe_mask,
            "threats": threats,
        }

    def _run_builder_combat(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        auto_switch = bool(options.get("auto_switch_to_builder", True))
        close_menu = bool(options.get("close_menu_after_scan", True))
        self._log_builder_combat_settings(auto_switch, close_menu)

        _, village, builder = self._new_builder()
        battle = builder.battle

        screen = self.adb.screencap()
        if screen is None:
            return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Không chụp được màn hình từ giả lập.", mode=mode)
        self.publish_frame(screen, "Màn hình ban đầu")

        phase = battle.phase(screen)
        menu_btn = battle.find_now(screen)
        is_bb_home = builder.is_home(screen)
        is_main_home = False if (phase or is_bb_home or menu_btn) else village.is_in_main_village(screen)
        self.log(f"[Trạng thái BH] phase={phase}, is_bb_home={is_bb_home}, find_now_open={bool(menu_btn)}, is_main_home={is_main_home}")

        if not phase and not is_bb_home and not menu_btn and is_main_home and auto_switch:
            self.log("[BH] Đang ở Làng chính -> bấm thuyền sang Làng Đêm...")
            if not builder.enter(screen):
                return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Không chuyển được từ Làng chính sang Làng Đêm.", mode=mode)
            screen = self.adb.screencap()
            if screen is None:
                return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Mất khung hình sau khi sang Làng Đêm.", mode=mode)
            is_bb_home = builder.is_home(screen)
            self.publish_frame(screen, "Đã sang Làng Đêm")

        if mode == "scan_only":
            if phase in ("scouting", "combat"):
                plan = self._analyze_builder_battlefield(screen, builder, allow_reveal=False)
                annotated = self.annotate_builder_battlefield(
                    plan["screen"],
                    plan["safe_mask"],
                    plan["approach"],
                    plan["cards"],
                    plan["slot_statuses"],
                    plan["drop_points"],
                )
                terrain = "Hang Đá" if plan["is_cave"] else "Rừng Cỏ"
                self.publish_frame(annotated, f"BH Stage {plan['stage']} ({terrain}): {len(plan['drop_points'])} điểm thả")
                self.log(
                    f"[Soi trận BH] Stage {plan['stage']} | Nền={terrain} | Đoạn đỏ={plan['segments']} | "
                    f"Nguồn={plan['source']} | Thẻ={[(s['kind'], s['status']) for s in plan['slot_statuses']]}"
                )
                if plan["approach"] is None:
                    return FlowTestResult(
                        "builder_combat",
                        FLOW_BY_ID["builder_combat"]["name"],
                        "WARN",
                        f"[Giả lập Stage {plan['stage']}] Đọc được {len(plan['cards'])} ô quân nhưng chưa xác định được hướng thả ({terrain}).",
                        details={"stage": plan["stage"], "terrain": terrain, "cards": len(plan["cards"]), "slots": plan["slot_statuses"]},
                        mode=mode,
                    )
                return FlowTestResult(
                    "builder_combat",
                    FLOW_BY_ID["builder_combat"]["name"],
                    "PASS",
                    f"[Giả lập Stage {plan['stage']}] Nền={terrain}, nguồn={plan['source']} ({plan['segments']} đoạn đỏ), "
                    f"đọc {len(plan['cards'])} ô quân, vẽ {len(plan['drop_points'])} điểm thả an toàn — KHÔNG thả.",
                    details={"stage": plan["stage"], "terrain": terrain, "source": plan["source"], "segments": plan["segments"], "slots": plan["slot_statuses"], "drop_points": len(plan["drop_points"])},
                    mode=mode,
                )

            if not menu_btn:
                if not is_bb_home:
                    return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Không ở Làng Đêm, menu Find Now hoặc trận đánh Làng Đêm.", mode=mode)
                menu_scr = battle.open_menu(screen)
                if menu_scr is None:
                    return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Không mở được menu Tìm trận Builder Base (Find Now).", mode=mode)
                screen = menu_scr
                menu_btn = battle.find_now(screen)
            request = battle.army.request()
            uses_current = battle.army.uses_current_lineup(request)
            self.log(f"[Menu BH] Find Now={menu_btn[:2] if menu_btn else None} | Request={request} | Dùng đội hình hiện tại={uses_current}")
            if menu_btn:
                self.publish_frame(self.annotate_point(screen, menu_btn[:2], "FIND NOW (GIA LAP)"), "Menu Tìm trận Làng Đêm (giả lập dừng)")
            else:
                self.publish_frame(screen, "Menu Làng Đêm")
            if close_menu:
                builder.close_panel(screen)
            return FlowTestResult(
                "builder_combat",
                FLOW_BY_ID["builder_combat"]["name"],
                "PASS",
                f"[Giả lập] Đã mở & xác minh Menu Làng Đêm (Find Now tại {menu_btn[:2] if menu_btn else None}, combo={request[0]}) — KHÔNG bấm tìm trận.",
                details={"find_now": menu_btn[:2] if menu_btn else None, "request": request},
                mode=mode,
            )

        if mode == "until_confirm":
            if phase not in ("scouting", "combat"):
                if not menu_btn:
                    if not is_bb_home:
                        return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Không ở Làng Đêm hoặc trận đánh Làng Đêm.", mode=mode)
                    menu_scr = battle.open_menu(screen)
                    if menu_scr is None:
                        return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Không mở được menu Find Now.", mode=mode)
                    screen = menu_scr
                request = battle.army.request()
                if not battle.army.uses_current_lineup(request):
                    self.log(f"[Đội hình BH] Đang chuẩn bị đội hình {request}...")
                    screen = battle.army.prepare(screen, *request, self.log)
                    if screen is None:
                        return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Không chuẩn bị được đội hình Làng Đêm.", mode=mode)
                btn = battle.find_now(screen)
                if not btn:
                    return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Không thấy nút Find Now xanh.", mode=mode)
                builder.tap(btn, 1.0)
                self.log("[BH] Đã bấm Find Now, chờ vào căn cứ đối thủ để phân tích...")
                matched = builder.wait_for(lambda f: battle.phase(f) in ("scouting", "combat"), 45)
                if matched is None:
                    return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Hết 45s chờ tìm trận Làng Đêm.", mode=mode)
                self.log("[BH] Đã tìm thấy căn cứ đối thủ. Đợi 3s load intro rồi zoom out...")
                time.sleep(3.0)
                self.adb.zoom_out_map(times=2)
                time.sleep(0.35)
                screen = self.adb.screencap()
                if screen is None:
                    return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Mất khung hình sau zoom out.", mode=mode)

            plan = self._analyze_builder_battlefield(screen, builder, allow_reveal=True)
            annotated = self.annotate_builder_battlefield(
                plan["screen"],
                plan["safe_mask"],
                plan["approach"],
                plan["cards"],
                plan["slot_statuses"],
                plan["drop_points"],
            )
            terrain = "Hang Đá" if plan["is_cave"] else "Rừng Cỏ"
            self.publish_frame(annotated, f"Giả lập BH Stage {plan['stage']} ({terrain}): {len(plan['drop_points'])} điểm (KHÔNG thả)")
            self.log(
                f"[GIẢ LẬP BH] Stage {plan['stage']} | Nền={terrain} | Nguồn={plan['source']} | "
                f"Đoạn đỏ={plan['segments']} | Thẻ={[(s['kind'], s['status']) for s in plan['slot_statuses']]} | "
                f"Điểm thả={plan['drop_points']}"
            )
            if plan["approach"] is None:
                return FlowTestResult(
                    "builder_combat",
                    FLOW_BY_ID["builder_combat"]["name"],
                    "FAIL",
                    f"Chưa xác định được hướng thả quân ở Stage {plan['stage']} ({terrain}).",
                    details={"stage": plan["stage"], "terrain": terrain, "slots": plan["slot_statuses"]},
                    mode=mode,
                )
            return FlowTestResult(
                "builder_combat",
                FLOW_BY_ID["builder_combat"]["name"],
                "PASS",
                f"Giả lập BH Stage {plan['stage']} OK ({terrain}, {plan['source']}): đọc {len(plan['cards'])} ô quân, "
                f"{len(plan['drop_points'])} điểm thả hợp lệ — KHÔNG thả quân.",
                details={"stage": plan["stage"], "terrain": terrain, "source": plan["source"], "segments": plan["segments"], "slots": plan["slot_statuses"], "drop_points": len(plan["drop_points"])},
                mode=mode,
            )

        # Real execution (Stage 1 & Stage 2)
        orig_screencap = self.adb.screencap
        last_pub = [0.0]

        def _live_screencap(*args, **kwargs):
            frame = orig_screencap(*args, **kwargs)
            now = time.monotonic()
            if frame is not None and now - last_pub[0] >= 0.8:
                last_pub[0] = now
                self.publish_frame(frame, f"Live Combat Làng Đêm (Stage {max(1, battle.stage)})")
            return frame

        self.adb.screencap = _live_screencap
        try:
            if phase in ("scouting", "combat"):
                self.log("[BH] Phát hiện trận Làng Đêm đang mở -> bám sát điều khiển tới khi kết thúc...")
                battle.stage = self._detect_builder_stage(screen, builder)
                battle.in_progress = True
                battle.match_requested = time.monotonic()
                ok = battle.monitor(self.log)
            else:
                if not is_bb_home and not menu_btn:
                    return FlowTestResult("builder_combat", FLOW_BY_ID["builder_combat"]["name"], "FAIL", "Không ở Làng Đêm để bắt đầu trận đánh.", mode=mode)
                ok = battle.start(screen, self.log)
        finally:
            self.adb.screencap = orig_screencap

        final = self.adb.screencap()
        if final is not None:
            self.publish_frame(final, "Sau trận Làng Đêm")
        return FlowTestResult(
            "builder_combat",
            FLOW_BY_ID["builder_combat"]["name"],
            "PASS" if ok else "WARN",
            f"Hoàn thành trận Làng Đêm (tới Stage {battle.stage})." if ok else f"Trận Làng Đêm dừng/chưa xác nhận về làng (Stage {battle.stage}).",
            details={"completed": ok, "stage_reached": battle.stage},
            mode=mode,
        )

    # ------------------------------------------------------------------
    # 11. Check Nâng tường Làng Đêm (Builder Base)
    # ------------------------------------------------------------------
    def _cleanup_builder_dialogs(self, builder: BuilderBaseManager):
        """Close any open Builder Base wall confirmation dialog / builder menu / selection."""
        try:
            for _ in range(3):
                self._checkpoint()
                frame = builder.adb.screencap()
                if frame is None:
                    break
                if self.vision.has_open_dialog(frame) or builder.walls.is_menu_open(frame):
                    if not builder.close_panel(frame):
                        builder.adb.keyevent(4)
                    time.sleep(0.3)
                    continue
                if builder.is_home(frame):
                    builder.deselect()
                    break
                builder.adb.keyevent(4)
                time.sleep(0.3)
        except BotStoppedError:
            raise
        except Exception as exc:
            self.log(f"[BH][Dọn dẹp] Cảnh báo khi đóng hộp thoại Làng Đêm: {exc}")

    def _run_builder_walls(self, mode: str, options: Dict[str, Any]) -> FlowTestResult:
        auto_switch = bool(options.get("auto_switch_to_builder", True))
        scan_layout = bool(options.get("scan_layout_editor", False))
        bypass_min = bool(options.get("bypass_min_threshold", False)) and mode != "real"
        force_verify = bool(options.get("force_verify_walls", True))

        s = self.settings
        trigger = s.get("builder_base_wall_trigger", "either")
        resource_cfg = s.get("builder_base_wall_resource", "both")
        lowest_first = bool(s.get("builder_base_wall_lowest_first", False))
        min_gold = int(s.get("builder_base_wall_min_gold", 0))
        min_elixir = int(s.get("builder_base_wall_min_elixir", 0))
        gap_ms = int(s.get("wall_quantity_fixed_gap_ms", 120))
        self.log("[Cấu hình Nâng tường Làng Đêm]")
        self.log(
            f"   • điều_kiện={trigger} | tài_nguyên={resource_cfg} | ưu_tiên_cấp_thấp={lowest_first} | "
            f"ngưỡng_vàng={min_gold:,} | ngưỡng_dầu={min_elixir:,} | gap={gap_ms}ms"
        )
        self.log(
            f"   • Tự sang Làng Đêm={auto_switch} | Quét Layout Editor={scan_layout} | "
            f"Bỏ qua ngưỡng tối thiểu={bypass_min} | Xóa cache quét lại={force_verify}"
        )

        intercept_state: Dict[str, Any] = {"builder": None}

        def should_intercept(x: int, y: int):
            if mode != "until_confirm":
                return None
            b = intercept_state["builder"]
            if b is not None and getattr(b, "_wall_payment_armed", False):
                pending = dict(getattr(b, "_pending_wall_confirm", None) or {})
                pending["confirm_point"] = (int(x), int(y))
                return ("builder_wall_confirm", pending)
            return None

        adb_used = _DryRunADBProxy(self.adb, should_intercept) if mode == "until_confirm" else self.adb
        _, village, builder = self._new_builder(adb_client=adb_used)
        intercept_state["builder"] = builder
        if force_verify:
            builder._wall_verified_groups = {}
            builder._wall_verified_signature = None
            builder._wall_verified_at = 0
            builder._elixir_wall_unavailable_at = 0
            builder.walls_confirmed_maxed = False
            builder.consecutive_no_wall_count = 0

        screen = adb_used.screencap()
        if screen is None:
            return FlowTestResult("builder_walls", FLOW_BY_ID["builder_walls"]["name"], "FAIL", "Không chụp được màn hình từ giả lập.", mode=mode)
        self.publish_frame(screen, "Màn hình ban đầu")

        is_bb_home = builder.is_home(screen)
        is_main_home = False if is_bb_home else village.is_in_main_village(screen)
        self.log(f"[Trạng thái BH] is_bb_home={is_bb_home}, is_main_home={is_main_home}")
        if not is_bb_home and is_main_home and auto_switch:
            self.log("[BH] Đang ở Làng chính -> bấm thuyền sang Làng Đêm...")
            if not builder.enter(screen):
                return FlowTestResult("builder_walls", FLOW_BY_ID["builder_walls"]["name"], "FAIL", "Không chuyển được từ Làng chính sang Làng Đêm.", mode=mode)
            screen = adb_used.screencap()
            if screen is None or not builder.is_home(screen):
                return FlowTestResult("builder_walls", FLOW_BY_ID["builder_walls"]["name"], "FAIL", "Mất màn hình Làng Đêm sau khi chuyển làng.", mode=mode)
            is_bb_home = True
            self.publish_frame(screen, "Đã sang Làng Đêm")
        if not is_bb_home:
            return FlowTestResult("builder_walls", FLOW_BY_ID["builder_walls"]["name"], "FAIL", "Không ở màn hình chính Làng Đêm 1600x900.", mode=mode)

        layout_ok = None
        if scan_layout:
            self.log("[BH] Bắt đầu quét tường 2 Stage qua Layout Editor...")
            layout_ok = builder.scan_initial_layout_walls(self.log)
            screen = adb_used.screencap()
            if screen is not None:
                self.publish_frame(screen, "Sau khi quét Layout Editor Làng Đêm")
            if screen is None or not builder.is_home(screen):
                return FlowTestResult("builder_walls", FLOW_BY_ID["builder_walls"]["name"], "FAIL", "Không xác nhận về được Làng Đêm sau Layout Editor.", mode=mode)

        old_cfg = {
            k: s.data.get(k)
            for k in (
                "builder_base_enabled",
                "builder_base_upgrade_walls",
                "builder_base_wall_min_gold",
                "builder_base_wall_min_elixir",
                "builder_base_pause_on_no_upgrades",
            )
            if k in s.data
        }
        try:
            s.data["builder_base_enabled"] = True
            s.data["builder_base_upgrade_walls"] = True
            s.data["builder_base_pause_on_no_upgrades"] = False
            if bypass_min:
                s.data["builder_base_wall_min_gold"] = 0
                s.data["builder_base_wall_min_elixir"] = 0

            raw_res = self.vision.read_village_resources(screen, builder=True)
            balances = builder.wall_balances(screen)
            eligible = sorted(builder.wall_opening_resources(balances))
            free_builders = builder.confirmed_free_builders(screen)
            gold_val = raw_res.get("gold") if raw_res.get("valid", {}).get("gold") else None
            elixir_val = raw_res.get("elixir") if raw_res.get("valid", {}).get("elixir") else None
            self.log(
                f"[Tường BH] Gold={f'{gold_val:,}' if type(gold_val) is int else '?'} | "
                f"Elixir={f'{elixir_val:,}' if type(elixir_val) is int else '?'} | "
                f"Thợ rảnh BH={free_builders} | Đạt điều kiện={eligible or 'Không'}"
            )

            if mode == "scan_only":
                return FlowTestResult(
                    "builder_walls",
                    FLOW_BY_ID["builder_walls"]["name"],
                    "PASS" if free_builders != 0 else "WARN",
                    (
                        f"[Giả lập BH] Gold={f'{gold_val:,}' if type(gold_val) is int else '?'}, "
                        f"Elixir={f'{elixir_val:,}' if type(elixir_val) is int else '?'}, "
                        f"Thợ rảnh={free_builders}, Đạt điều kiện={eligible or 'Chưa đạt'}"
                        + (f", LayoutEditor={'OK' if layout_ok else 'Chưa đủ'}" if scan_layout else "")
                        + "."
                    ),
                    details={
                        "gold": gold_val,
                        "elixir": elixir_val,
                        "free_builders": free_builders,
                        "eligible": eligible,
                        "layout_ok": layout_ok,
                    },
                    mode=mode,
                )

            if mode == "until_confirm":
                try:
                    result = builder.upgrade_wall_cycle(screen, self.log)
                    self._cleanup_builder_dialogs(builder)
                    if result is True:
                        msg = "Đã hoàn thành kiểm tra (không phát sinh giao dịch)."
                    elif builder.walls_confirmed_maxed:
                        msg = "Bảng thợ báo đã Max toàn bộ nâng cấp Làng Đêm."
                    elif not eligible:
                        msg = f"Chưa mở lượt nâng tường: tài nguyên chưa đạt điều kiện ({trigger})."
                    elif free_builders == 0:
                        msg = "Đã hết thợ xây Làng Đêm rảnh (0 thợ)."
                    else:
                        msg = "Chưa tới bước hộp thoại Confirm tường Làng Đêm (xem log chi tiết để biết bước dừng)."
                    return FlowTestResult(
                        "builder_walls",
                        FLOW_BY_ID["builder_walls"]["name"],
                        "WARN",
                        msg,
                        details={"result": result, "eligible": eligible, "free_builders": free_builders, "maxed": builder.walls_confirmed_maxed},
                        mode=mode,
                    )
                except DryRunReached as reached:
                    builder._wall_payment_armed = False
                    pt = reached.info.get("confirm_point", (0, 0))
                    lvl = reached.info.get("level", "?")
                    target_lvl = reached.info.get("target_level", "?")
                    qty = reached.info.get("quantity", 1)
                    price = reached.info.get("price", 0)
                    res_name = reached.info.get("resource", "?")
                    label = f"BH WALL CONFIRM ({qty}x Lv{lvl}->{target_lvl})"
                    annotated = self.annotate_point(reached.frame, pt, label)
                    if annotated is not None:
                        self.publish_frame(annotated, f"Giả lập dừng tại nút Confirm nâng {qty} đoạn tường BH cấp {lvl}->{target_lvl}")
                    self.log(
                        f"[GIẢ LẬP BH] ✅ Đã mở tới hộp thoại Confirm nâng {qty} đoạn tường cấp {lvl}→{target_lvl}, "
                        f"giá {price:,} {res_name} tại {pt} — KHÔNG BẤM TRẢ TIỀN!"
                    )
                    self._cleanup_builder_dialogs(builder)
                    return FlowTestResult(
                        "builder_walls",
                        FLOW_BY_ID["builder_walls"]["name"],
                        "PASS",
                        f"Giả lập OK tới nút Confirm {qty} đoạn cấp {lvl}→{target_lvl} ({price:,} {res_name}) tại {pt} — KHÔNG bấm nâng.",
                        details=dict(reached.info),
                        mode=mode,
                    )

            # Real execution
            ok = builder.upgrade_wall_cycle(screen, self.log)
            after = self.adb.screencap()
            if after is not None:
                self.publish_frame(after, "Sau khi chạy nâng tường Làng Đêm")
            return FlowTestResult(
                "builder_walls",
                FLOW_BY_ID["builder_walls"]["name"],
                "PASS" if ok else "WARN",
                "Đã hoàn thành chu trình nâng tường Làng Đêm." if ok else "Chu trình nâng tường Làng Đêm kết thúc chưa nâng thêm (xem log chi tiết).",
                details={"upgraded": ok, "walls_confirmed_maxed": builder.walls_confirmed_maxed},
                mode=mode,
            )
        finally:
            builder._wall_payment_armed = False
            for k in (
                "builder_base_enabled",
                "builder_base_upgrade_walls",
                "builder_base_wall_min_gold",
                "builder_base_wall_min_elixir",
                "builder_base_pause_on_no_upgrades",
            ):
                if k in old_cfg:
                    s.data[k] = old_cfg[k]
                else:
                    s.data.pop(k, None)
