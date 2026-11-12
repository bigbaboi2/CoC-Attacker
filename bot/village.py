import time
import random
import re
import subprocess
from urllib.parse import urlparse, parse_qs, unquote
import cv2
import numpy as np
import json
from pathlib import Path
from typing import Optional, Tuple, Dict
from core.adb_client import ADBSocketClient, BotStoppedError
from core.vision import VisionEngine
from config.settings import BotSettings

class VillageManager:
    """
    Manages all Main Village base automation:
    - Base state verification
    - Resource collection from collectors and mines
    - Army Quick Training
    - Wall upgrades
    - Closing popup dialogs
    """

    # Dung lượng kho tối đa chính thức theo cấp Town Hall (Gold / Elixir / Dark Elixir)
    TH_MAX_STORAGE_CAPACITIES = {
        7:  {'gold': 4_000_000,  'elixir': 4_000_000,  'dark_elixir': 20_000},
        8:  {'gold': 6_000_000,  'elixir': 6_000_000,  'dark_elixir': 80_000},
        9:  {'gold': 8_000_000,  'elixir': 8_000_000,  'dark_elixir': 140_000},
        10: {'gold': 8_500_000,  'elixir': 8_500_000,  'dark_elixir': 200_000},
        11: {'gold': 10_000_000, 'elixir': 10_000_000, 'dark_elixir': 200_000},
        12: {'gold': 12_000_000, 'elixir': 12_000_000, 'dark_elixir': 240_000},
        13: {'gold': 18_000_000, 'elixir': 18_000_000, 'dark_elixir': 300_000},
        14: {'gold': 20_000_000, 'elixir': 20_000_000, 'dark_elixir': 300_000},
        15: {'gold': 22_000_000, 'elixir': 22_000_000, 'dark_elixir': 340_000},
        16: {'gold': 24_000_000, 'elixir': 24_000_000, 'dark_elixir': 380_000},
        17: {'gold': 28_000_000, 'elixir': 28_000_000, 'dark_elixir': 420_000},
        18: {'gold': 31_000_000, 'elixir': 31_000_000, 'dark_elixir': 500_000},
    }

    def __init__(self, adb: ADBSocketClient, vision: VisionEngine, settings: BotSettings, village_state=None):
        self.village_state = village_state
        self.adb = adb
        self.vision = vision
        self.settings = settings
        self.last_collect_time = None
        self.last_train_time = 0.0
        self._treasury_next_check = 0.0
        self._treasury_collector = None
        self._apprentice = None
        self.wall_status = 'Chưa kiểm tra nâng tường.'
        self.wall_result = dict(status='not_checked', reason=self.wall_status)
        self._wall_builder_retry_at = 0.0
        self._wall_builder_notice_at = 0.0
        self._lowest_wall_target = None
        self.walls_confirmed_maxed = False
        self._wall_no_target_count = 0
        self.verified_maxed_accounts = {}
        self.account_capacities = {}
        self.account_builder_timers = {}
        self.current_account_key = None
        self._verified_header_key = None
        self._session_player_tag = None
        self.player_tag_failure = None
        self.player_tag_failures = 0
        config_path = getattr(settings, 'filepath', None)
        self._wall_memory_path = Path(config_path).resolve().with_name('wall_state.json') if isinstance(config_path, (str, Path)) else None
        if self._wall_memory_path is not None:
            try:
                saved = json.loads(self._wall_memory_path.read_text(encoding='utf-8'))
                if saved.get('device_serial') == settings.get('device_serial'):
                    self.verified_maxed_accounts = saved.get('verified_maxed_accounts', {})
                    if isinstance(saved.get('account_capacities'), dict):
                        self.account_capacities = saved['account_capacities']
                    if isinstance(saved.get('account_builder_timers'), dict):
                        self.account_builder_timers = saved['account_builder_timers']
                    curr_acc = saved.get('current_account')
                    self.current_account_key = curr_acc
                    for acc_k, acc_info in list(self.verified_maxed_accounts.items()):
                        if isinstance(acc_info, dict) and isinstance(acc_info.get('th_level'), int):
                            th_lvl = acc_info['th_level']
                            if th_lvl in self.TH_MAX_STORAGE_CAPACITIES and acc_k not in self.account_capacities:
                                self.account_capacities[acc_k] = dict(
                                    self.TH_MAX_STORAGE_CAPACITIES[th_lvl],
                                    th_level=th_lvl,
                                    source='th_verified',
                                    updated_at=acc_info.get('verified_at', time.time()),
                                )
                    target = saved.get('wall_target')
                    if self.valid_wall_target(target):
                        self._lowest_wall_target = target
                    if curr_acc and self.verified_maxed_accounts.get(curr_acc):
                        self.walls_confirmed_maxed = True
                    elif saved.get("walls_confirmed_maxed"):
                        if curr_acc:
                            self.verified_maxed_accounts[curr_acc] = True
                        self.walls_confirmed_maxed = True
                    retry = saved.get('builder_retry_at', 0)
                    if isinstance(retry, (int, float)) and time.time() < retry < time.time()+31*86400:
                        self._wall_builder_retry_at = float(retry)
                        self.wall_status = str(saved.get('reason', 'Đang chờ thợ thường rảnh.'))
            except (OSError, ValueError, TypeError, AttributeError):
                pass

    def set_wall_wait(self, retry_at, reason):
        self._wall_builder_retry_at = float(retry_at)
        self.wall_status = reason
        if self._wall_memory_path is not None:
            from core.stats_store import StatsStore
            payload = dict(device_serial=self.settings.get('device_serial'),
                           current_account=self.current_account_key,
                           verified_maxed_accounts=self.verified_maxed_accounts,
                           account_capacities=self.account_capacities,
                           account_builder_timers=self.account_builder_timers,
                           builder_retry_at=retry_at, reason=reason,
                           wall_target=self._lowest_wall_target,
                           walls_confirmed_maxed=bool(getattr(self, "walls_confirmed_maxed", False)))
            try:
                StatsStore._atomic_write(self._wall_memory_path, json.dumps(payload, ensure_ascii=False, indent=2, default=StatsStore.json_default).encode('utf-8'))
            except (OSError, TypeError, ValueError):
                self.wall_status += ' (chưa lưu được thời điểm chờ)'

    def get_account_th_level(self, acc_key: Optional[str] = None) -> Optional[int]:
        acc_key = acc_key if isinstance(acc_key, str) and acc_key else (
            self.current_account_key if isinstance(self.current_account_key, str) else None
        )
        if acc_key:
            v_info = self.verified_maxed_accounts.get(acc_key)
            if isinstance(v_info, dict) and isinstance(v_info.get('th_level'), int):
                return v_info['th_level']
            c_info = self.account_capacities.get(acc_key)
            if isinstance(c_info, dict) and isinstance(c_info.get('th_level'), int):
                return c_info['th_level']
        if acc_key is None or acc_key == self.current_account_key:
            v_state = getattr(self, 'village_state', None)
            th_lvl = getattr(v_state, 'th_level', None) if v_state is not None else None
            if isinstance(th_lvl, int) and th_lvl >= 1:
                return th_lvl
        return None

    def get_account_capacities(self, resources, acc_key: Optional[str] = None) -> Optional[Dict[str, int]]:
        """Only trust directly measured storage values bound to a player tag."""
        key = acc_key or self.current_account_key
        if not isinstance(key, str) or not re.fullmatch(r'#[0289PYLQGRJCUV]{3,15}', key):
            return None
        saved = self.account_capacities.get(key)
        if (isinstance(saved, dict) and saved.get('source') == 'popup_ocr'
                and all(type(saved.get(r)) is int and saved[r] > 0 for r in resources)):
            return {r: saved[r] for r in resources}
        return None

    def save_account_capacities(self, values: Dict[str, int], acc_key: Optional[str] = None,
                                th_level: Optional[int] = None, source: str = 'popup_ocr'):
        key = acc_key or self.current_account_key
        if not isinstance(key, str) or not re.fullmatch(r'#[0289PYLQGRJCUV]{3,15}', key):
            return
        if not isinstance(values, dict) or source != 'popup_ocr':
            return
        entry = dict(self.account_capacities.get(key) or {})
        for resource, value in values.items():
            if resource in ('gold', 'elixir', 'dark_elixir') and type(value) is int and value > 0:
                entry[resource] = value
        entry.update(source=source, updated_at=time.time())
        self.account_capacities[key] = entry
        self.set_wall_wait(self._wall_builder_retry_at, self.wall_status)

    @staticmethod
    def _player_tag_from_texts(texts):
        matches = set()
        for text in texts:
            cleaned = re.sub(r'\s+', '', str(text).upper())
            # Player tags use digit zero, never letter O; OCR often confuses them.
            cleaned = cleaned.translate(str.maketrans({'O': '0', 'I': 'L', '1': 'L'}))
            for tag in re.findall(r'#[0289PYLQGRJCUV]{3,15}(?![A-Z0-9])', cleaned):
                matches.add(tag)
        return next(iter(matches)) if len(matches) == 1 else None

    @staticmethod
    def _clipboard_player_tag(value):
        if not isinstance(value, str):
            return None
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme:
            if parsed.scheme != 'https' or parsed.hostname != 'link.clashofclans.com':
                return None
            values = parse_qs(parsed.query).get('tag', [])
            value = unquote(values[0]).upper() if len(values) == 1 else ''
        else:
            value = value.upper()
        return value if re.fullmatch(r'#[0289PYLQGRJCUV]{3,15}', value) else None

    @staticmethod
    def _windows_clipboard_text():
        try:
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                                     'Get-Clipboard -Raw'], capture_output=True, timeout=3,
                                    creationflags=flags)
            return result.stdout.decode('utf-8-sig', errors='replace').strip() if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def _copy_profile_tag(self, ocr, log=None):
        """Use Share/Copy only when clipboard synchronization can be verified."""
        before = self._windows_clipboard_text()
        if before is None:
            if log:
                log('[Tài khoản] Không đọc được clipboard Windows; không thể xác minh nút Copy.')
            return None
        screen = self.adb.screencap()
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return None
        template = cv2.imread(str(Path(__file__).resolve().parents[1] / 'local_assets' / 'profile_share_icon.png'))
        if template is None:
            return None
        area = screen[150:410, 350:900]
        best = (0, None)
        for scale in (.7, .8, .9, 1., 1.1, 1.2, 1.3, 1.4):
            needle = cv2.resize(template, None, fx=scale, fy=scale)
            if needle.shape[0] > area.shape[0] or needle.shape[1] > area.shape[1]:
                continue
            _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(area, needle, cv2.TM_CCOEFF_NORMED))
            if score > best[0]:
                best = (score, (350 + loc[0] + needle.shape[1]//2, 150 + loc[1] + needle.shape[0]//2))
        if best[0] < .82:
            if log:
                log('[Tài khoản] Không nhận diện chắc nút chia sẻ hồ sơ.')
            return None
        if log:
            log('[Tài khoản] OCR chưa chắc mã; đang mở Share và chọn Copy.')
        self.adb.tap(*best[1], jitter=0, delay_after=.25)
        frame = self.adb.screencap()
        result = ocr(frame, use_det=True)
        texts, boxes = getattr(result, 'txts', []) or [], getattr(result, 'boxes', None)
        if boxes is None or len(texts) != len(boxes) or not any(str(t).strip().lower() == 'share' for t in texts):
            return None
        buttons = []
        for text, box in zip(texts, boxes):
            x, y = int(box[:, 0].mean()), int(box[:, 1].mean())
            if str(text).strip().lower() == 'copy' and 300 <= x <= 1200 and 80 <= y <= 550:
                buttons.append((x, y))
        if len(buttons) != 1:
            return None
        self.adb.tap(*buttons[0], jitter=0, delay_after=.2)
        for _ in range(4):
            value = self._windows_clipboard_text()
            if value is not None and value != before:
                tag = self._clipboard_player_tag(value)
                if tag:
                    return tag
                break
            time.sleep(.1)
        if log:
            log('[Tài khoản] Clipboard không nhận được mã mới từ giả lập; không dùng dữ liệu cũ.')
        return None

    def is_valid_village_screen(self, screen: np.ndarray) -> bool:
        """Verify if player is either in Main Village or Builder Base."""
        if screen is None:
            return False
        if self.is_in_main_village(screen):
            return True
        builder = getattr(self, 'builder', None) or getattr(getattr(self, 'manager', None), 'builder', None)
        if builder and hasattr(builder, 'is_home') and builder.is_home(screen):
            return True
        from bot.builder_base import BuilderBaseManager
        if BuilderBaseManager.night_palette(screen):
            return True
        return False

    def resolve_player_tag(self, screen, log=None):
        """Read the profile tag twice; never reuse the previous account on failure."""
        validator = getattr(self, 'is_valid_village_screen', getattr(self, 'is_in_main_village', None))
        if screen is None or (callable(validator) and not validator(screen)):
            return None
        if self._session_player_tag:
            self.player_tag_failure = None
            self.player_tag_failures = 0
            return self._session_player_tag
        header = self.vision.read_player_profile_header(screen)
        header_key = header.get('account_key') if header else None
        if log:
            log('[Tài khoản] Đang mở hồ sơ và đối chiếu mã người chơi trên hai ảnh.')
        ocr = self.vision._get_ocr()
        if not ocr:
            self.player_tag_failure = 'Không khởi tạo được OCR'
            self.player_tag_failures += 1
            if log:
                log(f'[Tài khoản] {self.player_tag_failure} ({self.player_tag_failures}/3).')
            return None
        self.adb.tap(48, 58, jitter=0, delay_after=.25)
        tag = None
        profile_open = False
        try:
            reads = []
            selected_profile = False
            for _ in range(6):
                frame = self.adb.screencap()
                if frame is None:
                    break
                result = ocr(frame[:480, :1000], use_det=True)
                texts = getattr(result, 'txts', []) or []
                boxes = getattr(result, 'boxes', None)
                if not selected_profile:
                    if boxes is None or len(boxes) != len(texts):
                        time.sleep(.05)
                        continue
                    for text, box in zip(texts, boxes):
                        label = ' '.join(str(text).lower().split())
                        x, y = int(box[:, 0].mean()), int(box[:, 1].mean())
                        if 'profile' in label and 80 <= x <= 450 and 10 <= y <= 120:
                            profile_open = True
                            if log:
                                log('[Tài khoản] Đang chọn tab My Profile để đọc mã làng.')
                            self.adb.tap(x, y, jitter=0, delay_after=.25)
                            selected_profile = True
                            break
                    continue
                # The tab label is also visible on Settings; require the
                # profile's Home Village or Builder Base section before accepting any tag.
                joined = ' '.join(str(t).lower() for t in texts)
                if not any(k in joined for k in ('home village', 'builder base', 'builder')):
                    time.sleep(.05)
                    continue
                # The tag is small on the profile card. Detect it in a focused,
                # enlarged crop rather than relying on whole-screen OCR.
                crop = frame[190:410, 150:650]
                enlarged = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                tag_result = ocr(enlarged, use_det=True)
                found = self._player_tag_from_texts(getattr(tag_result, 'txts', []) or [])
                if found:
                    reads.append(found)
                    if len(reads) >= 2 and reads[-1] == reads[-2]:
                        tag = found
                        break
                time.sleep(.05)
            if not tag and selected_profile:
                tag = self._copy_profile_tag(ocr, log)
        except BotStoppedError:
            raise
        except Exception:
            tag = None
        finally:
            frame = self.adb.screencap()
            close = self.vision.find_close_button(frame) if frame is not None else None
            if close:
                self.adb.tap(close[0], close[1], jitter=0, delay_after=.2)
        validator = getattr(self, 'is_valid_village_screen', getattr(self, 'is_in_main_village', None))
        home = validator(self.adb.screencap()) if callable(validator) else True
        if not home or not tag:
            if not home:
                reason = 'Không đóng được hồ sơ hoặc chưa về làng'
            elif not profile_open:
                reason = 'Không mở hoặc nhận diện được hồ sơ người chơi'
            else:
                reason = 'OCR không đọc chắc mã người chơi'
            self.player_tag_failure = reason
            self.player_tag_failures += 1
            if log:
                log(f'[Tài khoản] {reason} ({self.player_tag_failures}/3); chưa dùng dữ liệu tài khoản cũ.')
            return None
        self.player_tag_failure = None
        self.player_tag_failures = 0
        if tag != self.current_account_key:
            self.current_account_key = tag
            self.walls_confirmed_maxed = bool(self.verified_maxed_accounts.get(tag))
            self._lowest_wall_target = None
            if log:
                log(f'[Tài khoản] Đã xác minh mã người chơi {tag}.')
        self._verified_header_key = header_key
        self._session_player_tag = tag
        if log:
            log(f'[Tài khoản] Đã kiểm tra mã {tag}; tiếp tục tại làng hiện tại.')
        return tag

    def save_account_builder_timers(self, jobs: list, acc_key: Optional[str] = None):
        """Lưu danh sách tiến trình thợ đang xây và thời điểm thợ rảnh gần nhất theo từng tài khoản."""
        acc_key = acc_key if isinstance(acc_key, str) and acc_key else (
            self.current_account_key if isinstance(self.current_account_key, str) and self.current_account_key else 'default'
        )
        now = time.time()
        normalized_jobs = []
        for job in (jobs or []):
            if not isinstance(job, dict):
                continue
            sec = int(job.get('duration_sec') or 0)
            if sec <= 0:
                continue
            finish_at = float(job.get('finish_at') or (now + sec))
            if finish_at <= now:
                continue
            dur_str = str(job.get('duration_str') or job.get('duration') or f"{sec}s")
            normalized_jobs.append({
                'name': str(job.get('name') or 'Công trình'),
                'duration': dur_str,
                'duration_str': dur_str,
                'duration_sec': sec,
                'finish_at': round(finish_at, 1),
            })
        normalized_jobs.sort(key=lambda j: j['finish_at'])
        next_free_at = normalized_jobs[0]['finish_at'] if normalized_jobs else 0.0
        next_free_in_sec = max(0, int(round(next_free_at - now))) if next_free_at > now else 0
        self.account_builder_timers[acc_key] = {
            'updated_at': round(now, 1),
            'next_free_at': round(next_free_at, 1),
            'next_free_in_sec': next_free_in_sec,
            'jobs': normalized_jobs,
        }
        v_state = getattr(self, 'village_state', None)
        if v_state is not None and hasattr(v_state, 'set_ongoing_upgrades'):
            v_state.set_ongoing_upgrades(normalized_jobs)
        self.set_wall_wait(self._wall_builder_retry_at, self.wall_status)

    def record_started_builder_upgrade(self, name: str, duration_sec: int, acc_key: Optional[str] = None):
        """Ghi nhận thêm một thợ vừa bắt đầu nâng cấp công trình mới vào lịch thợ của tài khoản."""
        if not isinstance(duration_sec, int) or duration_sec <= 0:
            return
        acc_key = acc_key if isinstance(acc_key, str) and acc_key else (
            self.current_account_key if isinstance(self.current_account_key, str) and self.current_account_key else 'default'
        )
        now = time.time()
        existing = []
        cur = self.account_builder_timers.get(acc_key)
        if isinstance(cur, dict) and isinstance(cur.get('jobs'), list):
            for j in cur['jobs']:
                if isinstance(j, dict) and float(j.get('finish_at') or 0) > now:
                    rem = max(1, int(round(float(j['finish_at']) - now)))
                    existing.append(dict(j, duration_sec=rem, finish_at=float(j['finish_at'])))
        h, rem = divmod(duration_sec, 3600)
        m, s = divmod(rem, 60)
        dur_str = f"{h}h {m}m" if h > 0 else (f"{m}m {s}s" if m > 0 else f"{s}s")
        existing.append({
            'name': str(name or 'Công trình'),
            'duration': dur_str,
            'duration_str': dur_str,
            'duration_sec': duration_sec,
            'finish_at': round(now + duration_sec, 1),
        })
        self.save_account_builder_timers(existing, acc_key=acc_key)

    def get_account_shortest_builder_seconds(self, acc_key: Optional[str] = None) -> Optional[int]:
        """Lấy số giây còn lại của thợ xây hoàn thành sớm nhất đã lưu cho tài khoản."""
        acc_key = acc_key if isinstance(acc_key, str) and acc_key else (
            self.current_account_key if isinstance(self.current_account_key, str) and self.current_account_key else 'default'
        )
        entry = self.account_builder_timers.get(acc_key)
        if not isinstance(entry, dict):
            return None
        now = time.time()
        active_finishes = [
            float(j['finish_at'])
            for j in (entry.get('jobs') or [])
            if isinstance(j, dict) and isinstance(j.get('finish_at'), (int, float)) and float(j['finish_at']) > now
        ]
        if not active_finishes and isinstance(entry.get('next_free_at'), (int, float)) and float(entry['next_free_at']) > now:
            active_finishes.append(float(entry['next_free_at']))
        if not active_finishes:
            return None
        return max(1, int(round(min(active_finishes) - now)))

    def clear_wall_wait(self):
        self.set_wall_wait(0, 'Sẽ kiểm tra lại thợ ở lượt về làng kế tiếp.')

    def check_and_sync_account(self, screen, log=None):
        """Check top-left player profile. If switched account, sync maxed status accordingly."""
        validator = getattr(self, 'is_valid_village_screen', getattr(self, 'is_in_main_village', None))
        if screen is None or (callable(validator) and not validator(screen)):
            return self.current_account_key
        prof = self.vision.read_player_profile_header(screen)
        if not prof:
            return self.current_account_key
        acc_key = prof['account_key']
        if self._session_player_tag:
            return self._session_player_tag
        if (isinstance(self.current_account_key, str) and self.current_account_key.startswith('#')
                and self._verified_header_key == acc_key):
            return self.current_account_key
        if acc_key != self.current_account_key:
            prev = self.current_account_key
            self.current_account_key = acc_key
            v_info = self.verified_maxed_accounts.get(acc_key)
            is_max = bool(v_info)
            th_lvl = v_info.get('th_level') if isinstance(v_info, dict) else None
            self.walls_confirmed_maxed = is_max
            v_state = getattr(self, 'village_state', None)
            if v_state is not None:
                v_state.record_walls_maxed(is_max, th_lvl)
            if isinstance(th_lvl, int) and th_lvl in self.TH_MAX_STORAGE_CAPACITIES and acc_key not in self.account_capacities:
                self.account_capacities[acc_key] = dict(
                    self.TH_MAX_STORAGE_CAPACITIES[th_lvl],
                    th_level=th_lvl,
                    source='th_verified',
                    updated_at=time.time(),
                )
            wall_enabled = bool(self.settings.get('auto_upgrade_walls', True)) if self.settings else True
            if log:
                th_str = f", TH{th_lvl}" if th_lvl else ""
                log(f"[Tài khoản] Đã nhận diện: {prof['name']} (Cấp {prof['level']}{th_str}).")
                if is_max:
                    log(f"[Tài khoản] Tài khoản {prof['name']} ĐÃ XÁC MINH MAX TƯỜNG ✅.")
                elif not wall_enabled:
                    log(f"[Tài khoản] Tài khoản {prof['name']}: Tự động nâng tường đang TẮT trong cài đặt.")
                else:
                    log(f"[Tài khoản] Tài khoản {prof['name']} CHƯA xác minh Max tường; sẽ nâng tường bình thường.")
                caps = self.get_account_capacities(('gold', 'elixir'), acc_key=acc_key)
                if caps:
                    log(f"[Tài khoản] Dung lượng kho {prof['name']}: Vàng {caps['gold']:,} | Dầu {caps['elixir']:,} (bỏ qua quét chạm).")
                shortest = self.get_account_shortest_builder_seconds(acc_key=acc_key)
                if shortest:
                    sh_h, sh_rem = divmod(shortest, 3600)
                    sh_m, sh_s = divmod(sh_rem, 60)
                    sh_str = f"{sh_h}h {sh_m}m {sh_s}s" if sh_h > 0 else f"{sh_m}m {sh_s}s"
                    log(f"[Tài khoản] Lịch thợ xây đã lưu của {prof['name']}: thợ gần nhất còn {sh_str}.")
            if self.settings:
                self.settings.set('farm_finish_enabled', is_max)
            self.set_wall_wait(self._wall_builder_retry_at, self.wall_status)
        return acc_key

    def verify_walls(self, log_callback=None):
        from bot.walls import WallUpgrader
        return WallUpgrader(self).verify_walls_via_layout_editor(log_callback)

    def refresh_wall_builder_wait(self, screen):
        """Discard an old wall cooldown once the HUD shows a regular builder."""
        if not self.is_in_main_village(screen):
            return False
        from bot.walls import WallUpgrader
        free = WallUpgrader(self).builders(screen)
        if type(free) is not int or free <= 0:
            return False
        self.clear_wall_wait()
        return True

    @staticmethod
    def valid_wall_target(target):
        if not isinstance(target, dict):
            return False
        return (all(type(target.get(key)) is int and low <= target[key] <= high for key,low,high in
                    (('level',1,25),('cost',1,100000000),('available',1,1000),('scroll_down',0,36)))
                and all(type(target.get(key)) in (int,float) and low <= target[key] <= high
                        for key,low,high in (('relative_x',.7,1),('relative_y',.05,.9)))
                and type(target.get('scanned_at')) in (int,float)
                and 0 <= time.time()-target['scanned_at'] < 1800)

    def save_wall_target(self, target):
        self._lowest_wall_target = target if self.valid_wall_target(target) else None
        self.set_wall_wait(self._wall_builder_retry_at, self.wall_status)

    def assign_apprentice(self, screen, log):
        if self._apprentice is None:
            from bot.apprentice import ApprenticeAssigner
            self._apprentice = ApprenticeAssigner(self)
        return self._apprentice.run(screen, log)

    def is_in_main_village(self, screen: np.ndarray) -> bool:
        """Verify if player is on the main village screen."""
        if screen is None:
            return False
        return self.vision.is_in_main_village(screen)

    def deselect_all(self):
        """Click a clear grass patch; fixed map coordinates can hit buildings after a pan."""
        screen=self.adb.screencap()
        if screen is None or self.vision.has_open_dialog(screen):return False
        area=screen[280:600,550:1100]
        hsv=cv2.cvtColor(area,cv2.COLOR_BGR2HSV)
        mask=cv2.inRange(hsv,(30,40,70),(85,190,220))
        distance=cv2.distanceTransform(mask,cv2.DIST_L2,3)
        _,score,_,position=cv2.minMaxLoc(distance)
        if score<10:return False
        self.adb.tap(position[0]+550,position[1]+280,jitter=0,delay_after=.3)
        return True

    def dismiss_notification_prompt(self, screen):
        """Dismiss only the game's identified Notifications / Not now dialog."""
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        # Avoid running OCR on every ordinary village frame.
        area = screen[360:650, 420:1180]
        if (np.min(area, axis=2) > 180).mean() < .25:
            return False
        ocr = self.vision._get_ocr()
        if not ocr:
            return False
        try:
            result = ocr(screen, use_det=True)
            texts = getattr(result, 'txts', []) or []
            boxes = getattr(result, 'boxes', [])
            if not any('notification' in str(t).lower() for t in texts) or len(texts) != len(boxes):
                return False
            positioned = [(str(text).lower().strip(), int(box[:, 0].mean()), int(box[:, 1].mean()))
                          for text, box in zip(texts, boxes)]
            for text, x, y in positioned:
                if 'not now' in ' '.join(text.split()):
                    self.adb.tap(x, y, jitter=0, delay_after=.5)
                    return True
                if text == 'not':
                    nearby = [(nx, ny) for word, nx, ny in positioned
                              if word == 'now' and 0 < nx-x < 180 and abs(ny-y) < 25]
                    if len(nearby) == 1:
                        self.adb.tap((x + nearby[0][0])//2, y, jitter=0, delay_after=.5)
                        return True
        except Exception:
            return False
        return False

    def close_popups_and_dialogs(self, screen: np.ndarray) -> bool:
        """Finds and dismisses common popups like Okay, Confirm, Cancel, Close 'X'."""
        if screen is None:
            return False

        if self.dismiss_notification_prompt(screen):
            return True

        if self.vision.wall_confirmation(screen):
            from pathlib import Path
            cancel=self.vision.match_template(screen,str(Path(__file__).resolve().parents[1]/'local_assets'/'wall_cancel.png'),.90,roi=(400,450,450,230))
            if cancel:
                self.adb.tap(cancel[0],cancel[1],jitter=0,delay_after=.5)
                return True
            return False

        close=self.vision.find_close_button(screen)
        if close:
            self.adb.tap(close[0],close[1],jitter=0,delay_after=.5)
            return True

        okay_top = self.vision.find_okay_button(screen) if hasattr(self.vision, 'find_okay_button') else None
        if okay_top:
            self.adb.tap(okay_top[0], okay_top[1], jitter=2, delay_after=0.5)
            return True

        # Không tự ý đóng nếu đang mở Menu Tấn công hoặc đang trong trận đánh
        if self.vision.is_attack_menu_open(screen)[0] or self.vision.is_enemy_base(screen) or self.vision.is_in_combat(screen):
            return False

        # 1. Đóng ngăn kéo Clan Chat nếu vô tình bị mở (thường ở nửa trái màn hình)
        # Kiểm tra nếu có ô nhập "Clan Message..." hoặc tab chat drawer mở
        ocr = self.vision._get_ocr()
        if ocr:
            try:
                # OCR nhanh góc dưới trái để kiểm tra Clan chat
                chat_box = screen[820:860, 20:300]
                chat_res = ocr(chat_box)
                if chat_res and hasattr(chat_res, "txts") and chat_res.txts:
                    for t in chat_res.txts:
                        if "clan" in t.lower() or "message" in t.lower():
                            self.adb.tap(490, 450, jitter=4, delay_after=0.5)
                            return True
            except Exception:
                pass

        # 2. Check nút tắt 'X' ở các góc dialog (Shop, Event, Task Cards, Season Challenges)
        for x_tmpl in ["x", "needmore_x", "profile_x", "xwall"]:
            x_match = self.vision.match_template(screen, x_tmpl, threshold=0.72)
            if x_match:
                self.adb.tap(x_match[0], x_match[1], jitter=3, delay_after=0.4)
                return True

        # Check nút 'X' đóng shop/ưu đãi/dialog ở góc trên phải (x: 1450-1580, y: 20-120)
        top_right_x = self.vision.match_template(screen, "x", threshold=0.65, roi=(1450, 20, 140, 100))
        if top_right_x:
            self.adb.tap(top_right_x[0], top_right_x[1], jitter=3, delay_after=0.4)
            return True

        # 3. Check Okay
        okay_match = self.vision.match_template(screen, "okay", threshold=0.75, roi=(500, 450, 600, 350))
        if okay_match:
            cx, cy, _, _, _ = okay_match
            self.adb.tap(cx, cy, jitter=3, delay_after=0.5)
            return True

        # 5. Check Return Home button
        home_match = self.vision.match_template(screen, "return_home", threshold=0.72, roi=(500, 600, 600, 250))
        if not home_match:
            home_match = self.vision.match_template(screen, "okhome", threshold=0.72, roi=(500, 600, 600, 250))
        if home_match:
            cx, cy, _, _, _ = home_match
            self.adb.tap(cx, cy, jitter=3, delay_after=1.0)
            return True

        return False

    def find_collectors(self, screen):
        """Find all visible collection bubbles, across zoom levels, away from HUD."""
        from pathlib import Path
        if screen is None or screen.shape[:2] != (900, 1600):
            return []
        area = screen[80:840, 160:1400]
        gray = cv2.cvtColor(area, cv2.COLOR_BGR2GRAY)
        matches = []
        for resource in ('gold', 'elixir', 'dark_elixir'):
            path = str(Path(__file__).resolve().parents[1] / 'local_assets' / ('collect_' + resource + '.png'))
            original = self.vision.load_template(path)
            if original is None:
                continue
            for scale in (.65, .75, .8, .85, .9, .95, 1., 1.05, 1.15, 1.3):
                candidate = cv2.resize(original, None, fx=scale, fy=scale)
                h, w = candidate.shape[:2]
                if h > area.shape[0] or w > area.shape[1]:
                    continue
                scores = cv2.matchTemplate(gray, cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY), cv2.TM_CCOEFF_NORMED)
                for _ in range(16):
                    _, score, _, position = cv2.minMaxLoc(scores)
                    if score < (.75 if resource == 'dark_elixir' else .80):
                        break
                    px, py = position
                    x, y = px + 160 + w//2, py + 80 + h//2
                    inner = area[py+int(h*.15):py+int(h*.65), px+int(w*.2):px+int(w*.8)]
                    hsv = cv2.cvtColor(inner, cv2.COLOR_BGR2HSV)
                    bounds = {'gold': ((15, 90, 120), (40, 255, 255)),
                              'elixir': ((125, 70, 100), (175, 255, 255)),
                              'dark_elixir': ((0, 0, 0), (180, 255, 90))}
                    color = cv2.inRange(hsv, *bounds[resource])
                    # Resource HUD and the expanded lower dock are not collectors.
                    if ((color > 0).mean() > .25 and not (x >= 1230 and y < 340)
                            and not (x < 820 and y >= 790)):
                        matches.append(dict(resource=resource, x=x, y=y, score=float(score)))
                    scores[max(0, py-h//2):py+h//2+1, max(0, px-w//2):px+w//2+1] = -1
        selected = []
        for match in sorted(matches, key=lambda item: item['score'], reverse=True):
            if all((match['x']-old['x'])**2 + (match['y']-old['y'])**2 > 28**2 for old in selected):
                selected.append(match)
        return selected

    def collect_resources(self, screen=None, log_callback=None):
        """Tap visible collector bubbles when the independent cooldown expires."""
        now = time.monotonic()
        interval = max(1, int(self.settings.get('collect_interval_min', 60))) * 60
        if self.last_collect_time is not None and now - self.last_collect_time < interval:
            return 0
        screen = screen if screen is not None else self.adb.screencap()
        if not self.is_in_main_village(screen):
            return 0

        self.last_collect_time = now
        attempted = []
        for _ in range(30):
            self.adb._guard_input()
            # One tap can clear neighboring bubbles. Scan for the next visible
            # target and avoid tapping a persistent bubble twice.
            item = next((item for item in self.find_collectors(screen)
                         if all((item['x']-x)**2 + (item['y']-y)**2 > 28**2
                                for x, y in attempted)), None)
            if item is None:
                break
            point = (item['x'], item['y'])
            self.adb.tap(*point, jitter=0, delay_after=.15)
            attempted.append(point)
            screen = self.adb.screencap()
            if not self.is_in_main_village(screen):
                break
        if log_callback:
            if attempted:
                log_callback(f'[Collect] Đã bấm {len(attempted)} biểu tượng thu hoạch.')
            else:
                log_callback('[Collect] Chưa thấy biểu tượng mỏ sẵn sàng thu.')
        return len(attempted)

    def train_army(self, force: bool = False):
        """
        Uses Quick Train to queue up the preferred army composition.
        Path: Train button (85, 740) -> Quick Train tab (1050, 110) -> Train slot 1 (1320, 310) -> Close (1510, 80)
        """
        now = time.time()
        if not force and now - self.last_train_time < 45:
            return

        # 1. Open Train Army
        self.adb.tap(85, 740, delay_after=0.7)

        # 2. Tap 'Quick Train' tab
        self.adb.tap(1050, 110, delay_after=0.6)

        # 3. Tap 'Train' for Slot 1 or chosen slot
        slot = self.settings.get("quick_train_slot", 1)
        slot_y = 310 if slot == 1 else (480 if slot == 2 else 650)
        self.adb.tap(1320, slot_y, delay_after=0.5)

        # 4. Close Training Menu (1542, 80 in 1600x900)
        self.adb.tap(1542, 80, delay_after=0.6)
        fresh = self.adb.screencap()
        if fresh is not None and self.vision.has_open_dialog(fresh):
            close = self.vision.find_close_button(fresh)
            if close:
                self.adb.tap(close[0], close[1], delay_after=0.5)

        self.last_train_time = now
        self.deselect_all()

    def request_clan_troops(self):
        """Request clan castle reinforcements if enabled."""
        if not self.settings.get("auto_request_troops", False):
            return
        # Open Clan Chat tab on the left: (20, 450)
        self.adb.tap(20, 450, delay_after=0.7)
        # Tap Request button in clan chat: (250, 820)
        self.adb.tap(250, 820, delay_after=0.5)
        # Tap Send Request: (1080, 720)
        self.adb.tap(1080, 720, delay_after=0.5)
        # Close chat drawer: tap arrow at (490, 450)
        self.adb.tap(490, 450, delay_after=0.4)
        self.deselect_all()

    def upgrade_walls(self, screen, current_gold, current_elixir, log_callback=None):
        from bot.walls import WallUpgrader
        return WallUpgrader(self).run_cycle(screen, current_gold, current_elixir, log_callback)

    def collect_treasury(self, log_callback=None):
        from bot.treasury import TreasuryCollector
        if self._treasury_collector is None:
            self._treasury_collector=TreasuryCollector(self)
        return self._treasury_collector.run(log_callback)

    def treasury_worker(self):
        from bot.treasury import TreasuryCollector
        if self._treasury_collector is None:
            self._treasury_collector=TreasuryCollector(self)
        return self._treasury_collector

    def observe_reward_timers(self,screen,log_callback=None):
        self.treasury_worker().observe_home(screen,log_callback)

    def handle_star_bonus(self,screen,log_callback=None):
        from bot.star_bonus import StarBonusWatcher
        return StarBonusWatcher(self.treasury_worker()).handle(screen,log_callback)
