import time
import threading
import re
import cv2
from contextlib import nullcontext
import numpy as np
from typing import Optional, Callable, Dict, Any
from core.adb_client import ADBSocketClient, BotStoppedError
from core.fixed_attack import FixedAttackPlan
from core.vision import VisionEngine
from core.mumu_optimizer import MuMuOptimizer
from config.settings import BotSettings
from bot.village import VillageManager
from bot.wall_guard import WallFarmGuard
from bot.farm_finish import FarmFinishController
from bot.battle import BattleSearcher
from bot.combat import CombatExecutor
from bot.builder_base import BuilderBaseManager
from core.stats_store import StatsStore
from core.village_state import VillageStateManager
from pathlib import Path

class BotEngine:
    """
    Hệ thống điều khiển trạng thái tự động (Autonomous State Machine).
    - Tự động nhận diện chính xác trạng thái màn hình hiện tại (Làng chính, Menu Tấn công, Mây trôi, Soi nhà, Trận đánh)
    - Tự động khởi động lại game (Auto-Restart Clash of Clans) khi bị kẹt/lỗi/mất kết nối
    - Xác minh từng bước chặt chẽ, loại bỏ hoàn toàn các click mù
    """

    STATE_IDLE = "IDLE"
    STATE_VILLAGE = "VILLAGE"
    STATE_ATTACK_MENU = "ATTACK_MENU"
    STATE_SEARCHING = "SEARCHING"
    STATE_COMBAT = "COMBAT"
    STATE_BUILDER = "BUILDER_BASE"
    STATE_RECOVERY = "RECOVERY"
    STATE_WALL_HOLD = "Giữ làng — chờ nâng tường"

    def __init__(
        self,
        settings: BotSettings,
        log_callback: Optional[Callable[[str], None]] = None,
        screen_callback: Optional[Callable[[np.ndarray], None]] = None,
        stats_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        state_callback: Optional[Callable[[str], None]] = None,
        stats_path: Optional[str] = None
    ):
        self._thread: Optional[threading.Thread] = None
        self._lifecycle_lock = threading.RLock()
        self._start_cancelled = threading.Event()
        self.settings = settings
        self.log_cb = log_callback or (lambda msg: print(f"[BotEngine] {msg}"))
        self.screen_cb = screen_callback
        self.stats_cb = stats_callback
        self.state_cb = state_callback

        self.adb = ADBSocketClient()
        self.adb.control_callback = self._checkpoint
        self.vision = VisionEngine()
        self.optimizer = MuMuOptimizer(self.adb)

        self.village_state = VillageStateManager()
        self.village = VillageManager(self.adb, self.vision, self.settings, village_state=self.village_state)
        self._wall_farm_guard = WallFarmGuard()
        self.farm_finish = FarmFinishController(self.village)
        self.completion_callback = None
        self.battle = BattleSearcher(self.adb, self.vision, self.settings)
        self.battle.bonus_handler=self.village.handle_star_bonus
        self.battle.fault_check = self._queue_screen_fault
        self.combat = CombatExecutor(self.adb, self.vision, self.settings, screen_callback=self.screen_cb, village_state=self.village_state)
        self.builder = BuilderBaseManager(self.adb, self.vision, self.settings, self.village,
                                          battle_stat=self._record_builder_battle, village_state=self.village_state,
                                          pause_callback=self.pause_bot)
        self.builder.engine = self
        self.village.builder = self.builder

        self.is_running = False
        self.is_paused = False
        self.current_state = self.STATE_IDLE
        self.last_village_maintenance = 0.0

        # Runtime metrics
        self.stats = {
            "battles_completed": 0,
            "stars_earned": 0,
            "gold_looted": 0,
            "elixir_looted": 0,
            "dark_looted": 0,
            "searches_skipped": 0,
            "bases_scanned": 0,
            "battles_started": 0,
            "battle_duration_ms": 0,
            "battle_duration_samples": 0,
            "fastest_50_ms": 0,
            "uptime_seconds": 0,
            "builder_uptime_seconds": 0,
            "builder_battles_started": 0,
            "builder_battles_completed": 0,
            "builder_stars_earned": 0,
            "rph_updated_uptime_seconds": 0,
            "rph_history": [],
            "rph_window_start_uptime_seconds": 0,
            "rph_window_version": 0,
            "rph_village_start_battle": 0
        }
        self.start_timestamp = 0.0
        stats_file = stats_path or str(Path(getattr(settings, 'filepath', Path(__file__).resolve().parents[1] / 'config.json')).with_name('stats.json'))
        self._stats_store = StatsStore(stats_file, self.log)
        self.stats = self._stats_store.load(self.stats)
        if self.stats['rph_window_version'] < 2:
            self.stats['rph_window_start_uptime_seconds']=self.stats['uptime_seconds']
            self.stats['rph_village_start_battle'] = self.stats['battles_completed']
            self.stats['rph_window_version'] = 2
        self._uptime_accumulated = float(self.stats['uptime_seconds'])
        self._run_started_at = None
        self._builder_elapsed = float(self.stats['builder_uptime_seconds'])
        self._builder_uptime_at_entry = None
        self._stats_save_lock = threading.Lock()
        self._last_stats_save = -100.0
        self._stats_stop = threading.Event()
        self._stats_thread = threading.Thread(target=self._autosave_stats, daemon=True, name='CoCAttackStats')
        self._stats_thread.start()

    def _autosave_stats(self):
        while not self._stats_stop.wait(10):
            self.save_stats(force=True)

    def save_stats(self, force=False):
        if not hasattr(self, '_stats_store'):
            return
        with self._stats_save_lock:
            now = time.monotonic()
            if not force and now - self._last_stats_save < 10:
                return
            try:
                saved = self._stats_store.save(self.get_stats())
                with self._lifecycle_lock:
                    for key, value in saved.items():
                        self.stats[key] = StatsStore.combine(key,self.stats.get(key,0),value)
                    active_elapsed = max(0, time.monotonic() - self._run_started_at) if self._run_started_at is not None else 0
                    self._uptime_accumulated = max(self._uptime_accumulated, saved['uptime_seconds'] - active_elapsed)
                    builder_active = self._active_uptime() - self._builder_uptime_at_entry if self._builder_uptime_at_entry is not None else 0
                    self._builder_elapsed = max(self._builder_elapsed, saved['builder_uptime_seconds'] - builder_active)
                self._last_stats_save = now
            except (OSError, TypeError, ValueError) as exc:
                self.log(f'[Stats] Chưa lưu được thống kê: {exc}')

    def close(self):
        self.stop_bot()
        self._stats_stop.set()
        self._stats_thread.join(timeout=1)

    def reset_fastest_record(self):
        with self._stats_save_lock:
            with self._lifecycle_lock:
                self.stats['fastest_50_ms']=0
            self._stats_store.save(self.get_stats(),reset_fastest=True)
        self.log('[Kỷ lục] Đã đặt lại số đo 50%; các tổng thống kê được giữ nguyên.')
        self._publish_stats()

    def _active_uptime(self):
        elapsed = getattr(self, '_uptime_accumulated', 0.0)
        started = getattr(self, '_run_started_at', None)
        if started is not None:
            elapsed += max(0, time.monotonic() - started)
        return elapsed

    def get_stats(self):
        with getattr(self,'_lifecycle_lock',nullcontext()):
            snapshot = dict(self.stats)
            elapsed = self._active_uptime()
            snapshot['uptime_seconds'] = int(elapsed)
            builder_elapsed = getattr(self, '_builder_elapsed', 0.0)
            builder_entry = getattr(self, '_builder_uptime_at_entry', None)
            if builder_entry is not None:
                builder_elapsed += max(0, elapsed - builder_entry)
            snapshot['builder_uptime_seconds'] = int(builder_elapsed)
            return snapshot

    def _record_builder_battle(self, key, *, stars=None):
        with self._lifecycle_lock:
            self.stats[key] = self.stats.get(key, 0) + 1
            if key == "builder_battles_completed" and type(stars) is int and 0 <= stars <= 6:
                self.stats["builder_stars_earned"] += stars
        self._publish_stats()
        if key == "builder_battles_completed":
            self.save_stats(force=True)

    def _publish_stats(self):
        snapshot = self.get_stats()
        self.stats['uptime_seconds'] = snapshot['uptime_seconds']
        if self.stats_cb:
            self.stats_cb(snapshot)
        self.save_stats()

    def _checkpoint(self):
        while True:
            if (not self.is_running or threading.current_thread() is not self._thread
                    or getattr(getattr(self, "adb", None), "device_unavailable", False) is True):
                raise BotStoppedError()
            if not self.is_paused:
                return
            time.sleep(.1)

    def log(self, msg: str):
        self.log_cb(msg)

    def set_state(self, new_state: str):
        with getattr(self, '_lifecycle_lock', nullcontext()):
            if new_state == getattr(self, 'current_state', None):
                return
            builder_entry = getattr(self, '_builder_uptime_at_entry', None)
            if builder_entry is not None:
                self._builder_elapsed += max(0, self._active_uptime() - builder_entry)
            self._builder_uptime_at_entry = self._active_uptime() if new_state == self.STATE_BUILDER else None
            self.current_state = new_state
            if hasattr(self, 'village_state'):
                if new_state != 'PAUSED':
                    if new_state != self.STATE_COMBAT:
                        self.village_state.clear_battle('main')
                    if new_state != self.STATE_BUILDER:
                        self.village_state.clear_battle('builder')
                if new_state == self.STATE_BUILDER:
                    self.village_state.set_active_village('builder')
                elif new_state in (self.STATE_VILLAGE, self.STATE_SEARCHING, self.STATE_COMBAT):
                    self.village_state.set_active_village('main')
        if self.state_cb:
            self.state_cb(new_state)

    def _connect_mumu_with_retries(self, serial, *, startup=False):
        """Retry only the selected device; never silently switch accounts."""
        attempts = 3 if self.settings.get('auto_reconnect', True) else 1
        for attempt in range(attempts):
            if startup:
                if self._start_cancelled.is_set():
                    return False
            elif not self._is_current_worker() or getattr(self, 'stop_requested', False):
                return False
            if attempt:
                self.log(f'[ADB] Thử kết nối lại {serial} ({attempt + 1}/{attempts})...')
            try:
                connected = self.adb.connect_device(serial, start_server=(attempt == 0))
            except BotStoppedError:
                cancelled = (self._start_cancelled.is_set() if startup else
                             not self._is_current_worker() or getattr(self, 'stop_requested', False))
                if cancelled:
                    return False
                if not self.adb.device_unavailable:
                    raise
                connected = False
            if connected:
                if attempt or not startup:
                    self.log(f'[ADB] Đã kết nối lại {serial}.')
                return True
            if attempt + 1 < attempts:
                if startup:
                    if self._start_cancelled.wait(2):
                        return False
                else:
                    for _ in range(4):
                        if not self._is_current_worker() or getattr(self, 'stop_requested', False):
                            return False
                        time.sleep(.5)
        return False

    def start_bot(self, device_serial: Optional[str] = None):
        """Khởi động worker thread mới."""
        with self._lifecycle_lock:
            self._start_cancelled.clear()
        if self._thread is not None and self._thread.is_alive():
            self.log('Worker trước chưa kết thúc; chờ rồi bắt đầu lại.')
            return False
        if not self.settings.get("main_village_enabled", True) and not self.settings.get("builder_base_enabled", False):
            self.log("Hãy bật farm Làng chính hoặc Làng đêm trước khi bắt đầu.")
            return False
        serial = device_serial or self.settings.get("device_serial", "127.0.0.1:5555")
        if not self._fixed_attack_ready(serial):
            return False
        self.log(f"Đang kết nối tới thiết bị {serial}...")

        if not self._connect_mumu_with_retries(serial, startup=True):
            if not self._start_cancelled.is_set():
                self.log(f"LỖI: Không thể kết nối tới {serial} sau nhiều lần thử. Kiểm tra MuMu và cổng ADB.")
            return False

        if self.adb.device_unavailable is True:
            self.set_state('ADB_DISCONNECTED')
            self.log('[ADB] MuMu đã mất kết nối trong lúc khởi động.')
            return False
        self.log("Kết nối thành công! Tối ưu hóa MuMu Player...")
        self.optimizer.optimize_windows_priority()
        self.optimizer.apply_android_optimizations()

        with self._lifecycle_lock:
            if self._start_cancelled.is_set():
                return False
            self._new_scan_empty_until = 0
            self._new_scan_empty_account = None
            self._new_scan_empty_snapshot = None
            self._army_lab_busy_until = 0
            self._army_lab_busy_account = None
            self._identity_restart_used = False
            self.village.player_tag_failure = None
            self.village.player_tag_failures = 0
            self.village._session_player_tag = None
            self.village._session_legacy_key = None
            self.village._identity_verified_at = 0.0
            self.village._verified_header_key = None
            self.village.current_account_key = None
            self.village.walls_confirmed_maxed = False
            self._startup_origin = None
            self._startup_night_pass_done = False
            self._night_layout_scan_done = False
            self._startup_main_work_pending = False
            self._main_scanned_account = None
            self.is_running = True
            self.is_paused = False
            self._action_failures = {}
            self._pending_recovery = None
            self._recovery_attempts = 0
            self._recovery_retry_at = 0
            self._robot_restart_attempted = False
            self._recovery_paused = False
            self._scout_counted = False
            builder = getattr(self, "builder", None)
            if builder is not None:
                builder.battle.army.unavailable_request = None
                builder.battle.army.verified_request = None
                builder.consecutive_no_wall_count = 0
                builder.walls_confirmed_maxed = False
                builder.retry_after = 0
                builder.next_maintenance = 0
                builder.next_resource_scan = 0
                builder.next_wall_check = 0
                builder.next_building_check = 0
                builder.next_cart_check = 0
                builder.main_turn_until = 0
                builder.all_upgrades_complete = False
                builder.main_schedule_checked = False
                builder.night_schedule_checked = False
                builder.main_builders_wait_until = 0
                builder.night_builders_wait_until = 0
                builder._restored_wait_account = None
            self.start_timestamp = time.time()
            self._run_started_at = time.monotonic()
            self._reset_restart_timer()
            self.set_state(self.STATE_VILLAGE)

            self._thread = threading.Thread(target=self.run, daemon=True, name="CoCAttackWorker")
            self.adb.owner_thread = self._thread
            self._thread.start()
            return True

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def pause_bot(self):
        with self._lifecycle_lock:
            if not self.is_running or self.is_paused:return
            self._state_before_pause = self.current_state
            if self._run_started_at is not None:
                self._uptime_accumulated+=max(0,time.monotonic()-self._run_started_at)
                self._run_started_at=None
            self.is_paused = True
        self.set_state("PAUSED")
        self.log("Đã tạm dừng bot.")
        self.save_stats(force=True)

    def resume_bot(self):
        with self._lifecycle_lock:
            if not self.is_running or not self.is_paused:return
            self._run_started_at=time.monotonic()
            self.is_paused = False
            if getattr(self, '_recovery_paused', False):
                self._recovery_paused = False
                self._pending_recovery = None
                self._recovery_attempts = 0
                self._recovery_retry_at = 0
                self._robot_restart_attempted = False
                self._action_failures = {}
            if hasattr(self, 'builder') and self.builder is not None:
                self.builder.consecutive_no_wall_count = 0
                self.builder.walls_confirmed_maxed = False
                self.builder.next_wall_check = 0
        self.set_state(getattr(self, "_state_before_pause", self.STATE_VILLAGE))
        self.log("Tiếp tục chạy bot.")

    def stop_bot(self):
        with self._lifecycle_lock:
            if self._run_started_at is not None:
                self._uptime_accumulated += max(0, time.monotonic() - self._run_started_at)
                self._run_started_at = None
            self._start_cancelled.set()
            self.is_running = False
            self.adb.owner_thread = None
        self.set_state(self.STATE_IDLE)
        self.log("Đã dừng bot.")
        self.save_stats(force=True)

    def _is_current_worker(self) -> bool:
        return self.is_running and threading.current_thread() is self._thread

    def identify_screen_state(self, screen):
        if screen is None: return 'SCREEN_NONE'
        for name in ('return_home','okhome'):
            if self.vision.match_template(screen,name,.75,roi=(450,600,700,280)):
                return 'STATE_BATTLE_END'
        if self.village.is_in_main_village(screen):return 'STATE_MAIN_VILLAGE'
        phase=self.vision.battle_phase(screen)
        if phase=='scouting':return 'STATE_ENEMY_BASE'
        if phase=='combat':return 'STATE_IN_COMBAT'
        if self.vision.is_attack_menu_open(screen)[0]:return 'STATE_ATTACK_MENU'
        return 'STATE_UNKNOWN'

    def _reset_restart_timer(self):
        minutes = max(1, int(self.settings.get('periodic_restart_interval_mins', 60)))
        self._restart_due_at = time.monotonic() + minutes * 60
        self._restart_deferred = False

    def _periodic_restart_due(self):
        due = getattr(self, '_restart_due_at', None)
        return (self.settings.get('periodic_restart_enabled', True)
                and due is not None and time.monotonic() >= due)

    def _maybe_periodic_restart(self, screen=None):
        if not self.settings.get('periodic_restart_enabled', True):
            return False
        if getattr(self, '_restart_due_at', None) is None:
            self._reset_restart_timer()
            return False
        if not self._periodic_restart_due():
            return False
        screen = screen if screen is not None else self.adb.screencap()
        if not self.village.is_in_main_village(screen):
            if not getattr(self, '_restart_deferred', False):
                self.log('[Restart] Đến giờ khởi động lại; chờ kết thúc trận và về làng.')
                self._restart_deferred = True
            return False
        self.save_stats(force=True)
        self.log('[Restart] Khởi động lại game theo lịch định kỳ; thống kê được giữ nguyên.')
        if self.restart_game_and_recover(force=True):
            self._reset_restart_timer()
        else:
            self._restart_due_at = time.monotonic() + 300
            self.log('[Restart] Chưa xác nhận vào làng sau restart; thử lại lịch sau 5 phút.')
        return True

    def _request_recovery(self, reason):
        if getattr(self, '_pending_recovery', None) is None or reason == 'robot':
            self._pending_recovery = reason

    def _queue_screen_fault(self, screen):
        reason = self.vision.game_fault(screen)
        if reason in ('network', 'robot'):
            if getattr(self, '_pending_recovery', None) is None:
                detail = getattr(self.vision, 'last_game_fault_message', None)
                if detail:
                    self.log(f'[Recovery] Thông báo game nhận diện: {detail}')
                if isinstance(screen, np.ndarray) and screen.size:
                    try:
                        folder = Path(__file__).resolve().parents[1] / 'supervision' / 'runtime'
                        folder.mkdir(exist_ok=True)
                        if cv2.imwrite(str(folder / 'recovery-game-fault.png'), screen):
                            self.log('[Recovery] Đã lưu ảnh thông báo lỗi vào supervision/runtime/recovery-game-fault.png.')
                    except (OSError, cv2.error):
                        pass
            self._request_recovery(reason)
            return True
        return False

    def _action_outcome(self, action, success, *, inspect=True):
        failures = getattr(self, '_action_failures', None)
        if failures is None:
            failures = self._action_failures = {}
        if success:
            failures.pop(action, None)
            return
        if getattr(self, '_pending_recovery', None):
            return
        if inspect and self._queue_screen_fault(self.adb.screencap()):
            return
        failures[action] = failures.get(action, 0) + 1
        self.log(f'[Recovery] {action} chưa thành công ({failures[action]}/5).')
        if failures[action] >= 5:
            self._request_recovery(action)

    def _service_recovery(self):
        reason = getattr(self, '_pending_recovery', None)
        if reason is None:
            return False
        if time.monotonic() < getattr(self, '_recovery_retry_at', 0):
            return True
        attempts = getattr(self, '_recovery_attempts', 0)
        labels = {'network': 'Lỗi kết nối game', 'robot': 'Kiểm tra robot'}
        label = labels.get(reason, reason)
        if (not self.settings.get('auto_restart_game', True) or attempts >= 3
                or (reason == 'robot' and getattr(self, '_robot_restart_attempted', False))):
            self.log(f'[Recovery] {label}. Tạm dừng để kiểm tra thủ công.')
            self._recovery_paused = True
            self.pause_bot()
            return True
        self._recovery_attempts = attempts + 1
        if reason == 'robot':
            self._robot_restart_attempted = True
        self.log(f'[Recovery] {label}. Khởi động lại game ({attempts+1}/3).')
        self.save_stats(force=True)
        try:
            if reason == 'Chụp màn hình MuMu' and self.settings.get('auto_reconnect', True):
                self.adb.connect_device(self.settings.get('device_serial', '127.0.0.1:5555'), start_server=False)
            recovered = self.restart_game_and_recover()
        except BotStoppedError:
            raise
        except Exception as exc:
            self.log(f'[Recovery] Khởi động lại thất bại: {exc}')
            recovered = False
        if recovered:
            self._pending_recovery = None
            self._action_failures = {}
            self._recovery_retry_at = 0
        else:
            self._recovery_retry_at = 0
            if self._pending_recovery == 'robot' and self._robot_restart_attempted:
                self.log('[Recovery] Kiểm tra robot vẫn xuất hiện. Cần xử lý trong game rồi tiếp tục.')
                self._recovery_paused = True
                self.pause_bot()
            elif self._recovery_attempts >= 3:
                self.log('[Recovery] Ba lần khởi động lại chưa phục hồi. Tạm dừng để kiểm tra.')
                self._recovery_paused = True
                self.pause_bot()
            else:
                self.log('[Recovery] Chưa vào lại làng. Thử phục hồi lại ngay.')
        return True

    def restart_game_and_recover(self, force=False) -> bool:
        """
        Tự động khởi động lại Clash of Clans khi bị kẹt/lỗi/mất kết nối:
        1. Force stop game
        2. Mở lại game
        3. Chờ nạp game vào một trong hai làng
        4. Tắt mọi thông báo khởi động và bỏ chọn công trình
        """
        if not force and not self.settings.get('auto_restart_game',True):
            self.log('[Recovery] Tự khởi động lại đang tắt; giữ trạng thái để kiểm tra.')
            return False
        self.log('[Recovery] Đang khởi động lại Clash of Clans...')
        self.set_state(self.STATE_RECOVERY)

        self.optimizer.restart_game()
        self.village._identity_verified_at = 0.0

        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            self._checkpoint()
            time.sleep(1.5)
            screen = self.adb.screencap()
            if screen is None:
                continue
            screen = self._ensure_game_resolution(screen)
            if screen is None:
                return False

            if self.screen_cb:
                self.screen_cb(screen)

            fault = self.vision.game_fault(screen)
            if fault == 'robot':
                self._request_recovery(fault)
                return False
            if fault == 'network':
                return False

            # Game may reopen in the village that was active before the restart.
            if self.village.is_in_main_village(screen):
                self.log("[Recovery] Đã vào lại Làng chính thành công!")
                time.sleep(1.0)
                self.village.deselect_all()
                self.set_state(self.STATE_VILLAGE)
                self._reset_restart_timer()
                return True
            builder = getattr(self, 'builder', None)
            if builder is not None and builder.is_home(screen):
                self.log("[Recovery] Đã vào lại Làng đêm thành công!")
                builder.deselect()
                self.set_state(self.STATE_BUILDER)
                self._reset_restart_timer()
                return True
            self.village.close_popups_and_dialogs(screen)

        self.log('[Recovery] Hết thời gian chờ nạp game.')
        return False

    def _open_game_if_needed(self):
        """Bring the game forward without force-stopping a background session."""
        now = time.monotonic()
        if now - getattr(self, '_last_launch_check', -100) < 8:
            return False
        self._last_launch_check = now
        if self.adb.is_coc_foreground():
            return False
        self.log('[Launch] MuMu chưa mở game; đang mở Clash of Clans...')
        self.set_state(self.STATE_RECOVERY)
        self.adb.start_coc()
        self._reset_restart_timer()
        deadline = time.monotonic() + 60
        while self._is_current_worker() and time.monotonic() < deadline:
            self._checkpoint()
            screen = self.adb.screencap()
            if screen is not None:
                screen = self._ensure_game_resolution(screen)
                if screen is None:
                    return False
                if self.screen_cb:
                    self.screen_cb(screen)
                if self._queue_screen_fault(screen):
                    return True
                if self.village.is_in_main_village(screen):
                    self.set_state(self.STATE_VILLAGE)
                    return True
                # Preserve an existing battle or attack menu when resuming.
                if self.identify_screen_state(screen) != 'STATE_UNKNOWN':
                    return True
                self.village.close_popups_and_dialogs(screen)
            time.sleep(1.5)
        return True

    def run(self):
        try:
            self._run_loop()
        except BotStoppedError:
            # Stop/reconnect can race with ADB guards outside the iteration.
            # Cancellation is a normal worker exit, not an unhandled traceback.
            pass
        finally:
            if self._is_current_worker() and self.adb.device_unavailable is True:
                self.stop_bot()
                self.set_state('ADB_DISCONNECTED')
                self.log('[ADB] Mất kết nối MuMu. Đã dừng bot. Mở MuMu rồi bấm Kết nối hoặc Bắt đầu.')

    def _run_loop(self):
        """Main Automation Loop."""
        self.log("Bắt đầu chu trình tự động hóa Clash of Clans.")

        try:
            self._open_game_if_needed()
        except BotStoppedError:
            if not self.adb.device_unavailable:
                return
        except Exception as exc:
            self.log(f'[Launch] Không kiểm tra được game: {exc}')

        while self._is_current_worker():
            if self.is_paused:
                time.sleep(1.0)
                continue

            if self.adb.device_unavailable:
                if not self.settings.get('auto_reconnect', True):
                    return
                self.set_state('ADB_DISCONNECTED')
                serial = self.adb.device_serial or self.settings.get('device_serial', '127.0.0.1:5555')
                self.log(f'[ADB] Mất kết nối {serial}; đang chủ động kết nối lại.')
                if not self._connect_mumu_with_retries(serial):
                    if not self._is_current_worker() or getattr(self, 'stop_requested', False):
                        return
                    self.log('[ADB] Chưa kết nối được MuMu; tiếp tục thử sau 5 giây.')
                    for _ in range(10):
                        if not self._is_current_worker() or getattr(self, 'stop_requested', False):
                            return
                        time.sleep(.5)
                    continue
                self.set_state(self.STATE_RECOVERY)
            try:
                self._iteration()
            except BotStoppedError:
                if self.adb.device_unavailable and self.settings.get('auto_reconnect', True):
                    continue
                return
            except Exception as e:
                self.log(f"Ngoại lệ vòng lặp: {e}")
                self._action_outcome('Vòng xử lý game', False, inspect=False)
                time.sleep(2.0)
            else:
                self._action_outcome('Vòng xử lý game', True)

            # Update uptime
            self._publish_stats()

            time.sleep(0.6)

    def _ensure_game_resolution(self, screen):
        if screen.shape[:2] == (900, 1600):
            return screen
        if not self.adb.is_coc_foreground():
            return None

        height, width = screen.shape[:2]
        self.log(f'[MuMu] Ảnh hiện tại {width}×{height}. Đang tự chỉnh về 1600×900, 240 DPI.')
        try:
            self.optimizer.set_optimal_resolution()
            for _ in range(8):
                self._checkpoint()
                time.sleep(.5)
                screen = self.adb.screencap()
                if screen is not None and screen.shape[:2] == (900, 1600):
                    self.log('[MuMu] Đã xác nhận ảnh game 1600×900.')
                    return screen
        except BotStoppedError:
            raise
        except Exception as exc:
            self.log(f'[MuMu] Không chỉnh được độ phân giải: {exc}')

        if not self.adb.device_unavailable:
            self.log('[MuMu] Chưa xác nhận được ảnh game 1600×900. Đã dừng bot.')
            self.stop_bot()
        return None

    def _prepare_village_startup(self, screen):
        """Bind the session to an account before either village can act."""
        builder = getattr(self, 'builder', None)
        night = builder is not None and builder.is_home(screen)
        main = self.village.is_in_main_village(screen)
        if not (night or main):
            return True
        if getattr(self, '_startup_origin', None) is None:
            self._startup_origin = 'night' if night else 'main'
            self._startup_main_work_pending = main and self.settings.get('main_village_enabled', True)
        account = self.village.check_and_sync_account(screen, self.log)
        if not account:
            self.log('[Tài khoản] Chưa nhận diện được tài khoản; chờ màn hình làng ổn định.')
            return False
        if builder is not None:
            builder.restore_builder_waits(self.log)
        if night or not self.settings.get('main_village_enabled', True):
            return True
        if getattr(self, '_main_scanned_account', None) == account:
            return True
        self._startup_main_work_pending = True
        worker = self.farm_finish.suggested_upgrader()
        first = worker.observe_home_builders(screen)
        second = worker.observe_home_builders(self.adb.screencap()) if type(first) is int else None
        if type(first) is int and first == second:
            self.log(f'[Thợ] Đầu lượt làng chính có {first} thợ rảnh.')
            if builder is not None:
                builder.main_schedule_checked = True
                if first == 0:
                    seconds = worker.get_shortest_ongoing_builder_seconds()
                    builder.main_builders_wait_until = time.monotonic() + (seconds + 10 if type(seconds) is int and seconds > 0 else 300)
                else:
                    builder.main_builders_wait_until = 0
                builder.remember_builder_waits()
        else:
            self.log('[Thợ] Chưa đọc ổn định số thợ làng chính; sẽ kiểm tra lại.')
        self.log('[Wall] Quét tường đầu phiên qua Layout Editor.')
        result = self.village.verify_walls(self.log)
        if result is None:
            self.log('[Wall] Quét chưa thành công; hoãn công việc làng chính và thử lại lượt sau.')
            return False
        self._main_scanned_account = account
        return True

    def _iteration(self):
        if not self.settings.get("main_village_enabled", True) and not self.settings.get("builder_base_enabled", False):
            self.set_state(self.STATE_IDLE)
            time.sleep(1)
            return
        if self._service_recovery():
            return
        # 1. Chụp màn hình giả lập
        screen = self.adb.screencap()
        if screen is None:
            if self.adb.device_unavailable is True:
                return
            self.log("Không nhận được hình ảnh giả lập. Đang kiểm tra game...")
            if self.settings.get('auto_reconnect',True):
                self.adb.connect_device(self.settings.get('device_serial','127.0.0.1:5555'), start_server=False)
            self._action_outcome('Chụp màn hình MuMu', False, inspect=False)
            return
        self._action_outcome('Chụp màn hình MuMu', True)
        if screen.shape[:2] != (900, 1600):
            screen = self._ensure_game_resolution(screen)
            if screen is None:
                if self._is_current_worker() and not self.adb.device_unavailable:
                    self._open_game_if_needed()
                return

        if self.screen_cb:
            self.screen_cb(screen)

        if self.vision.has_system_dialog(screen) and self._queue_screen_fault(screen):
            return

        if self.village.dismiss_notification_prompt(screen):
            self.log('[Bot] Đã đóng thông báo Star Bonus.')
            return

        if not self._prepare_village_startup(screen):
            return

        if self._handle_builder_base(screen):
            return

        # 2. Nhận diện trạng thái màn hình hiện tại
        state = self.identify_screen_state(screen)

        if state == "STATE_UNKNOWN":
            if self._queue_screen_fault(screen):
                return
            self._action_outcome('Nhận diện màn hình', False, inspect=False)
            if getattr(self, '_pending_recovery', None):
                return
            if self._open_game_if_needed():
                return
            # Thử đóng các popup ngẫu nhiên nếu có
            if self.village.close_popups_and_dialogs(screen):
                self.log("[Bot] Đã đóng một thông báo popup nổi.")
                time.sleep(1.0)
                return

            time.sleep(1.5)
            return

        # Đã nhận diện được trạng thái hợp lệ -> reset bộ đếm lỗi
        self._action_outcome('Nhận diện màn hình', True)

        # TRƯỜNG HỢP A: ĐANG TRONG TRẬN ĐÁNH
        if state == "STATE_IN_COMBAT":
            if not self.settings.get("main_village_enabled", True):
                time.sleep(1)
                return
            self.log("[Recovery] Phát hiện trận đấu đang diễn ra. Chuyển sang theo dõi trận đánh...")
            self.set_state(self.STATE_COMBAT)
            result=self.combat.monitor_existing_battle(log_callback=self.log)
            self._after_combat(result)
            return

        # TRƯỜNG HỢP B: BẢNG KẾT QUẢ TRẬN ĐÁNH
        if state == "STATE_BATTLE_END":
            self.log("[Bot] Phát hiện bảng kết quả trận đánh. Bấm Return Home...")
            home = self._ensure_main_village()
            self._action_outcome('Quay về làng', home)
            if home:
                self._run_village_maintenance(force=True)
            return

        # TRƯỜNG HỢP C: ĐANG SOI NHÀ ĐỐI THỦ (HOẶC VỪA TÌM TRẬN XONG)
        if state == "STATE_ENEMY_BASE":
            self._handle_scouting(screen)
            return

        # TRƯỜNG HỢP D: MENU TẤN CÔNG ĐANG MỞ SẴN
        if state == "STATE_ATTACK_MENU":
            if not self.settings.get("main_village_enabled", True):
                self._ensure_main_village()
                return
            if self._farm_finish_gate(screen, attack_menu=True):
                return
            if self._wall_hold_gate(screen, attack_menu=True):
                return
            if not self._fixed_attack_ready():
                time.sleep(1)
                return
            self.log("[Bot] Menu Tấn công đang mở -> Bấm Tìm trận (Find a Match)...")
            self.set_state(self.STATE_SEARCHING)
            searching = self.battle.start_find_match(log_callback=self.log)
            self._action_outcome('Bấm Tìm trận', searching)
            if searching:
                self._run_scouting_loop()
            elif not getattr(self, '_pending_recovery', None):
                self.log('[Bot] Chưa xác nhận được Tìm trận.')
            return

        # TRƯỜNG HỢP E: ĐANG Ở LÀNG CHÍNH (MAIN VILLAGE)
        if state == "STATE_MAIN_VILLAGE":
            if not self.settings.get("main_village_enabled", True):
                self.set_state(self.STATE_IDLE)
                time.sleep(1)
                return
            if not getattr(getattr(self, '_wall_farm_guard', None), 'active', False):
                self.set_state(self.STATE_VILLAGE)
            if self._maybe_periodic_restart(screen):
                return

            if getattr(self, '_manual_village_scan_requested', False):
                self._manual_village_scan_requested = False
                readings = self.vision.read_village_resources(screen)
                updated = self.village_state.update_resources('main', readings)
                builders = self.farm_finish.suggested_upgrader().observe_home_builders(screen)
                self.log('[Làng] ' + ('Đã cập nhật thông số từ ảnh làng.' if updated else 'Chưa đọc rõ tài nguyên; sẽ giữ số liệu trước.'))
                if builders is not None:
                    self.log(f'[Thợ] Đã đọc {builders} thợ thường rảnh từ cùng ảnh làng.')

            if self._farm_finish_gate(screen):
                self._startup_main_work_pending = False
                return

            # Thực hiện chu trình chăm sóc làng (Luyện quân, Thu hoạch, Nâng tường)
            self._run_village_maintenance()
            if getattr(self, '_startup_main_work_pending', False):
                self._startup_main_work_pending = False
                self._defer_battle_after_maintenance = True
            if getattr(self, '_defer_battle_after_maintenance', False):
                self._force_builder_maintenance = True
                return
            fresh=self.adb.screencap()
            if not self.settings.get("main_village_enabled", True):
                return
            home = fresh is not None and self.village.is_in_main_village(fresh)
            self._action_outcome('Xác minh làng sau bảo trì', home)
            if not home:
                self.log('[Village] Chưa ở làng sau bảo trì; chưa tìm trận mới.')
                return
            if self._maybe_periodic_restart(fresh):
                return

            if self._wall_hold_gate(fresh):
                return

            if not self._fixed_attack_ready():
                time.sleep(1)
                return
            # Mở menu Tấn công có xác minh
            self.set_state(self.STATE_SEARCHING)
            opened = self.battle.open_attack_menu(log_callback=self.log)
            self._action_outcome('Mở menu Tấn công', opened)
            if not opened:
                self.log("[Bot] Không thể mở menu Tấn công. Kiểm tra lại ở vòng lặp sau...")
                return

            if not self.settings.get("main_village_enabled", True):
                return
            # Bấm Tìm trận có xác minh
            searching = self.battle.start_find_match(log_callback=self.log)
            self._action_outcome('Bấm Tìm trận', searching)
            if not searching:
                self.log("[Bot] Không thể bấm Tìm trận. Thử lại...")
                return

            # Vào chu trình soi nhà
            self._run_scouting_loop()

    def _verify_builder_walls_once(self, builder):
        """Verify both Builder Base stages before the first action in this village."""
        if getattr(self, '_night_layout_scan_done', False):
            return True
        scan = getattr(builder, 'scan_initial_layout_walls', None)
        if not callable(scan):
            self.log('[BH] Không có chức năng xác minh tường Layout Editor.')
            return False
        self.log('[BH] Đang quét tường hai Stage qua Layout Editor.')
        verified = scan(self.log)
        if not builder.is_home(self.adb.screencap()):
            self.log('[BH] Chưa về được làng sau khi quét; dừng bot để kiểm tra.')
            self.stop_bot()
            return False
        if not verified:
            self.log('[BH] Chưa xác minh đủ tường hai Stage; hoãn tác vụ làng đêm và thử lại.')
            return False
        self._night_layout_scan_done = True
        return True

    def _handle_builder_base(self, screen):
        builder = getattr(self, "builder", None)
        if builder is None:
            return False
        if (getattr(self, '_startup_main_work_pending', False)
                and self.village.is_in_main_village(screen)):
            return False
        bh_failures = getattr(getattr(builder, "battle", None), "find_now_failures", 0)
        if isinstance(bh_failures, (int, float)) and bh_failures >= 3:
            self.log("[BH] Làng đêm bị kẹt liên tiếp (Find Now không phản hồi 3 lần). Khởi động lại game...")
            builder.battle.find_now_failures = 0
            self.restart_game_and_recover(force=True)
            return True
        if ((builder.active or builder.battle.in_progress or self.settings.get('builder_base_enabled', False))
                and builder.battle.obstruction_buttons(screen)):
            if not builder.battle.in_progress:
                builder.battle.in_progress = True
                builder.battle.combat_seen = False
                builder.battle.planned = False
                builder.battle.match_requested = time.monotonic()
                self.log("[BH] Phát hiện lại cảnh báo chướng ngại khi khôi phục; tiếp tục xử lý tìm trận.")
            self.set_state(self.STATE_BUILDER)
            builder.battle.monitor(self.log, seconds=20)
            return True
        if getattr(builder, "dismiss_star_bonus", None) and builder.dismiss_star_bonus(screen, self.log):
            time.sleep(0.5)
            screen = self.adb.screencap()
        home = builder.is_home(screen)
        if builder.battle.in_progress and not home:
            # A reconnect may return directly to TH while a previous BH fight ends.
            if not builder.night_palette(screen) and self.village.is_in_main_village(screen):
                builder.battle.in_progress = False
                builder.active = False
            else:
                self.set_state(self.STATE_BUILDER)
                if not builder.battle.monitor(self.log, seconds=12):
                    self._queue_screen_fault(self.adb.screencap())
                return True
        if home:
            self.set_state(self.STATE_BUILDER)
            builder.battle.in_progress = False
            if hasattr(self, 'village_state'):
                self.village_state.clear_battle('builder')
            village = getattr(self, 'village', None)
            if village is not None and hasattr(village, 'check_and_sync_account'):
                village.check_and_sync_account(screen, self.log)
            if callable(getattr(builder, 'restore_builder_waits', None)):
                builder.restore_builder_waits(self.log)
            if not BotEngine._verify_builder_walls_once(self, builder):
                return True
            first_night = (getattr(self, '_startup_origin', None) == 'night'
                           and not getattr(self, '_startup_night_pass_done', False))
            if first_night:
                builder.next_wall_check = 0
                builder.next_building_check = 0
                builder.next_maintenance = 0
                builder.walls_confirmed_maxed = False
            if not (getattr(self, '_startup_night_pass_done', False)
                    and self.settings.get('main_village_enabled', True)
                    and getattr(self, '_main_scanned_account', None) is None):
                builder.run_cycle(screen, self.log, first_pass=first_night)
                if callable(getattr(builder, 'remember_builder_waits', None)):
                    builder.remember_builder_waits()
            if first_night:
                self._startup_night_pass_done = True
            if (getattr(self, '_startup_origin', None) == 'night'
                    and self._startup_night_pass_done
                    and self.settings.get('main_village_enabled', True)
                    and getattr(self, '_main_scanned_account', None) is None
                    and not builder.battle.in_progress):
                if builder.leave(self.adb.screencap()):
                    builder.active = False
                    self.set_state(self.STATE_VILLAGE)
                return True
            if self._service_cross_village_sleep():
                return True
            main_waiting = time.monotonic() < getattr(builder, 'main_builders_wait_until', 0)
            if (self.settings.get("main_village_enabled", True) and not builder.battle.in_progress
                    and not main_waiting):
                if builder.leave(self.adb.screencap()):
                    builder.active = False
                    self.set_state(self.STATE_VILLAGE)
                    return True
            time.sleep(1)
            return True
        if builder.active:
            if self.village.is_in_main_village(screen) and not builder.night_palette(screen):
                builder.active = False
            else:
                if not self._queue_screen_fault(screen):
                    builder.close_panel(screen)
                time.sleep(.7)
                return True
        if (self.settings.get('main_village_enabled', True)
                and self.settings.get('builder_base_enabled', False)
                and not getattr(builder, 'main_schedule_checked', False)
                and builder.is_main(screen)):
            if callable(getattr(builder, 'restore_builder_waits', None)):
                builder.restore_builder_waits(self.log)
            if not getattr(builder, 'main_schedule_checked', False):
                worker = self.farm_finish.suggested_upgrader()
                first = worker.observe_home_builders(screen)
                second = worker.observe_home_builders(self.adb.screencap()) if type(first) is int else None
                if type(first) is int and first == second:
                    builder.main_schedule_checked = True
                    if first == 0:
                        remaining = worker.get_shortest_ongoing_builder_seconds()
                        seconds = remaining + 10 if type(remaining) is int and remaining > 0 else 300
                        builder.main_builders_wait_until = time.monotonic() + seconds
                        builder.main_turn_until = 0
                        if callable(getattr(builder, 'remember_builder_waits', None)):
                            builder.remember_builder_waits()
                        self.log(f'[Thợ] Kiểm tra đầu lượt: làng chính 0 thợ rảnh, kiểm tra lại sau khoảng {max(1, seconds//60)} phút.')
                    else:
                        builder.main_builders_wait_until = 0
                        if callable(getattr(builder, 'remember_builder_waits', None)):
                            builder.remember_builder_waits()
                        self.log(f'[Thợ] Kiểm tra đầu lượt: làng chính có {first} thợ rảnh.')
                    screen = self.adb.screencap()
                else:
                    self.log('[Thợ] Chưa xác nhận được thợ làng chính qua hai ảnh; sẽ kiểm tra lại.')
        if builder.due() and builder.is_main(screen):
            self.set_state(self.STATE_BUILDER)
            builder.run_cycle(screen, self.log)
            if self._service_cross_village_sleep():
                return True
            main_waiting = time.monotonic() < getattr(builder, 'main_builders_wait_until', 0)
            if (self.settings.get("main_village_enabled", True) and builder.active
                    and not builder.battle.in_progress and not main_waiting):
                if builder.leave(self.adb.screencap()):
                    builder.active = False
                    self.set_state(self.STATE_VILLAGE)
            return True
        return False

    def _run_scouting_loop(self):
        """Vòng lặp tìm kiếm và soi nhà đối thủ cho đến khi tìm được mục tiêu phù hợp."""
        self._scout_counted = False
        skips = 0
        max_skips = self.settings.get("max_search_skips", 50)

        while self.is_running and not self.is_paused and skips < max_skips:
            enemy_screen = self.battle.wait_for_enemy_base(timeout_sec=40.0, log_callback=self.log)
            if enemy_screen is None:
                self._action_outcome('Chờ đối thủ', False)
                if not getattr(self, '_pending_recovery', None):
                    self.log('[Search] Hết thời gian chờ đối thủ.')
                break

            if not self.vision.is_enemy_base(enemy_screen):
                self._action_outcome('Chờ đối thủ', False)
                self.log("[Search] Cảnh báo: Màn hình không phải căn cứ đối thủ. Dừng chu trình soi...")
                break
            self._action_outcome('Chờ đối thủ', True)
            if self._periodic_restart_due():
                self.log('[Restart] Đến giờ trong lúc soi nhà; về làng để khởi động lại game.')
                self.battle.surrender_and_return_home(log_callback=self.log)
                if self._ensure_main_village():
                    self._maybe_periodic_restart()
                return

            if self.screen_cb:
                self.screen_cb(enemy_screen)

            # Đánh giá mục tiêu
            should_attack = self._handle_scouting(enemy_screen, skips=skips)
            if should_attack:
                break

            skips += 1
            self.stats["searches_skipped"] += 1
            self._publish_stats()

        if skips >= max_skips:
            self.log("[Search] Đã đạt giới hạn số lần Next. Rút quân về Làng chính...")
            self.battle.surrender_and_return_home(log_callback=self.log)
            time.sleep(2.0)

    def _fixed_attack_ready(self, serial=None):
        if (self.settings.get('main_village_enabled', True)
                and self.settings.get('attack_strategy') == FixedAttackPlan.KEY):
            if serial is None:
                serial = getattr(self.adb, 'device_serial', None)
                if not isinstance(serial, str):
                    serial = self.settings.get('device_serial')
            if FixedAttackPlan.load(self.settings, serial) is None:
                self.log('[Rải cố định] Thiết bị chưa có hiệu chuẩn hợp lệ. Mở Cách thả quân → Hiệu chuẩn.')
                return False
        return True

    def _handle_scouting(self, screen: np.ndarray, skips: int = 0) -> bool:
        """Đọc tài nguyên, đưa ra quyết định đánh hay bỏ qua."""
        if not self.settings.get("main_village_enabled", True):
            self.battle.surrender_and_return_home(log_callback=self.log)
            return True
        if not self._fixed_attack_ready():
            self.battle.surrender_and_return_home(log_callback=self.log)
            return True
        should_attack, loot = self.battle.evaluate_target(screen)
        if not getattr(self, '_scout_counted', False):
            self.stats['bases_scanned'] = self.stats.get('bases_scanned', 0) + 1
            self._scout_counted = True
        self._publish_stats()
        def amount(key):
            return f"{loot[key]:,}" if loot.get('known',{}).get(key,True) else 'chưa đọc rõ'
        self.log(
            f"[Soi Nhà #{skips+1}] 💰 Vàng: {amount('gold')} | 🧪 Dầu: {amount('elixir')} | 🟣 Hắc dầu: {amount('dark_elixir')}"
        )

        if should_attack:
            self.log("[Combat] Đối thủ đạt ngưỡng; bắt đầu tấn công.")
            self.set_state(self.STATE_COMBAT)
            self.stats['battles_started'] = self.stats.get('battles_started', 0) + 1
            self._publish_stats()
            options = {'approved_scout': screen} if self.settings.get('attack_strategy') == FixedAttackPlan.KEY else {}
            result = self.combat.execute_full_attack(log_callback=self.log, initial_loot=loot, **options)
            self._after_combat(result)
            return True
        else:
            uncertain=any(not loot.get('known',{}).get(key,True) for key in ('gold','elixir','dark_elixir')
                          if self.settings.get('loot_filter_'+key,True))
            self.log('[Search] '+('Chưa đọc rõ tài nguyên cần lọc' if uncertain else 'Tài nguyên chưa đạt điều kiện đã chọn')+' -> Bấm Next tìm nhà khác...')
            moved = self.battle.click_next(log_callback=self.log)
            self._action_outcome('Chuyển đối thủ', moved)
            if not moved:
                self.log('[Search] Chưa xác nhận chuyển nhà; dừng lượt soi để kiểm tra lại.')
                return True
            self._scout_counted = False
            time.sleep(1.0)
            return False

    def _after_combat(self,result):
        if result.get('reason') == 'monitor_timeout':
            return False
        self._action_outcome('Kết thúc trận', result.get('completed', False))
        if not result.get('completed',False):
            self.log('[Combat] Trận chưa xác nhận kết thúc; chưa chạy bảo trì làng.')
            return False
        self._scout_counted = False
        self._recovery_attempts = 0
        self._robot_restart_attempted = False
        builder = getattr(self, "builder", None)
        if builder is not None:
            builder.main_turn_until = 0
        self.stats['battles_completed']+=1
        self.stats['stars_earned']+=result.get('stars',0)
        duration=result.get('duration')
        record=result.get('time_to_50_ms')
        if type(record) is int and record>=5000 and type(duration) in (int,float) and record<=duration*1000+100:
            previous=self.stats.get('fastest_50_ms',0)
            self.stats['fastest_50_ms']=StatsStore.combine('fastest_50_ms',previous,record)
            if not previous or record<previous:
                self.log(f'[Kỷ lục] Đạt 50% trong {record/1000:.2f}s từ lần bắt đầu thả quân.')
        if type(duration) in (int,float) and 0 < duration <= 3600:
            self.stats['battle_duration_ms']=self.stats.get('battle_duration_ms',0)+round(duration*1000)
            self.stats['battle_duration_samples']=self.stats.get('battle_duration_samples',0)+1
        gained=result.get('loot_gained',{})
        for key,stat in [('gold','gold_looted'),('elixir','elixir_looted'),('dark_elixir','dark_looted')]:
            self.stats[stat]+=max(0,int(gained.get(key,0)))
        snapshot = self.get_stats()
        uptime = max(0, snapshot['uptime_seconds'] - snapshot['builder_uptime_seconds'])
        from core.recent_rph import append_battle
        append_battle(self.stats,gained,uptime)
        self.stats['rph_updated_uptime_seconds'] = uptime
        self._publish_stats()
        self.save_stats(force=True)
        self.log(f"[Combat] Kết thúc: {result.get('stars',0)} sao, {result.get('duration',0):.0f}s.")
        home = self._ensure_main_village(timeout_sec=25)
        self._action_outcome('Quay về làng', home)
        if home:
            if self.settings.get("main_village_enabled", True) and self._maybe_periodic_restart():
                return True
            if FarmFinishController.enabled(self.settings) and self.settings.get('auto_collect', True):
                screen = self.adb.screencap()
                if screen is not None and self.village.is_in_main_village(screen):
                    self.village.collect_resources(screen, log_callback=self.log)
            self._run_village_maintenance(force=True)
            return True
        self.log('[Village] Chưa xác nhận về làng; hoãn bảo trì.')
        return False

    def _ensure_main_village(self, timeout_sec: float = 25.0) -> bool:
        """Chờ và xác minh chắc chắn game đã quay về Làng chính sau trận đánh hoặc chuyển cảnh."""
        self.log("[Bot] Đang chờ và xác nhận quay về Làng chính an toàn...")
        start_wait = time.time()
        home_taps = 0
        while time.time() - start_wait < timeout_sec:
            time.sleep(1.0)
            screen = self.adb.screencap()
            if screen is None:
                continue

            if self.screen_cb:
                self.screen_cb(screen)

            home = self.village.is_in_main_village(screen)
            if (not home or self.vision.has_system_dialog(screen)) and self._queue_screen_fault(screen):
                return False
            if home:
                self.set_state(self.STATE_VILLAGE)
                self.village.deselect_all()
                self.log('[Village] Đã xác nhận màn hình làng.')
                return True

            # Nếu còn ở bảng kết quả (thấy Return Home), bấm tiếp để về
            home_match = self.vision.match_template(screen, "return_home", threshold=0.70, roi=(500, 650, 600, 200))
            if not home_match:
                home_match = self.vision.match_template(screen, "okhome", threshold=0.70, roi=(500, 650, 600, 200))
            if home_match:
                if home_taps >= 5:
                    self._request_recovery('Bấm về làng 5 lần chưa thành công')
                    return False
                home_taps += 1
                self.log("[Bot] Thấy nút Return Home trên màn hình kết quả -> Bấm quay về...")
                self.adb.tap(home_match[0], home_match[1], delay_after=2.0)
                continue

            # Đóng bất kỳ thông báo popup nào xuất hiện (sự kiện mùa giải, shop ưu đãi, thưởng sao, drawer chat)
            if self.village.close_popups_and_dialogs(screen):
                continue

            # Đóng hộp thoại Settings/Info nếu đang mở
            close_btn = self.vision.match_template(screen, "btn_close_x", threshold=0.75)
            if close_btn:
                self.adb.tap(close_btn[0], close_btn[1], delay_after=0.6)
                continue

            if self.village.is_in_main_village(screen):
                self.log("[Bot] ĐÃ VỀ LÀNG CHÍNH THÀNH CÔNG!")
                self.set_state(self.STATE_VILLAGE)
                self.village.deselect_all()
                time.sleep(0.5)
                return True

        self.log("[Bot] Hết thời gian chờ về làng, thử chạm nhẹ góc màn hình để bỏ qua...")
        self.village.deselect_all()
        final_screen = self.adb.screencap()
        return self.village.is_in_main_village(final_screen) if final_screen is not None else False

    def _sleep_wait_for_builders(self, wait_seconds: int, target_village='main') -> bool:
        """Close CoC, sleep until builder is free, with live countdown and clean stop responsiveness."""
        from datetime import datetime, timezone, timedelta

        raw_wait = max(1, int(wait_seconds))
        total_wait = max(15, raw_wait + 15)
        wake_timestamp = time.time() + total_wait
        wake_str = datetime.fromtimestamp(wake_timestamp, timezone(timedelta(hours=7))).strftime('%H:%M:%S ngày %d/%m')

        a_h, a_rem = divmod(raw_wait, 3600)
        a_m, a_s = divmod(a_rem, 60)
        actual_str = f"{a_h}h {a_m}m {a_s}s" if a_h > 0 else (f"{a_m}m {a_s}s" if a_m > 0 else f"{a_s}s")

        hours, rem = divmod(total_wait, 3600)
        mins, secs = divmod(rem, 60)
        dur_str = f"{hours}h {mins}m {secs}s" if hours > 0 else (f"{mins}m {secs}s" if mins > 0 else f"{secs}s")

        self.log(f"💤 [Kế hoạch] Đã full kho tài nguyên và hết thợ rảnh.")
        self.log(f"💤 [Kế hoạch] Thợ gần nhất còn {actual_str} (nghỉ {dur_str} tính cả đệm 15s, thức dậy lúc {wake_str}).")
        self.log(f"📱 [Hệ thống] Đang đóng game CoC để bảo vệ tài nguyên và nghỉ ngơi...")

        if hasattr(self.village, 'set_wall_wait') and callable(self.village.set_wall_wait):
            self.village.set_wall_wait(wake_timestamp, f"Đang ngủ chờ thợ rảnh (thức dậy lúc {wake_str}).")

        try:
            self.adb.kill_coc()
        except Exception as e:
            self.log(f"[Hệ thống] Lỗi khi đóng game CoC: {e}")

        self.set_state('CHỜ THỢ RẢNH')
        last_log_time = 0.0

        while time.time() < wake_timestamp:
            if not self._is_current_worker() or getattr(self, 'stop_requested', False):
                self.log("[Hệ thống] Nhận lệnh dừng bot trong lúc chờ thợ.")
                return False
            if self.is_paused:
                time.sleep(1.0)
                continue

            remaining = int(wake_timestamp - time.time())
            r_h, r_rem = divmod(remaining, 3600)
            r_m, r_s = divmod(r_rem, 60)
            status_text = f"Đang ngủ chờ thợ rảnh: còn {r_h:02d}:{r_m:02d}:{r_s:02d} (thức dậy {wake_str})"
            self.village.wall_status = status_text

            if time.time() - last_log_time >= 600:
                self.log(f"⏳ [Chờ thợ] Còn {r_h:02d}h {r_m:02d}m {r_s:02d}s nữa đến lượt thợ rảnh kế tiếp ({wake_str}).")
                last_log_time = time.time()

            time.sleep(1.0)

        self.log(f"⏰ [Hệ thống] Đã đến giờ thợ xây hoàn thành ({wake_str})! Đang khởi động lại game CoC...")
        self.set_state(self.STATE_RECOVERY)
        try:
            self.optimizer.restart_game()
            self.village._identity_verified_at = 0.0
        except Exception as e:
            self.log(f"[Hệ thống] Khởi động game CoC gặp lỗi: {e}")
            return False

        deadline = time.monotonic() + 40
        recovered = False
        while self._is_current_worker() and time.monotonic() < deadline:
            self._checkpoint()
            frame = self.adb.screencap()
            if frame is None:
                time.sleep(1)
                continue
            frame = self._ensure_game_resolution(frame)
            if frame is None:
                return False
            main = self.village.is_in_main_village(frame)
            night = self.builder.is_home(frame) if getattr(self, 'builder', None) else False
            if target_village == 'builder':
                recovered = night or (main and self.builder.enter(frame))
            else:
                recovered = main or (night and self.builder.leave(frame))
            if recovered:
                self.log(f'[Hệ thống] Đã tới {"làng đêm" if target_village == "builder" else "làng chính"} sau khi thức dậy.')
                break
            self.village.close_popups_and_dialogs(frame)
            time.sleep(1)
        if not recovered:
            self.log('[Hệ thống] Chưa tới được làng có thợ rảnh; chuyển sang chu trình khôi phục.')
            self.restart_game_and_recover(force=True)
            return False

        return True

    def _service_cross_village_sleep(self):
        builder = getattr(self, "builder", None)
        if builder is None:
            return False
        if getattr(getattr(builder, "battle", None), "in_progress", False):
            return False
        now = time.monotonic()
        main_enabled = self.settings.get("main_village_enabled", True)
        night_enabled = self.settings.get("builder_base_enabled", False)

        main_wait = getattr(builder, 'main_builders_wait_until', 0)
        night_wait = getattr(builder, 'night_builders_wait_until', 0)

        main_waiting = main_enabled and (now < main_wait)
        night_waiting = night_enabled and (now < night_wait)

        if main_enabled and night_enabled and main_waiting and night_waiting:
            if not (getattr(builder, 'main_schedule_checked', True)
                    and getattr(builder, 'night_schedule_checked', True)):
                return False
            main_rem = int(main_wait - now)
            night_rem = int(night_wait - now)
            shortest_sec = min(main_rem, night_rem)
            if shortest_sec >= 15:
                current = self.adb.screencap()
                here_night = self.builder.is_home(current) if current is not None else False
                next_village = 'builder' if night_rem < main_rem or (night_rem == main_rem and here_night) else 'main'
                self.log(f"💤 [Kế hoạch] Cả Làng chính (còn {main_rem//60}m) và Làng đêm (còn {night_rem//60}m) đều hết thợ rảnh.")
                target_str = "Làng đêm" if next_village == 'builder' else "Làng chính"
                self.log(f"💤 [Kế hoạch] Tạm đóng game nghỉ ngơi, thức dậy sau {shortest_sec//60} phút để xử lý {target_str}.")
                slept = self._sleep_wait_for_builders(shortest_sec, next_village)
                if slept:
                    if next_village == 'builder':
                        builder.night_builders_wait_until = 0
                    else:
                        builder.main_builders_wait_until = 0
                    return True
                return False

        if night_enabled and not main_enabled and night_waiting:
            if not getattr(builder, 'night_schedule_checked', True):
                return False
            night_rem = int(night_wait - now)
            if night_rem >= 15:
                self.log(f"💤 [Kế hoạch] Làng đêm hết thợ rảnh (thợ gần nhất còn {night_rem//60} phút).")
                slept = self._sleep_wait_for_builders(night_rem, 'builder')
                if slept:
                    builder.night_builders_wait_until = 0
                    return True
                return False

        return False

    def _defer_finished_main_to_builder(self, wait_seconds):
        """Keep Builder Base running while Home Village waits for its next builder."""
        builder = getattr(self, 'builder', None)
        if (builder is None or not self.settings.get('main_village_enabled', True)
                or not self.settings.get('builder_base_enabled', False)):
            return False
        seconds = wait_seconds if type(wait_seconds) is int and wait_seconds > 0 else 300
        builder.main_builders_wait_until = time.monotonic() + seconds + 15
        builder.main_schedule_checked = True
        builder.main_turn_until = 0
        if callable(getattr(builder, 'remember_builder_waits', None)):
            builder.remember_builder_waits()
        if type(wait_seconds) is int and wait_seconds > 0:
            self.log(f'[Điều phối] Làng chính full kho và hết thợ; kiểm tra lại sau khoảng {max(1, seconds//60)} phút. Tiếp tục làng đêm.')
        else:
            self.log('[Điều phối] Chưa đọc chắc giờ thợ làng chính; kiểm tra lại sau 5 phút. Tiếp tục làng đêm.')
        return True

    def _farm_finish_gate(self, screen, attack_menu=False):
        if not FarmFinishController.enabled(self.settings):
            controller = getattr(self, 'farm_finish', None)
            if isinstance(controller, FarmFinishController):
                controller.observe_home(self.settings, screen, self.log)
            return False
        if attack_menu:
            self._ensure_main_village()
            return True
        controller = getattr(self, 'farm_finish', None)
        if controller is None:
            controller = self.farm_finish = FarmFinishController(self.village)
        builder = getattr(self, 'builder', None)
        if (builder is not None and self.settings.get('farm_finish_wait_builders', False) is True
                and self.settings.get('main_village_enabled', True)
                and self.settings.get('builder_base_enabled', False)
                and controller.completed
                and time.monotonic() < getattr(builder, 'main_builders_wait_until', 0)):
            if self._service_cross_village_sleep():
                controller.completed = False
                controller.ready_reads = 0
            return True
        action = controller.observe_home(self.settings, screen, self.log)
        if (getattr(self.village, 'player_tag_failures', 0) >= 3
                and not getattr(self.village, '_session_legacy_key', None)):
            reason = getattr(self.village, 'player_tag_failure', None) or 'Không xác minh được mã người chơi'
            self.village.player_tag_failures = 0
            self.log(f'[Tài khoản] {reason} sau 3 lần và thử Share/Copy; tạm dừng để kiểm tra clipboard giả lập.')
            self._recovery_paused = True
            self.pause_bot()
            return True
        self.village.wall_status = controller.status
        self.set_state('HOÀN TẤT FARM')
        if action == 'farm':
            self.battle.farm_needed_resources = set(controller.missing)
            # Research and new construction can be started while filling the
            # farm-finish target; waiting for completely full stores can leave
            # the laboratory idle indefinitely.
            if ((self.settings.get('auto_upgrade_army', False) or self.settings.get('auto_buy_new_buildings', False))
                    and (getattr(self, '_optional_upgrade_retry', False) or
                         time.time() - self.last_village_maintenance >= self.settings.get('maintenance_interval_sec', 45))):
                optional = self._service_optional_upgrades(self.adb.screencap())
                self.last_village_maintenance = time.time()
                self._optional_upgrade_retry = optional['status'] in ('blocked', 'uncertain')
                if optional['status'] == 'needs_resources':
                    controller.extra_resources.update(optional.get('resources', ()))
                    return True
                if optional['status'] in ('progress', 'uncertain', 'blocked'):
                    return True
                self.battle.farm_needed_resources = set(controller.missing)
            return False
        if action == 'complete':
            if time.monotonic() - getattr(self, '_completed_status_scan_at', 0) >= 30:
                frame = self.adb.screencap()
                if frame is not None and self.village.is_in_main_village(frame):
                    self.village_state.update_resources('main', self.vision.read_village_resources(frame))
                    controller.suggested_upgrader().observe_home_builders(frame)
                    self._completed_status_scan_at = time.monotonic()
            optional = self._service_optional_upgrades(self.adb.screencap())
            if optional['status'] == 'needs_resources':
                controller.extra_resources.update(optional.get('resources', ()))
                controller.force_full = True
                controller.completed = False
                controller.ready_reads = 0
                return True
            if optional['status'] == 'progress':
                controller.completed = False
                controller.ready_reads = 0
                return True
            if optional['status'] != 'idle':
                return True
            result = controller.assign_builders(self.log)
            self.village.wall_status = controller.status
            if result == 'farm':
                return True  # recompute targets on a fresh home frame next iteration
            if result == 'wait':
                return True
            if result == 'blocked':
                now = time.monotonic()
                reason = controller.status
                if (reason != getattr(self, '_builder_blocked_reason', None)
                        or now >= getattr(self, '_builder_blocked_log_at', 0)):
                    self.log('[Thợ] Chưa thể tiếp tục phân công: ' + reason)
                    self._builder_blocked_reason = reason
                    self._builder_blocked_log_at = now + 30
                return True

            wait_builders_enabled = self.settings.get('farm_finish_wait_builders', False) is True
            status_msg = str(getattr(controller, 'status', '') or '')
            no_matching_buildings = ('Không tìm thấy công trình nào khớp bộ lọc' in status_msg)
            if wait_builders_enabled and not no_matching_buildings and (result == 'done' or 'Tạm dừng' in status_msg):
                upgrader = controller.suggested_upgrader()
                shortest_sec = upgrader.get_shortest_ongoing_builder_seconds() if upgrader is not None else None
                if not (type(shortest_sec) is int and shortest_sec > 0):
                    if callable(getattr(self.village, 'get_account_shortest_builder_seconds', None)):
                        saved_sec = self.village.get_account_shortest_builder_seconds()
                        if type(saved_sec) is int and saved_sec > 0:
                            shortest_sec = saved_sec
                if type(shortest_sec) is int and shortest_sec > 0:
                    if self._defer_finished_main_to_builder(shortest_sec):
                        if self._service_cross_village_sleep():
                            controller.completed = False
                            controller.ready_reads = 0
                        return True
                    slept_ok = self._sleep_wait_for_builders(shortest_sec)
                    if slept_ok:
                        controller.completed = False
                        controller.ready_reads = 0
                        return True
                    else:
                        self.stop_bot()
                        return True
                if self._defer_finished_main_to_builder(shortest_sec):
                    return True

            # All regular builders must be confirmed busy before auto-closing.
            self.log('[Hoàn tất] '+controller.status)
            self.settings.set('farm_finish_enabled', False)
            if self.settings.get("builder_base_enabled", False):
                self.settings.set("main_village_enabled", False)
                self.set_state(self.STATE_IDLE)
                return True
            self.stop_bot()
            callback = getattr(self, 'completion_callback', None)
            if result == 'done' and self.settings.get('farm_finish_close_app', True) and callable(callback):
                callback()
            return True
        return True

    def _wall_hold_gate(self, screen, attack_menu=False):
        """Return True when new battles must wait; never interrupts combat."""
        guard = getattr(self, '_wall_farm_guard', None)
        if guard is None:
            guard = self._wall_farm_guard = WallFarmGuard()
        if (FarmFinishController.enabled(self.settings) or getattr(self, '_builders_need_assignment', False)
                or getattr(self, '_upgrade_resource_hold', False)):
            guard.clear()
            return False
        if getattr(self.village, 'walls_confirmed_maxed', None) is True:
            guard.clear()
            return False
        enabled = (self.settings.get('auto_upgrade_walls', True)
                   and self.settings.get('wall_hold_enabled', True))
        if not enabled:
            guard.clear()
            return False
        result = getattr(self.village, 'wall_result', {})
        if not isinstance(result, dict):
            result = {}
        if result.get('status') == 'maxed':
            guard.clear()
            return False
        if attack_menu:
            if guard.active or result.get('status') in WallFarmGuard.FAILURES:
                # Menu overlays cannot supply trustworthy home resource OCR.
                # Return home, then let the next iteration run maintenance.
                self._ensure_main_village()
                return True
            return False
        if screen is None or not self.village.is_in_main_village(screen):
            return guard.active
        readings = self.vision.read_village_resources(screen)
        was_active = guard.active
        if not guard.observe(self.settings, readings, result):
            if was_active:
                self.log('[Wall] Tài nguyên dưới 110% qua hai lần đọc; tiếp tục đánh.')
            return False
        self.set_state(self.STATE_WALL_HOLD)
        now = time.time()
        retry_delay = 5 if result.get('status') == 'transaction_pending' else 30
        if not was_active:
            # The triggering maintenance has already used its recovery budget.
            # An uncertain purchase needs prompt verification; ordinary
            # selection failures keep the slower recovery interval.
            guard.next_retry = now + retry_delay
            self.log('[Wall] Giữ làng — chờ nâng tường: ' + str(result.get('reason', 'Chưa nâng được')))
        elif result.get('status') == 'transaction_pending':
            guard.next_retry = min(guard.next_retry, now + retry_delay)
        retry_at = getattr(self.village, '_wall_builder_retry_at', 0)
        if not isinstance(retry_at, (int, float)):
            retry_at = 0
        if guard.next_retry <= now < retry_at and guard.low_reads == 0:
            if self.village.refresh_wall_builder_wait(screen) is True:
                retry_at = 0
            else:
                guard.next_retry = min(retry_at, now + retry_delay)
        if now >= max(guard.next_retry, retry_at) and guard.low_reads == 0:
            guard.next_retry = now + retry_delay
            # Never convert an invalid resource reading into a spendable zero.
            if all(readings.get('valid', {}).get(name) is True for name in guard.resources):
                self.log('[Wall] Giữ làng: thử nâng lại; ' + str(result.get('reason', '')))
                self.village.upgrade_walls(screen, readings.get('gold', 0)
                                           if readings.get('valid', {}).get('gold') else 0,
                                           readings.get('elixir', 0)
                                           if readings.get('valid', {}).get('elixir') else 0,
                                           log_callback=self.log)
            next_status = getattr(self.village, 'wall_result', {})
            next_delay = 5 if isinstance(next_status, dict) and next_status.get('status') == 'transaction_pending' else 30
            guard.next_retry = time.time() + next_delay
            self.log('[Wall] Giữ làng — chờ nâng tường: ' + self.village.wall_status)
        return True

    def _maintain_free_builders(self, screen):
        controller = getattr(self, 'farm_finish', None)
        if not isinstance(controller, FarmFinishController):
            return
        worker = controller.suggested_upgrader()
        is_maxed = getattr(self.village, 'walls_confirmed_maxed', False)
        reserve = 0 if is_maxed else int(self.settings.get('reserve_builders', 0))
        custom_buildings = bool(self.settings.get('auto_upgrade_buildings', False))
        cached = getattr(self, '_home_builder_snapshot', None)
        account = getattr(self.village, 'current_account_key', None)
        if cached and cached[2] == account and time.monotonic() - cached[0] < 3:
            free = cached[1]
        else:
            free = worker.observe_home_builders(screen)
        if free is None:
            if custom_buildings:
                self.log('[Thợ] Chưa đọc chắc số thợ thường; hoãn tìm trận để kiểm tra lại.')
                self._defer_battle_after_maintenance = True
            return
        builder = getattr(self, 'builder', None)
        if (builder is not None and self.settings.get('builder_base_enabled', False)
                and self.settings.get('main_village_enabled', True)):
            if free == 0:
                if time.monotonic() >= getattr(builder, 'main_builders_wait_until', 0):
                    try:
                        seconds = worker.get_shortest_ongoing_builder_seconds()
                    except BotStoppedError:
                        raise
                    except Exception as exc:
                        seconds = None
                        self.log(f'[Thợ] Chưa đọc được thời gian thợ làng chính ({type(exc).__name__}); kiểm tra lại sau 5 phút.')
                    seconds = seconds + 10 if type(seconds) is int and seconds > 0 else 300
                    builder.main_builders_wait_until = time.monotonic() + seconds
                    builder.main_schedule_checked = True
                    builder.main_turn_until = 0
                    if callable(getattr(builder, 'remember_builder_waits', None)):
                        builder.remember_builder_waits()
                    self.log(f'[Thợ] Làng chính hết thợ rảnh; ưu tiên làng đêm, kiểm tra thợ lại sau khoảng {max(1, seconds//60)} phút.')
                    if self._service_cross_village_sleep():
                        return True
            else:
                builder.main_builders_wait_until = 0
                builder.main_schedule_checked = True
                if callable(getattr(builder, 'remember_builder_waits', None)):
                    builder.remember_builder_waits()
                # A maxed wall alone does not mean the village is maxed.
                # Only the builder menu's explicit completion message qualifies.
                now = time.monotonic()
                if is_maxed and now >= getattr(builder, '_home_max_scan_after', 0):
                    builder._home_max_scan_after = now + 1800
                    try:
                        if worker.ui.ensure_menu_open():
                            frame = self.adb.screencap()
                            words = worker.ui.ocr_positions(frame, (1180, 55, 415, 740), scale=1)
                            text = re.sub(r'[^a-z0-9]', '', ''.join(str(word[0]).lower() for word in words))
                            worker.ui.close_dialog()
                            if 'allupgradescomplete' in text:
                                builder.main_builders_wait_until = time.monotonic() + 1800
                                builder.main_turn_until = 0
                                self.log('[Thợ] Làng chính báo hoàn tất toàn bộ nâng cấp; ưu tiên làng đêm và kiểm tra lại sau 30 phút.')
                                return
                    except BotStoppedError:
                        raise
                    except Exception as exc:
                        self.log(f'[Thợ] Chưa xác nhận được trạng thái max làng chính ({type(exc).__name__}).')
        if free <= reserve:
            self._builders_need_assignment = False
            if custom_buildings:
                self.log(f'[Thợ] Có {free} thợ thường rảnh, mức dự phòng {reserve}; chưa nâng công trình.')
            return
        if worker.pending is not None:
            self.log('[Thợ] Giao dịch công trình trước chưa được xác minh; hoãn tìm trận để tránh bỏ sót.')
            self._defer_battle_after_maintenance = True
            return
        if custom_buildings:
            self.log(f'[Thợ] Có {free} thợ thường rảnh, dự phòng {reserve}; đang tìm công trình nâng cấp.')
            self._builders_need_assignment = True
            wait_mode = self.settings.get('building_upgrade_wait_mode', 'target_cost')
            readings = self.vision.read_village_resources(screen) if screen is not None else {}
            if wait_mode == 'full_storage':
                res_cfg = self.settings.get('building_upgrade_resources') or {'gold': True, 'elixir': True}
                check_res = tuple(r for r in ('gold', 'elixir') if res_cfg.get(r, True)) or ('gold', 'elixir')
                target_pct = max(90, min(100, int(self.settings.get('farm_finish_target_percent', 100))))
                caps = controller.capacity_reader.read(check_res, self.log)
                if caps is None:
                    worker.last_status = f'Đang chờ đọc dung lượng kho trước khi giao {free - reserve} thợ.'
                    self.log('[Thợ] ' + worker.last_status)
                    return False
                fresh_scr = self.adb.screencap()
                readings = self.vision.read_village_resources(fresh_scr) if fresh_scr is not None else readings
                missing = {
                    r for r in check_res
                    if readings.get('valid', {}).get(r) is True and readings.get(r, 0) * 100 < caps[r] * target_pct
                }
                if missing:
                    if hasattr(self, 'battle') and self.battle is not None:
                        self.battle.farm_needed_resources = set(missing)
                    worker.last_status = (
                        f'Chế độ đợi đầy kho ({target_pct}%): còn {free - reserve} thợ cần giao; '
                        f'tạm dừng nâng tường để farm đầy {", ".join(sorted(missing))}.'
                    )
                    self.log('[Thợ] ' + worker.last_status)
                    return False
            elif wait_mode == 'target_cost' and isinstance(worker.target_candidate, dict):
                t_res = worker.target_candidate.get('resource')
                t_cost = worker.target_candidate.get('cost', 0)
                t_name = worker.target_candidate.get('name', 'công trình')
                if (t_res and readings.get('valid', {}).get(t_res) is True
                        and readings.get(t_res, 0) < t_cost):
                    if hasattr(self, 'battle') and self.battle is not None:
                        self.battle.farm_needed_resources = {t_res}
                    worker.last_status = (
                        f'Đang tích {t_res} cho {t_name}: {readings.get(t_res, 0):,}/{t_cost:,} '
                        f'(còn {free - reserve} thợ cần giao); tạm dừng nâng tường.'
                    )
                    self.log('[Thợ] ' + worker.last_status)
                    return False
        # Each pass re-reads the regular-builder counter before spending.
        attempted = False
        remaining_free = free
        for _ in range(free - reserve):
            if not self.settings.get('main_village_enabled', True):
                break
            attempted = True
            result = worker.step(self.log, maintain_reserve=True)
            self.log('[Thợ] ' + result['reason'])
            if result['status'] == 'progress':
                assigned = result.get('count', 1)
                remaining_free = max(0, remaining_free - assigned)
                if remaining_free <= reserve:
                    self._builders_need_assignment = False
                    break
            elif result['status'] == 'done':
                self._builders_need_assignment = False
                break
            else:
                if custom_buildings and result['status'] == 'needs_resources':
                    self._builders_need_assignment = True
                    if hasattr(self, 'battle') and self.battle is not None and result.get('resources'):
                        self.battle.farm_needed_resources = set(result['resources'])
                elif result['status'] in ('blocked', 'idle') and not result.get('resources'):
                    # No valid candidates left at all / specific target not found -> do not block wall upgrades indefinitely
                    self._builders_need_assignment = False
                break
        return attempted

    @staticmethod
    def _new_only_mode(settings):
        if not settings.get('auto_buy_new_buildings', False):
            return False
        other_tasks = ('auto_collect', 'auto_train_army', 'auto_request_troops',
                       'auto_collect_treasury', 'auto_assign_apprentice',
                       'auto_upgrade_walls', 'auto_upgrade_buildings',
                       'auto_upgrade_army', 'farm_finish_enabled', 'builder_base_enabled')
        return not any(settings.get(key, False) for key in other_tasks)

    @staticmethod
    def _new_scan_snapshot(readings, free, reserve):
        valid = readings.get('valid', {})
        resources = tuple((name, readings.get(name) if valid.get(name) is True else None)
                          for name in ('gold', 'elixir', 'dark_elixir'))
        return free, reserve, resources

    def _service_optional_upgrades(self, fresh):
        self._upgrade_resource_hold = False
        builder = getattr(self, 'builder', None)
        if builder is not None and builder.is_home(fresh):
            return dict(status='blocked', reason='Đang ở làng đêm; chờ về làng chính để kiểm tra nâng quân và công trình.')
        # Research has first claim on shared elixir; construction and walls
        # must not consume it while the selected research target is pending.
        worker = self.farm_finish.suggested_upgrader()
        account = getattr(self.village, 'current_account_key', None)
        lab_busy = (account == getattr(self, '_army_lab_busy_account', None)
                    and time.monotonic() < getattr(self, '_army_lab_busy_until', 0))
        if self.settings.get('auto_upgrade_army', False) and not lab_busy:
            from bot.army_upgrades import ArmyUpgrader
            if not hasattr(self, '_army_upgrader'):
                self._army_upgrader = ArmyUpgrader(worker)
            if time.monotonic() < getattr(self, '_army_upgrade_retry_at', 0):
                return dict(status='blocked', reason='Đang chờ kiểm tra lại nâng quân.')
            self._army_upgrader.log = self.log
            hud_reader = getattr(self._army_upgrader, 'home_lab_status', None)
            hud_status = hud_reader(fresh) if callable(hud_reader) else None
            if hud_status in ('goblin', 'busy'):
                second_frame = self.adb.screencap()
                confirmed = (self.village.is_in_main_village(second_frame)
                             and hud_reader(second_frame) == hud_status)
            else:
                confirmed = False
            if confirmed:
                result = dict(status='idle', lab_busy=True,
                              reason='Lab thường đang bận theo hai ảnh làng; bỏ qua menu Army Upgrades.')
            else:
                result = self._army_upgrader.step()
            self._army_upgrade_retry_at = 0
            if result.get('lab_busy'):
                self._army_lab_busy_account = account
                self._army_lab_busy_until = time.monotonic() + 300
            self.log('[Quân] ' + result['reason'])
            if result['status'] in ('needs_resources', 'blocked', 'uncertain'):
                if result.get('resources'):
                    self.battle.farm_needed_resources = set(result['resources'])
                    self._upgrade_resource_hold = True
                self.last_village_maintenance = time.time()
                return result
            if result['status'] == 'progress':
                self.last_village_maintenance = time.time()
                return result
            fresh = self.adb.screencap()
            if not self.village.is_in_main_village(fresh):
                return dict(status='blocked', reason='Chưa trở về làng chính.')
        no_new_cached = (account == getattr(self, '_new_scan_empty_account', None)
                         and time.monotonic() < getattr(self, '_new_scan_empty_until', 0))
        readings = None
        free = None
        reserve = None
        if no_new_cached and getattr(self, '_new_scan_empty_snapshot', None) is not None:
            readings = self.vision.read_village_resources(fresh)
            free = worker.observe_home_builders(fresh)
            reserve = (0 if (FarmFinishController.enabled(self.settings)
                             or getattr(self.village, 'walls_confirmed_maxed', False))
                       else int(self.settings.get('reserve_builders', 0)))
            no_new_cached = (BotEngine._new_scan_snapshot(readings, free, reserve)
                             == self._new_scan_empty_snapshot)
        if self.settings.get('auto_buy_new_buildings', False) and not no_new_cached:
            from bot.new_buildings import NewBuildingBuyer
            if not hasattr(self, '_new_building_buyer'):
                self._new_building_buyer = NewBuildingBuyer(worker)
            # Read the resource HUD and the ordinary-builder HUD from the
            # same home frame; share the fresh builder count with maintenance.
            if readings is None:
                readings = self.vision.read_village_resources(fresh)
            self.village_state.update_resources('main', readings)
            if free is None:
                free = worker.observe_home_builders(fresh)
            self._home_builder_snapshot = (time.monotonic(), free, account)
            if free is None:
                self.log('[Thợ] Chưa đọc được số thợ từ ảnh làng; tiếp tục các việc khác.')
                return dict(status='idle', reason='Chưa đọc được số thợ rảnh.')
            valid = readings.get('valid', {})
            shown = ', '.join(f'{r}={readings[r]:,}' for r in ('gold', 'elixir', 'dark_elixir')
                              if valid.get(r) is True)
            self.log(f'[Làng] Thợ thường rảnh: {free}; tài nguyên: {shown or "chưa đọc được"}.')
            if reserve is None:
                reserve = (0 if (FarmFinishController.enabled(self.settings)
                                 or getattr(self.village, 'walls_confirmed_maxed', False))
                           else int(self.settings.get('reserve_builders', 0)))
            handoff = (bool(self.settings.get('auto_upgrade_buildings', False)) and free > reserve
                       and self.settings.get('building_upgrade_wait_mode', 'target_cost') == 'target_cost'
                       and worker.pending is None)
            result = self._new_building_buyer.step(self.log, free, reserve, keep_menu_open=handoff)
            if result.get('no_eligible') or result.get('no_new'):
                self._new_scan_empty_account = account
                self._new_scan_empty_until = time.monotonic() + 300
                self._new_scan_empty_snapshot = (None if result.get('no_new') else
                                                 BotEngine._new_scan_snapshot(readings, free, reserve))
                # A later wall/treasury branch may exit maintenance early. Keep
                # the normal maintenance interval instead of reopening New each loop.
                self.last_village_maintenance = time.time()
            self.log('[Công trình mới] ' + result['reason'])
            if result.get('no_eligible') and BotEngine._new_only_mode(self.settings):
                self.log('[Công trình mới] Không còn mục New phù hợp; tạm dừng vì không bật công việc khác.')
                self.pause_bot()
                return dict(status='paused', reason=result['reason'])
            if result.get('preopened'):
                self.log('[Thợ] Dùng lại menu đang mở để kiểm tra nâng công trình.')
                result = worker.step(self.log, maintain_reserve=not FarmFinishController.enabled(self.settings),
                                     preopened=result['preopened'])
                self.log('[Thợ] ' + result['reason'])
                self._builders_need_assignment = result['status'] == 'needs_resources'
                if result['status'] == 'done':
                    result = dict(status='idle', reason=result['reason'])
            if result['status'] in ('needs_resources', 'blocked', 'uncertain'):
                if result.get('resources'):
                    self.battle.farm_needed_resources = set(result['resources'])
                    self._upgrade_resource_hold = True
                self.last_village_maintenance = time.time()
                return result
            if result['status'] == 'progress':
                self.last_village_maintenance = time.time()
                return result
            fresh = self.adb.screencap()
            if not self.village.is_in_main_village(fresh):
                return dict(status='blocked', reason='Chưa trở về làng chính.')
        if self.settings.get('auto_upgrade_army', False) or self.settings.get('auto_buy_new_buildings', False):
            self.battle.farm_needed_resources = set()

        return dict(status='idle', reason='Không có mục tùy chọn cần xử lý.')

    def _service_apprentice(self, screen):
        if self.settings.get('auto_assign_apprentice', False) and self.village.assign_apprentice(screen, self.log):
            return self.adb.screencap()
        return screen

    def _run_village_maintenance(self, force: bool = False):
        """
        Chu trình tự động hóa chăm sóc Làng chính:
        1. Tự động luyện quân nhanh (Quick Train)
        2. Tự động thu hoạch mỏ Vàng & Dầu
        3. Tự động xin lính Clan
        4. Tự động nâng cấp Tường khi dư tài nguyên
        """
        force = force or getattr(self, '_force_builder_maintenance', False)
        self._force_builder_maintenance = False
        self._defer_battle_after_maintenance = False
        if not self.settings.get("main_village_enabled", True):
            return
        now = time.time()
        if FarmFinishController.enabled(self.settings):
            # Walls are explicitly confirmed complete in this opt-in mode.
            return
        guard = getattr(self, '_wall_farm_guard', None)
        result = getattr(self.village, 'wall_result', {})
        # Run this before the maintenance interval and before collections.
        if ((guard is not None and guard.active)
                or (isinstance(result, dict) and result.get('status') in WallFarmGuard.FAILURES)):
            if self._wall_hold_gate(self._service_apprentice(self.adb.screencap())):
                return
        if not force and now - self.last_village_maintenance < self.settings.get("maintenance_interval_sec",45):
            return

        screen = self.adb.screencap()
        if screen is None:
            return

        if not self.village.is_in_main_village(screen):
            # Thử đóng popup nếu có
            if self.village.close_popups_and_dialogs(screen):
                time.sleep(0.8)
                screen = self.adb.screencap()

        if screen is None or not self.village.is_in_main_village(screen):
            self.log("[Village] Chưa xác nhận được Làng chính -> Bỏ qua chu trình chăm sóc.")
            return

        self.log("[Village] Kiểm tra thu hoạch, đội quân và tường.")
        if hasattr(self.village, 'check_and_sync_account'):
            self.village.check_and_sync_account(screen, self.log)
        self.village.deselect_all()

        # 1. Tự động luyện quân nhanh (Quick Train)
        if self.settings.get("auto_train_army", True):
            self.log("[Village] Mở đội quân nhanh theo thiết lập.")
            self.village.train_army(force=force)

        fresh=self.adb.screencap()
        if fresh is None or not self.village.is_in_main_village(fresh):
            self.log('[Village] Chưa xác nhận màn hình làng sau thao tác; dừng lượt bảo trì.')
            return

        # 2. Thu hoạch mỏ tài nguyên (Vàng & Dầu)
        if (self.settings.get("auto_collect", True)
                and self.village.collect_resources(fresh, log_callback=self.log)):
            fresh = self.adb.screencap()
            if not self.village.is_in_main_village(fresh):
                self.log('[Village] Màn hình đã đổi sau thu hoạch; dừng lượt bảo trì.')
                return

        # 3. Tự động xin lính Clan (nếu bật)
        if self.settings.get('auto_collect_treasury',False):
            self.village.observe_reward_timers(fresh,log_callback=self.log)
            self.village.collect_treasury(log_callback=self.log)
            fresh = self.adb.screencap()
            if not self.village.is_in_main_village(fresh):
                self.log('[Treasury] Chưa xác nhận về làng; hoãn các thao tác bảo trì còn lại.')
                return

        if self.settings.get("auto_request_troops", False):
            self.log("[Village] Tự động xin lính Clan Castle...")
            self.village.request_clan_troops()
            fresh = self.adb.screencap()
            if not self.village.is_in_main_village(fresh):
                self.log('[Village] Chưa xác nhận màn hình làng sau khi xin lính; dừng lượt bảo trì.')
                return

        optional_result = self._service_optional_upgrades(fresh)
        if optional_result['status'] != 'idle':
            if optional_result['status'] in ('blocked', 'uncertain', 'progress'):
                self._defer_battle_after_maintenance = True
            return
        fresh = self.adb.screencap()
        if not self.village.is_in_main_village(fresh):
            self.log('[Thợ] Chưa xác nhận về làng sau kiểm tra công trình mới; hoãn tìm trận để kiểm tra thợ.')
            self._defer_battle_after_maintenance = True
            return

        ran_builder_actions = self._maintain_free_builders(fresh)
        if ran_builder_actions:
            fresh = self.adb.screencap()
            if not self.village.is_in_main_village(fresh):
                return
        builder = getattr(self, 'builder', None)
        if (builder is not None and self.settings.get('builder_base_enabled', False)
                and time.monotonic() < getattr(builder, 'main_builders_wait_until', 0)
                and time.monotonic() >= getattr(builder, 'night_builders_wait_until', 0)
                and not getattr(builder, 'all_upgrades_complete', False)):
            self._defer_battle_after_maintenance = True
            return
        if self.settings.get('auto_assign_apprentice', False):
            fresh = self._service_apprentice(fresh)
            if not self.village.is_in_main_village(fresh):
                return

        # 4. Tự động nâng cấp tường (Auto Wall Upgrade)
        upgrade_walls = self.settings.get("auto_upgrade_walls", True)
        if hasattr(self, 'village_state') or upgrade_walls:
            res = self.vision.read_village_resources(fresh)
            if hasattr(self, 'village_state'):
                self.village_state.update_resources('main', res)
            if upgrade_walls:
                valid = res.get('valid', {})
                gold = f"{res['gold']:,}" if valid.get('gold') else 'chưa đọc được'
                elixir = f"{res['elixir']:,}" if valid.get('elixir') else 'chưa đọc được'
                self.log(f'[Village] Vàng: {gold} | Dầu: {elixir}')
                if getattr(self.village, 'walls_confirmed_maxed', False):
                    self.village.wall_status = 'Đã xác nhận Max tường qua Layout Editor; sử dụng hết số thợ xây khả dụng, tiếp tục farm full tài nguyên.'
                    self.village.wall_result = dict(status='maxed', reason=self.village.wall_status)
                    self.log('[Wall] ' + self.village.wall_status)
                elif getattr(self, '_builders_need_assignment', False) and self.settings.get('auto_upgrade_buildings', False):
                    reserve = int(self.settings.get('reserve_builders', 0))
                    self.village.wall_status = (
                        f'Tạm dừng nâng tường (cả vàng & dầu) để tích tài nguyên phân công thợ xây (duy trì {reserve} thợ rảnh).'
                    )
                    self.village.wall_result = dict(status='paused_for_builders', reason=self.village.wall_status)
                    self.log('[Wall] ' + self.village.wall_status)
                elif res['gold'] > 0 or res['elixir'] > 0:
                    self.village.upgrade_walls(fresh, res['gold'], res['elixir'], log_callback=self.log)

        fresh=self.adb.screencap()
        if fresh is None or not self.village.is_in_main_village(fresh):
            self.log('[Village] Chưa xác nhận màn hình làng sau thao tác; dừng lượt bảo trì.')
            return

        if self._wall_hold_gate(fresh):
            return

        self.last_village_maintenance = time.time()
        self.village.deselect_all()
        self.log("[Village] Hoàn tất bảo trì.")
