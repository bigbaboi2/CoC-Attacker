"""Assign regular builders using verified suggestions, custom filters, and resource confirmations."""
import re
import time
from pathlib import Path

import cv2
import numpy as np

from bot.walls import WallUpgrader
from config.settings import (
    BUILDING_CATALOG,
    BUILDING_KEY_ALIASES,
    BUILDING_KEY_TO_CATEGORY,
    DEFAULT_BUILDING_WHITELIST,
)
from core.adb_client import BotStoppedError


class SuggestedUpgrader:
    FIXED_FILTER_POINTS = {
        'button': (1216, 33),
        'res_gold': (670, 218),
        'res_elixir': (901, 218),
        'res_dark_elixir': (1132, 218),
        'type_army': (670, 358),
        'type_defense': (901, 358),
        'type_hero': (1132, 358),
        'type_resource': (670, 446),
        'type_new': (901, 446),
        'type_permanent': (1132, 446),
        'sort_cost_max': (670, 587),
        'sort_cost_min': (901, 587),
    }

    def __init__(self, village):
        self.village = village
        self.adb = village.adb
        self.vision = village.vision
        self.ui = WallUpgrader(village)
        self.assignments = []
        self.pending = None
        self.target_candidate = None
        self.unaffordable = {}
        self.last_status = 'Chưa kiểm tra nâng công trình.'
        self.deadline = 0
        catalog = Path(self.vision.asset_dir) / 'building_names'
        # Seasonal modules have their own catalog. Only ordinary level upgrades
        # from the root catalog can consume a regular builder in this workflow.
        self.base_regular_names = {
            self.normalized(path.stem)
            for path in catalog.glob('*.png')
            if self.normalized(path.stem) != 'craftingstation'
        }
        self.custom_regular_names = set(self.base_regular_names)
        for items in BUILDING_CATALOG.values():
            for key, _, _ in items:
                self.custom_regular_names.add(key)
        self.regular_names = self.base_regular_names

    def check(self):
        self.adb._guard_input()
        if self.deadline > 0 and time.monotonic() >= self.deadline:
            raise TimeoutError('Hết thời gian kiểm tra nâng công trình')

    @staticmethod
    def normalized(text):
        return re.sub(r'[^a-z0-9]', '', text.lower())

    @classmethod
    def canonical_key(cls, text):
        norm = cls.normalized(text)
        return BUILDING_KEY_ALIASES.get(norm, norm)

    @classmethod
    def category_of(cls, name):
        ckey = cls.canonical_key(name)
        return BUILDING_KEY_TO_CATEGORY.get(ckey)

    def custom_mode_enabled(self):
        settings = getattr(self.village, 'settings', None)
        return bool(settings and settings.get('auto_upgrade_buildings', False))

    @staticmethod
    def is_supercharge_row(screen, y) -> bool:
        """Detect the cyan-blue lightning bolt badge shown under Supercharge rows."""
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        y1 = max(0, int(y) + 6)
        y2 = min(900, int(y) + 40)
        if y2 <= y1:
            return False
        patch = screen[y1:y2, 1243:1365]
        if not patch.size:
            return False
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        blue = (hsv[:, :, 0] >= 85) & (hsv[:, :, 0] <= 118) & (hsv[:, :, 1] >= 120) & (hsv[:, :, 2] >= 130)
        return int(blue.sum()) >= 18

    _HEADER_KEYWORDS = ('suggest', 'filter', 'gợi ý', 'đã lọc', 'lọc')

    def diagnose_row_rejection(self, candidate, custom_mode=False):
        """Return rejection reason string if candidate is disallowed, else None."""
        name = candidate.get('name', '')
        key = self.normalized(name)
        allowed_names = self.custom_regular_names if custom_mode else self.base_regular_names
        if key not in allowed_names or key.startswith('wall'):
            return 'Ngoài danh mục hoặc là tường'

        settings = getattr(self.village, 'settings', None)
        ckey = self.canonical_key(name)

        # 1. Town Hall Safety Lock: Strictly forbid Town Hall if lock is on
        lock_th = True if settings is None else bool(settings.get('lock_townhall_upgrade', True))
        if lock_th and (ckey in ('townhall', 'town_hall') or 'town hall' in name.lower() or 'nhà chính' in name.lower()):
            return 'Khóa an toàn Town Hall đang bật'

        category = BUILDING_KEY_TO_CATEGORY.get(ckey)
        if (settings is not None and not settings.get('building_upgrade_allow_supercharge', True)
                and candidate.get('supercharge') and category != 'heroes'):
            return 'Tắt nâng Supercharge (tia sét xanh)'

        if not custom_mode:
            return None
        if settings is None:
            return None

        whitelist = settings.get('building_upgrade_whitelist')
        if isinstance(whitelist, dict) and ckey in whitelist and not whitelist[ckey]:
            return f'Bị bỏ chọn trong danh sách chi tiết ({ckey})'

        # 2. Specific Target Upgrade Priority: Scan ONLY for that specific building!
        priority = settings.get('building_upgrade_priority', 'time_min')
        if priority == 'specific':
            spec_target = (settings.get('building_upgrade_specific_target') or '').strip().lower()
            if spec_target:
                canon_target = self.canonical_key(spec_target)
                canon_target = BUILDING_KEY_ALIASES.get(canon_target, canon_target)
                canon_cand = BUILDING_KEY_ALIASES.get(ckey, ckey)
                if canon_cand != canon_target:
                    return f'Không khớp công trình cụ thể ({canon_target})'
                return None

        allowed_res = settings.get('building_upgrade_resources') or {
            'gold': True, 'elixir': True, 'dark_elixir': True
        }
        resource = candidate.get('resource')
        if resource and not allowed_res.get(resource, True):
            return f'Tài nguyên {resource} đang tắt'
        category = BUILDING_KEY_TO_CATEGORY.get(ckey)
        categories_cfg = settings.get('building_upgrade_categories') or {
            'traps': True, 'resource': True, 'defense': True, 'army': True, 'heroes': True
        }
        if category and not categories_cfg.get(category, True):
            return f"Nhóm công trình '{category}' đang tắt"
        return None

    def is_candidate_allowed(self, candidate, custom_mode=False):
        return self.diagnose_row_rejection(candidate, custom_mode=custom_mode) is None

    @staticmethod
    def sort_candidates(candidates, priority='suggested'):
        if priority == 'time_min':
            return sorted(
                candidates,
                key=lambda c: (
                    c.get('duration_sec') if isinstance(c.get('duration_sec'), int) and c['duration_sec'] > 0 else 10**9,
                    c['cost'],
                    c.get('page', 0),
                    c['y'],
                ),
            )
        if priority == 'time_max':
            return sorted(
                candidates,
                key=lambda c: (
                    -(c.get('duration_sec') if isinstance(c.get('duration_sec'), int) else 0),
                    -c['cost'],
                    c.get('page', 0),
                    c['y'],
                ),
            )
        if priority == 'cost_min':
            return sorted(
                candidates,
                key=lambda c: (
                    c['cost'],
                    c.get('duration_sec') if isinstance(c.get('duration_sec'), int) and c['duration_sec'] > 0 else 10**9,
                    c.get('page', 0),
                    c['y'],
                ),
            )
        if priority == 'cost_max':
            return sorted(
                candidates,
                key=lambda c: (
                    -c['cost'],
                    -(c.get('duration_sec') if isinstance(c.get('duration_sec'), int) else 0),
                    c.get('page', 0),
                    c['y'],
                ),
            )
        if priority == 'specific':
            return sorted(candidates, key=lambda c: (c.get('page', 0), c['cost'], c['y']))
        return sorted(candidates, key=lambda c: (c.get('page', 0), c['y']))

    @staticmethod
    def is_filter_popup_open(screen):
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        p = screen[105:115, 690:710]
        if p.size == 0:
            return False
        bgr = p.mean(axis=(0, 1))
        return (50 <= bgr[0] <= 80) and (75 <= bgr[1] <= 110) and (110 <= bgr[2] <= 145)

    @staticmethod
    def is_filter_button_active(screen, point, sorting=False):
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return None
        cx, cy = point
        btn = screen[max(0, cy - 25):min(900, cy + 25), max(0, cx - 85):min(1600, cx + 85)]
        if btn.size == 0:
            return None
        hsv = cv2.cvtColor(btn, cv2.COLOR_BGR2HSV)
        yellow_mask = (hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 35) & (hsv[:, :, 1] >= 140) & (hsv[:, :, 2] >= 180)
        if sorting:
            return int(yellow_mask.sum()) >= 300
        h, w = btn.shape[:2]
        bg_yellow_mask = yellow_mask.copy()
        bg_yellow_mask[max(0, h//2 - 22):min(h, h//2 + 22), max(0, w//2 - 25):min(w, w//2 + 25)] = False
        return int(bg_yellow_mask.sum()) >= 300

    def set_menu_resource_filter(self, screen, selected_resource=None):
        """The game's resource buttons are exclusive; no selection shows all."""
        pts = self.FIXED_FILTER_POINTS
        resources = ('gold', 'elixir', 'dark_elixir')
        for resource in resources:
            point = pts[f'res_{resource}']
            active = self.is_filter_button_active(screen, point)
            if active is None:
                return screen, False
            if active and resource != selected_resource:
                self.adb.tap(*point, jitter=0, delay_after=0.3)
                screen = self.adb.screencap()
        if selected_resource is not None:
            point = pts[f'res_{selected_resource}']
            if self.is_filter_button_active(screen, point) is False:
                self.adb.tap(*point, jitter=0, delay_after=0.3)
                screen = self.adb.screencap()
        verified = all(
            self.is_filter_button_active(screen, pts[f'res_{resource}'])
            is (resource == selected_resource)
            for resource in resources
        )
        return screen, verified

    def _menu_header_shows_unfiltered(self, screen) -> bool:
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        words = self.words(screen, (1170, 50, 430, 755))
        joined = ' '.join(t.lower() for t, _, _ in words)
        return ('suggest' in joined or 'gợi ý' in joined) and 'filter' not in joined and 'đã lọc' not in joined

    def apply_menu_filter_if_needed(self):
        """Use fixed coordinates on the Builder Menu Filter popup when enabled."""
        settings = getattr(self.village, 'settings', None)
        if not settings or not settings.get('auto_upgrade_buildings', False):
            return True
        if not settings.get('building_upgrade_use_menu_filter', True):
            return True
        res_cfg = settings.get('building_upgrade_resources') or {'gold': True, 'elixir': True, 'dark_elixir': True}
        allowed_resources = [key for key in ('gold', 'elixir', 'dark_elixir')
                             if res_cfg.get(key, True)]
        priority = settings.get('building_upgrade_priority', 'time_min')
        if priority == 'specific':
            spec_target = (settings.get('building_upgrade_specific_target') or '').strip().lower()
            canon_target = self.canonical_key(spec_target)
            canon_target = BUILDING_KEY_ALIASES.get(canon_target, canon_target)
            target_res_tuple = BUILDING_KEY_TO_RESOURCES.get(canon_target)
            if target_res_tuple:
                allowed_resources = [key for key in allowed_resources if key in target_res_tuple]
        # Selecting Gold then Elixir leaves only Elixir selected in CoC. When
        # several resources are allowed, clear the game's filter and apply the
        # user's resource choices to each OCR candidate instead.
        selected_resource = allowed_resources[0] if len(allowed_resources) == 1 else None
        log = getattr(self, '_active_log', None)
        pts = self.FIXED_FILTER_POINTS
        self.check()
        screen = self.adb.screencap()
        if (selected_resource is None and priority not in ('cost_max', 'cost_min')
                and not self.is_filter_popup_open(screen)
                and self._menu_header_shows_unfiltered(screen)):
            if callable(log):
                log('[Thợ] [Lọc] Menu đang ở chế độ gợi ý gốc (không lọc) -> giữ nguyên.')
            return True
        if callable(log):
            log(f"[Thợ] [Lọc] Mở bộ lọc game: tài nguyên={selected_resource or 'tất cả'}, ưu tiên={priority}.")
        if not self.is_filter_popup_open(screen):
            self.adb.tap(*pts['button'], jitter=0, delay_after=0.35)
            screen = self.adb.screencap()
        if not self.is_filter_popup_open(screen):
            if callable(log):
                log('[Thợ] [Lọc] ❌ Không mở được popup bộ lọc menu thợ.')
            return False
        screen, resource_verified = self.set_menu_resource_filter(screen, selected_resource)
        if not resource_verified:
            if callable(log):
                log('[Thợ] [Lọc] ❌ Không xác minh được nút tài nguyên trong popup bộ lọc.')
            return False
        if priority in ('cost_max', 'cost_min'):
            target_pt_key = 'sort_cost_max' if priority == 'cost_max' else 'sort_cost_min'
            active = self.is_filter_button_active(screen, pts[target_pt_key], sorting=True)
            if active is False:
                self.adb.tap(*pts[target_pt_key], jitter=0, delay_after=0.2)
                screen = self.adb.screencap()
        else:
            for pt_key in ('sort_cost_max', 'sort_cost_min'):
                active = self.is_filter_button_active(screen, pts[pt_key], sorting=True)
                if active is True:
                    self.adb.tap(*pts[pt_key], jitter=0, delay_after=0.2)
                    screen = self.adb.screencap()
        if self.is_filter_popup_open(screen):
            self.adb.tap(*pts['button'], jitter=0, delay_after=0.25)
        closed = not self.is_filter_popup_open(self.adb.screencap())
        if callable(log):
            log(f"[Thợ] [Lọc] {'✅ Đã áp dụng & đóng popup bộ lọc' if closed else '❌ Chưa đóng được popup bộ lọc'}.")
        return closed

    def reset_menu_filter(self):
        """Clear exclusive resource and cost filters to show all builder jobs."""
        pts = self.FIXED_FILTER_POINTS
        self.check()
        screen = self.adb.screencap()
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return
        if not self.is_filter_popup_open(screen):
            self.adb.tap(*pts['button'], jitter=0, delay_after=0.5)
            screen = self.adb.screencap()
        if not self.is_filter_popup_open(screen):
            return
        screen, verified = self.set_menu_resource_filter(screen)
        if not verified:
            return
        for pt_key in ('sort_cost_max', 'sort_cost_min'):
            active = self.is_filter_button_active(screen, pts[pt_key], sorting=True)
            if active is True:
                self.adb.tap(*pts[pt_key], jitter=0, delay_after=0.25)
                screen = self.adb.screencap()
        if self.is_filter_popup_open(screen):
            self.adb.tap(*pts['button'], jitter=0, delay_after=0.35)

    @classmethod
    def parse_duration(cls, text, allow_unitless_seconds=False):
        if not text:
            return None
        s = str(text).strip()
        if not s:
            return None
        # Chuyển S đứng trước d/h/m/s thành 5 (VD: 'Sd' -> '5d', 'Sh' -> '5h')
        s = re.sub(r'[sS](?=[dDhHmMsS]\b)', '5', s)
        s = s.lower()
        s = re.sub(r'[ilo]', lambda m: '1' if m.group(0) in 'il' else '0', s)
        s = re.sub(r'[\s\-.,:_]+', '', s)
        # Bỏ ký tự ở cuối do OCR nhầm icon đồng hồ bên phải (VD: '1d3h0', '8h0', '11m7sc')
        s = re.sub(r'([dhms])[0coqg@()]+$', r'\1', s)
        if re.fullmatch(r'(?:\d+[dhms])+', s):
            total = 0
            mult = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
            for val_str, unit in re.findall(r'(\d+)([dhms])', s):
                val = int(val_str)
                # Trong CoC, số giờ hiển thị luôn < 24, số phút/giây luôn < 60.
                # Nếu OCR đọc nhầm '14h' -> '74h' (1 thành 7), chuẩn hóa lại:
                if unit == 'h' and 70 <= val <= 79:
                    val -= 60
                elif unit in ('m', 's') and 70 <= val <= 79:
                    val -= 60
                if unit == 'h' and val >= 24:
                    return None
                if unit in ('m', 's') and val >= 60:
                    return None
                total += val * mult[unit]
            return total if total > 0 else None
        if allow_unitless_seconds:
            m_sec = re.fullmatch(r'([1-5]?\d)[5$]', s)
            if m_sec and int(m_sec.group(1)) > 0:
                return int(m_sec.group(1))
        return None

    def words(self, screen, roi):
        self.check()
        return self.ui.ocr_positions(screen, roi, scale=1)

    def regular_builders(self, screen):
        """The left menu footer is the regular-builder counter, not Goblin's."""
        if screen is None or not self.ui.is_menu_open(screen):
            return None
        self.check()
        heading = ' '.join(self.ui.texts(screen, (1170, 0, 430, 55))).lower()
        if 'building' not in heading and 'heroes' not in heading:
            return None
        text = ''.join(self.ui.texts(screen, (1180, 835, 90, 60)))
        match = re.search(r'([0-7])\s*[/7|]\s*([1-8])', text)
        if match:
            raw_free = int(match[1])
            raw_total = int(match[2])
            # Check if Goblin Builder is active (costs Gems, not regular).
            # 1. raw_total > 6 is guaranteed to include Goblin (max regular builders is 6).
            # 2. Or paid builder was detected on HUD (green face).
            # 3. Or the icon in Tab 1 has Goblin green hue (> 0.15).
            is_goblin = (raw_total > 6) or getattr(self.ui, 'paid_builder_visible', False)
            if not is_goblin and hasattr(screen, 'shape') and screen.shape[:2] == (900, 1600):
                tab_icon = screen[810:850, 1200:1245]
                tab_green = (cv2.inRange(cv2.cvtColor(tab_icon, cv2.COLOR_BGR2HSV), (30, 70, 70), (85, 255, 255)) > 0).mean()
                if tab_green > 0.15:
                    is_goblin = True

            if is_goblin:
                free = max(0, raw_free - 1)
                total = min(6, max(1, raw_total - 1))
            else:
                free = min(raw_total, raw_free)
                total = raw_total
            self.publish_builders(free, total)
            return free
        return None

    @staticmethod
    def currency(screen, roi):
        if not isinstance(screen, np.ndarray):
            return None
        x, y, w, h = roi
        crop = screen[max(0, y):max(0, y)+h, max(0, x):max(0, x)+w]
        if not crop.size:
            return None
        hue, sat, val = cv2.split(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV))
        counts = dict(
            elixir=int(((hue >= 125) & (hue <= 175) & (sat > 110) & (val > 150)).sum()),
            gold=int(((hue >= 12) & (hue <= 38) & (sat > 120) & (val > 170)).sum()),
            dark_elixir=int(((hue >= 110) & (hue <= 175) & (sat > 25) & (val > 25) & (val < 135)).sum()))
        # Pick the dominant currency color so minor border/badge pixels (e.g. 12 gold vs 190 dark_elixir) never win.
        best_name, best_count = max(counts.items(), key=lambda kv: kv[1])
        return best_name if best_count >= 12 else None

    def _parse_menu_rows(self, screen, words, start=50, end=795, custom_mode=False):
        """Parse candidate rows and collect diagnostic details for rejected or incomplete rows."""
        candidates = []
        diagnostics = []
        for text, x, y in words:
            if not (1230 <= x <= 1460 and start + 10 < y < end - 20):
                continue
            name = re.sub(r'\s*[xX×]\s*\d+\s*$', '', text).strip()
            key = self.normalized(name)
            allowed_names = self.custom_regular_names if custom_mode else self.base_regular_names
            if key not in allowed_names:
                if key not in self.custom_regular_names:
                    continue
                diagnostics.append(dict(name=name, x=x, y=y, cost=0, resource=None, duration_sec=0,
                                        supercharge=False, allowed=False, reject_reason='Ngoài danh mục Suggested mặc định'))
                continue
            if key.startswith('wall'):
                continue

            # New construction has its own placement flow and must never be
            # sent through the ordinary upgrade confirmation.
            if any(re.fullmatch(r'new', t.strip(), re.I) and 1230 <= nx <= 1450
                   and y + 4 <= ny <= y + 40 for t, nx, ny in words):
                continue

            # Duration parse (optional; never discard candidate if duration OCR fails)
            duration_sec = 0
            dur_candidates = [
                token for token, tx, ty in words if tx > 1410 and abs(ty - y) <= 18
            ]
            if dur_candidates:
                combined_dur = ''.join(
                    token for token, tx, ty in sorted(
                        [(t, tx, ty) for t, tx, ty in words if tx > 1410 and abs(ty - y) <= 18],
                        key=lambda item: item[1]
                    )
                )
                sec = self.parse_duration(combined_dur) or self.ui.duration_seconds(
                    re.sub(r'[iloO]', lambda m: '1' if m.group(0) in 'il' else '0', combined_dur.strip().lower())
                )
                if sec:
                    duration_sec = sec
            if not duration_sec:
                for raw_tok in dur_candidates:
                    sec = self.parse_duration(raw_tok)
                    if not sec:
                        cleaned_tok = raw_tok.strip().lower()
                        cleaned_tok = re.sub(r'[iloO]', lambda m: '1' if m.group(0) in 'il' else '0', cleaned_tok)
                        sec = self.ui.duration_seconds(cleaned_tok)
                    if sec:
                        duration_sec = sec
                        break

            # Prices parse with OCR digit normalization (price row is ~23px below title, never 48px on next row)
            price_tokens = []
            for token, px, py in words:
                if px > 1350 and y + 10 <= py <= y + 36:
                    if self.parse_duration(token, allow_unitless_seconds=False) is not None:
                        continue
                    norm_p = token.replace('O', '0').replace('o', '0').replace('l', '1').replace('I', '1').replace('i', '1')
                    if re.search(r'[a-zA-Z]', norm_p):
                        continue
                    digits = re.sub(r'[^0-9]', '', norm_p)
                    if digits:
                        price_tokens.append((px, py, digits))
            if not price_tokens:
                diagnostics.append(dict(name=name, x=x, y=y, cost=0, resource=None, duration_sec=duration_sec,
                                        supercharge=False, allowed=False, reject_reason='Không đọc được giá nâng cấp từ OCR'))
                continue

            price_y = price_tokens[0][1]
            cost_str = ''.join(d for _, _, d in sorted(price_tokens, key=lambda p: p[0]))
            cost = int(cost_str) if cost_str.isdigit() else 0
            if cost <= 0:
                diagnostics.append(dict(name=name, x=x, y=y, cost=0, resource=None, duration_sec=duration_sec,
                                        supercharge=False, allowed=False, reject_reason='Giá đọc được không hợp lệ (<= 0)'))
                continue

            resource = self.currency(screen, (1560, max(0, price_y - 16), 38, 34))
            if not resource:
                diagnostics.append(dict(name=name, x=x, y=y, cost=cost, resource=None, duration_sec=duration_sec,
                                        supercharge=False, allowed=False,
                                        reject_reason=f'Không nhận diện được icon tiền tệ ở (1560, {max(0, price_y - 16)})'))
                continue

            row = dict(
                name=name,
                x=x,
                y=y,
                cost=cost,
                resource=resource,
                duration_sec=duration_sec,
                category=self.category_of(name),
                supercharge=self.is_supercharge_row(screen, y),
            )
            reject_reason = self.diagnose_row_rejection(row, custom_mode=custom_mode)
            diagnostics.append(dict(row, allowed=(reject_reason is None), reject_reason=reject_reason))
            if reject_reason is None:
                candidates.append(row)
        return sorted(candidates, key=lambda row: row['y']), sorted(diagnostics, key=lambda row: row['y'])

    def menu_candidates(self, screen, words, start=50, end=795, custom_mode=False):
        candidates, _ = self._parse_menu_rows(screen, words, start=start, end=end, custom_mode=custom_mode)
        return candidates

    def find_candidate(self, wallet, custom_mode=None):
        if custom_mode is None:
            custom_mode = self.custom_mode_enabled()
        self.unaffordable = {}
        self.target_candidate = None
        if custom_mode:
            return self._find_custom_candidate(wallet)
        # Return to the Suggested header; active jobs above it are never candidates.
        previous = None
        for _ in range(14):
            self.check()
            screen = self.adb.screencap()
            words = self.words(screen, (1170, 50, 430, 755))
            headers = [y for text, _, y in words if 'suggest' in text.lower()]
            signature = self.ui.menu_signature(words)
            if headers or signature == previous:
                break
            previous = signature
            self.ui.scroll_menu('up', fast=True)
        if not headers:
            return None, set(), 'Chưa xác nhận được mục Suggested upgrades; không chọn hàng đang nâng.'
        needed = set()
        previous = None
        start = min(headers)
        for _ in range(14):
            self.check()
            ends = [y for text, _, y in words if 'other upgrade' in text.lower() and y > start]
            end = min(ends) if ends else 795
            for candidate in self.menu_candidates(screen, words, start, end):
                resource = candidate['resource']
                if wallet.get('valid', {}).get(resource) is True:
                    if wallet[resource] >= candidate['cost']:
                        return candidate, needed, ''
                    needed.add(resource)
                    self.unaffordable[resource] = min(candidate['cost'], self.unaffordable.get(resource, candidate['cost']))
            signature = self.ui.menu_signature(words)
            if ends or signature == previous:
                break
            previous = signature
            self.ui.scroll_menu('down', fast=True)
            screen = self.adb.screencap()
            words = self.words(screen, (1170, 50, 430, 755))
            start = 40
        return None, needed, 'Không có gợi ý nâng hợp lệ/đủ tiền cho thợ thường.'

    def _find_custom_candidate(self, wallet):
        settings = getattr(self.village, 'settings', None)
        priority = settings.get('building_upgrade_priority', 'time_min') if settings else 'time_min'
        use_filter = settings.get('building_upgrade_use_menu_filter', True) if settings else True
        if use_filter:
            if not self.apply_menu_filter_if_needed():
                return None, set(), 'Chưa xác minh được bộ lọc tài nguyên trong menu thợ.'

        # Check if already at top (allow y < 430 when 1-6 ongoing builders occupy the top area)
        screen = self.adb.screencap()
        words = self.words(screen, (1170, 50, 430, 755))
        top_headers = [y for text, _, y in words if any(k in text.lower() for k in self._HEADER_KEYWORDS) and y < 430]
        if not top_headers:
            previous = None
            for _ in range(6):
                self.check()
                self.ui.scroll_menu('up', fast=True)
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                headers = [y for text, _, y in words if any(k in text.lower() for k in self._HEADER_KEYWORDS) and y < 430]
                signature = self.ui.menu_signature(words)
                if headers or signature == previous:
                    break
                previous = signature

        # In cost_min, cost_max or suggested: the game menu filter already ordered items!
        # Check page 0 candidates first for instantaneous selection
        headers = [y for text, _, y in words if any(k in text.lower() for k in self._HEADER_KEYWORDS)]
        start = min(headers) if headers else 45
        p0_rows = self.menu_candidates(screen, words, start=start, end=795, custom_mode=True)

        if priority in ('suggested', 'cost_min', 'cost_max'):
            # When cost_min or cost_max is selected with Builder Menu filter,
            # the game natively sorted the items. If no category-filtered row matched on page 0,
            # fallback to all page 0 non-wall candidates so the top row under Filtered is selected immediately!
            candidates_to_check = p0_rows
            if not candidates_to_check and use_filter and priority in ('cost_min', 'cost_max'):
                candidates_to_check = self.menu_candidates(screen, words, start=start, end=795, custom_mode=False)

            # If an affordable candidate exists, return it immediately without scrolling!
            for row in candidates_to_check:
                res = row['resource']
                if wallet.get('valid', {}).get(res) is True and wallet[res] >= row['cost']:
                    self.target_candidate = dict(row, page=0)
                    return row, set(), ''

            # If page 0 has candidates but none affordable, the top candidate is the optimal target!
            if candidates_to_check:
                top_cand = candidates_to_check[0]
                self.target_candidate = dict(top_cand, page=0)
                needed = {top_cand['resource']}
                self.unaffordable[top_cand['resource']] = top_cand['cost']
                dur_text = f" ({top_cand['duration_sec']//3600}h)" if isinstance(top_cand.get('duration_sec'), int) and top_cand['duration_sec'] > 0 else ""
                return (
                    None,
                    needed,
                    f"Mục tiêu tiếp theo: {top_cand['name']}{dur_text} cần {top_cand['cost']:,} {top_cand['resource']} "
                    f"(hiện có {wallet.get(top_cand['resource'], 0):,}).",
                )

        # For time_min / time_max or if page 0 had no allowed candidates, scan up to 5 pages
        collected = []
        seen_keys = set()
        for r in p0_rows:
            dedup = (self.canonical_key(r['name']), r['cost'], r['resource'])
            if dedup not in seen_keys:
                seen_keys.add(dedup)
                collected.append(dict(r, page=0))

        previous = self.ui.menu_signature(words)
        for page in range(1, 5):
            self.check()
            self.ui.scroll_menu('down', fast=True)
            screen = self.adb.screencap()
            words = self.words(screen, (1170, 50, 430, 755))
            rows = self.menu_candidates(screen, words, start=45, end=795, custom_mode=True)
            for row in rows:
                dedup = (self.canonical_key(row['name']), row['cost'], row['resource'])
                if dedup not in seen_keys:
                    seen_keys.add(dedup)
                    collected.append(dict(row, page=page))
            if priority in ('suggested', 'cost_min', 'cost_max'):
                for row in rows:
                    res = row['resource']
                    if wallet.get('valid', {}).get(res) is True and wallet[res] >= row['cost']:
                        self.target_candidate = dict(row, page=page)
                        return row, set(), ''
            signature = self.ui.menu_signature(words)
            if signature == previous:
                break
            previous = signature

        if not collected:
            if priority == 'specific':
                spec_name = settings.get('building_upgrade_specific_target', 'công trình')
                self.specific_cooldown_until = time.monotonic() + 300
                msg = f"Không tìm thấy '{spec_name}' trong menu (có thể đã max cấp hoặc đang nâng). Tạm dừng 5 phút để làm việc khác."
                if hasattr(self.village, 'village_state') and self.village.village_state:
                    self.village.village_state.set_builder_plan(f"Không tìm thấy '{spec_name}' ➔ Tạm dừng")
                return None, set(), msg
            return None, set(), ('Không tìm thấy công trình nào khớp bộ lọc tùy chọn; '
                                 'kiểm tra danh sách công trình đã chọn và công trình đang nâng/max cấp.')

        ranked = self.sort_candidates(collected, priority=priority)
        best = ranked[0]
        self.target_candidate = best
        best_res = best['resource']
        if wallet.get('valid', {}).get(best_res) is True and wallet[best_res] >= best['cost']:
            sought = self._seek_candidate_row(best)
            if sought is not None:
                return sought, set(), ''
            return None, set(), f'Chưa cuộn lại được tới dòng {best["name"]}.'

        needed = {best_res}
        self.unaffordable[best_res] = best['cost']
        dur_text = f" ({best['duration_sec']//3600}h)" if isinstance(best.get('duration_sec'), int) and best['duration_sec'] > 0 else ""
        return (
            None,
            needed,
            f"Mục tiêu tiếp theo: {best['name']}{dur_text} cần {best['cost']:,} {best_res} "
            f"(hiện có {wallet.get(best_res, 0):,}).",
        )

    def _seek_candidate_row(self, target, start_page=None):
        target_page = target.get('page')
        if isinstance(start_page, int) and isinstance(target_page, int) and start_page != target_page:
            steps = abs(start_page - target_page)
            fast_dir = 'up' if start_page > target_page else 'down'
            for _ in range(min(steps, 6)):
                self.check()
                self.ui.scroll_menu(fast_dir, fast=True)
        for direction in ('up', 'down'):
            previous = None
            for _ in range(14):
                self.check()
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                rows = self.menu_candidates(screen, words, start=45, end=795, custom_mode=True)
                match = next(
                    (r for r in rows
                     if self.canonical_key(r['name']) == self.canonical_key(target['name'])
                     and r['cost'] == target['cost']
                     and r['resource'] == target['resource']),
                    None,
                )
                if match is not None:
                    return match
                signature = self.ui.menu_signature(words)
                if signature == previous:
                    break
                previous = signature
                self.ui.scroll_menu(direction, fast=True)
        return None

    def close_menu(self, screen):
        if isinstance(screen, np.ndarray) and hasattr(self.vision, 'find_okay_button'):
            okay = self.vision.find_okay_button(screen)
            if okay:
                self.adb.tap(okay[0], okay[1], jitter=0, delay_after=.4)
                screen = self.adb.screencap()
        if self.ui.is_menu_open(screen):
            close = self.vision.find_close_button(screen)
            if not close or not (close[0] > 1530 and close[1] < 55):
                return False
            self.adb.tap(*close[:2], jitter=0, delay_after=.3)
        return True

    def upgrade_close_button(self, screen):
        # The builder list can remain visible behind the upgrade dialog.
        # Prefer the foreground X over the menu X at the far right.
        return self.vision.match_template(screen, 'x', .88, roi=(1000, 10, 530, 170))

    @staticmethod
    def _is_magic_hammer_button(screen, x, y):
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        crop = screen[max(0, y - 65):min(900, y + 15), max(0, x - 45):min(1600, x + 45)]
        if not crop.size:
            return False
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        blue = cv2.inRange(hsv, (95, 110, 110), (135, 255, 255))
        return float((blue > 0).mean()) > 0.35

    def upgrade_button(self, screen, candidate):
        if self.ui.is_menu_open(screen):
            return None
        title = self.normalized(' '.join(self.ui.texts(screen, (150, 450, 1300, 280))))
        cand_name_norm = self.normalized(candidate['name'])
        cand_key = self.canonical_key(candidate['name'])
        words = self.words(screen, (150, 580, 1300, 260))
        points = [(x, y) for text, x, y in words if any(k in text.strip().lower() for k in ('upgrade', 'nang cap', 'nâng cấp', 'upg'))]
        if len(points) == 1:
            return points[0]
        if len(points) > 1:
            regular_pts = [pt for pt in sorted(points, key=lambda p: p[0]) if not self._is_magic_hammer_button(screen, *pt)]
            if regular_pts:
                return regular_pts[0]
        hammer_pt = self.vision.match_template(screen, 'bua', 0.8, roi=(200, 600, 1180, 230))
        if hammer_pt:
            return (hammer_pt[0] + hammer_pt[2] // 2, hammer_pt[1] + hammer_pt[3] // 2)
        return None

    def confirmation(self, screen, candidate):
        if screen is None:
            return None
        words = self.words(screen, (220, 20, 1230, 850))
        all_text = ' '.join(text for text, _, _ in words).lower()
        settings = getattr(self.village, 'settings', None)
        if settings is not None and not settings.get('building_upgrade_allow_supercharge', True):
            if 'supercharge' in all_text or 'this supercharge will last' in all_text:
                return None
        if re.search(r'\bgoblin builder\b|\bgems?\b|need more|buy resources|finish now', all_text):
            return None
        title = self.normalized(' '.join(text for text, _, y in words if y < 250))
        name = self.normalized(candidate['name'])
        ckey = self.canonical_key(candidate['name'])
        allowed_names = self.custom_regular_names if self.custom_mode_enabled() else self.base_regular_names
        if name not in allowed_names or name.startswith('wall'):
            return None
        if ckey == 'craftingstation':
            # The seasonal dialog is titled after its module (Cake-A-Pult,
            # Hot Candle, ...), and its Confirm can be in any of three rows.
            # Match the selected menu row by its exact price and currency.
            if not any('crafting phase is active' in text.lower() for text, _, _ in words):
                return None
            cost = candidate.get('cost')
            resource = candidate.get('resource')
            for text, px, py in words:
                if not (type(cost) is int and 1250 <= px <= 1380 and 180 <= py <= 590):
                    continue
                if int(re.sub(r'\D', '', text) or 0) != cost:
                    continue
                confirm = next(((cx, cy) for label, cx, cy in words
                                if re.search(r'confirm', label, re.I)
                                and abs(cx - px) <= 100 and 0 < py - cy <= 40), None)
                if confirm is None:
                    continue
                if self.currency(screen, (1360, py - 25, 75, 60)) != resource:
                    continue
                button = screen[max(0, py - 50):min(900, py + 15), 1230:1410]
                hsv = cv2.cvtColor(button, cv2.COLOR_BGR2HSV)
                green = cv2.inRange(hsv, (35, 75, 70), (95, 255, 255))
                if float((green > 0).mean()) >= 0.08:
                    return confirm
            return None

        name_matched = name in title or ckey in title
        if not name_matched:
            return None

        # Look for Confirm / Upgrade buttons in lower region (y > 550)
        cost_digits = str(candidate['cost'])
        action_candidates = []
        for text, x, y in words:
            if y < 550:
                continue
            t_lower = text.strip().lower()
            if any(k in t_lower for k in ('confirm', 'upgrade', 'nang cap', 'nâng cấp')):
                action_candidates.append((x, y))
            elif cost_digits in re.sub(r'[^0-9]', '', text):
                action_candidates.append((x, y))

        # Color fallback for Green upgrade action button if text didn't trigger
        if not action_candidates:
            hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
            green_mask = (hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 95) & (hsv[:, :, 1] >= 70) & (hsv[:, :, 2] >= 70)
            roi_mask = np.zeros_like(green_mask)
            roi_mask[600:850, 650:1250] = green_mask[600:850, 650:1250]
            contours, _ = cv2.findContours(roi_mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for cnt in contours:
                cx, cy, cw, ch = cv2.boundingRect(cnt)
                if cw >= 80 and ch >= 35:
                    action_candidates.append((cx + cw // 2, cy + ch // 2))

        if not action_candidates:
            return None

        # Exclude Magic Hammer buttons (e.g. Hammer of Building)
        regular_buttons = []
        for bx, by in action_candidates:
            if self._is_magic_hammer_button(screen, bx, by):
                continue
            is_hammer = any('hammer' in t.lower() for t, px, py in words if abs(px - bx) < 180 and abs(py - by) < 120)
            if is_hammer:
                continue
            regular_buttons.append((bx, by))

        if not regular_buttons:
            return None

        # Target the button with green background and matching resource currency
        target_btn = None
        for bx, by in regular_buttons:
            roi = (max(0, bx - 160), max(0, by - 70), 320, 135)
            crop = screen[roi[1]:roi[1] + roi[3], roi[0]:roi[0] + roi[2]]
            if not crop.size:
                continue
            green = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (35, 80, 70), (95, 255, 255))
            if (green > 0).mean() < 0.08:
                continue
            btn_curr = self.currency(screen, roi)
            if btn_curr and btn_curr == candidate['resource']:
                target_btn = (bx, by)
                break
            elif not btn_curr:
                wider_roi = (max(0, bx - 220), max(0, by - 80), 400, 160)
                if self.currency(screen, wider_roi) == candidate['resource']:
                    target_btn = (bx, by)
                    break

        if target_btn is None:
            for bx, by in regular_buttons:
                roi = (max(0, bx - 120), max(0, by - 50), 240, 100)
                crop = screen[roi[1]:roi[1] + roi[3], roi[0]:roi[0] + roi[2]]
                if crop.size:
                    green = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (35, 80, 70), (95, 255, 255))
                    if (green > 0).mean() >= 0.08:
                        target_btn = (bx, by)
                        break

        if target_btn is None:
            return None

        x, y = target_btn

        # Price verification with OCR typo normalization
        price_rows = {}
        for text, px, py in words:
            if not (x - 170 < px < x + 90 and 8 <= abs(py - y) <= 75):
                continue
            norm_tok = text.replace('O', '0').replace('o', '0').replace('l', '1').replace('I', '1').replace('i', '1')
            digits = re.sub(r'[^0-9]', '', norm_tok)
            if digits:
                row = next((r for r in price_rows if abs(py - r) <= 8), py)
                price_rows.setdefault(row, []).append((px, digits))

        if price_rows:
            row = min(price_rows, key=lambda r: abs(r - y))
            strike = screen[row - 6:row + 7, max(0, x - 115):x + 45].astype(np.int16)
            blue, green, red = cv2.split(strike)
            if not np.any(((red > 150) & (red > green + 70) & (red > blue + 70)).mean(axis=1) > 0.45):
                price = int(''.join(digits for _, digits in sorted(price_rows[row], key=lambda item: item[0])))
                expected = candidate['cost']
                if price == expected or (expected > 0 and 0.50 <= price / expected <= 1.50):
                    return (x, y)
            return (x, y)

        # Fallback to verified green confirm button
        return (x, y)

    def plan_multi_builders(self, wallet, needed_builders, log=None, custom_mode=None):
        if custom_mode is None:
            custom_mode = self.custom_mode_enabled()
        settings = getattr(self.village, 'settings', None)
        priority = settings.get('building_upgrade_priority', 'time_min') if settings else 'time_min'
        use_filter = settings.get('building_upgrade_use_menu_filter', True) if settings else True

        if priority == 'specific' and time.monotonic() < getattr(self, 'specific_cooldown_until', 0):
            rem = int(self.specific_cooldown_until - time.monotonic())
            msg = f"Tạm dừng kiểm tra công trình cụ thể (cooldown còn {rem}s để xử lý việc khác)."
            if log:
                log(f"[Thợ] ⏳ {msg}")
            return [], set(), msg

        if custom_mode and use_filter:
            self._active_log = log
            try:
                filter_ok = self.apply_menu_filter_if_needed()
            finally:
                self._active_log = None
            if not filter_ok:
                return [], set(), 'Chưa xác minh được bộ lọc tài nguyên trong menu thợ.'

        screen = self.adb.screencap()
        words = self.words(screen, (1170, 50, 430, 755))
        top_headers = [y for text, _, y in words if any(k in text.lower() for k in self._HEADER_KEYWORDS) and y < 430]
        if top_headers:
            if log:
                log(f'[Thợ] Đã ở đầu danh sách (tiêu đề tại y={min(top_headers)}).')
        else:
            previous = None
            for step_up in range(1, 7):
                self.check()
                if log:
                    log(f'[Thợ] ⬆️ Chưa thấy tiêu đề đầu danh sách -> cuộn lên (lần {step_up}/6)...')
                self.ui.scroll_menu('up', fast=True)
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                headers_up = [y for text, _, y in words if any(k in text.lower() for k in self._HEADER_KEYWORDS) and y < 430]
                signature = self.ui.menu_signature(words)
                if headers_up or signature == previous:
                    if log:
                        if headers_up:
                            log(f'[Thợ] Đã thấy tiêu đề đầu danh sách tại y={min(headers_up)}.')
                        else:
                            log('[Thợ] ⏹ Đã chạm đỉnh menu (nội dung không đổi khi cuộn lên).')
                    break
                previous = signature

        collected = []
        seen_keys = set()
        headers = [y for text, _, y in words if any(k in text.lower() for k in self._HEADER_KEYWORDS)]
        start = min(headers) if headers else 45

        # Scan active ongoing jobs appearing above headers (group by right-side timer so green progress bars hiding names never skip jobs)
        ongoing_jobs = self._extract_ongoing_jobs_from_words(screen, words, start) if start > 55 else []
        if ongoing_jobs and log:
            log('[Thợ] 🔨 Đang xây ở đầu menu: ' + ', '.join(f"{j['name']} ({j['duration_str']})" for j in ongoing_jobs))
        state = getattr(self.village, 'village_state', None)
        if state is not None and ongoing_jobs:
            state.set_ongoing_upgrades(ongoing_jobs)
        if ongoing_jobs and any(j.get('duration_sec', 0) > 0 for j in ongoing_jobs):
            self._last_ongoing_scan = (time.monotonic(), list(ongoing_jobs))
            if callable(getattr(self.village, 'save_account_builder_timers', None)):
                self.village.save_account_builder_timers(ongoing_jobs)

        def _log_page_scan(page_idx, page_screen, page_words, scan_start):
            accepted, diag = self._parse_menu_rows(page_screen, page_words, start=scan_start, end=795, custom_mode=custom_mode)
            if not accepted:
                patched = self.menu_candidates(page_screen, page_words, start=scan_start, end=795, custom_mode=custom_mode)
                if patched:
                    accepted = patched
            if log:
                log(f'[Thợ] 📄 Trang {page_idx} (start_y={scan_start}): OCR nhận {len(diag)} dòng công trình ({len(accepted)} đạt điều kiện).')
                for item in diag:
                    sc = ' ⚡Supercharge' if item.get('supercharge') else ''
                    if item.get('allowed'):
                        log(f"   ✅ {item['name']} | {item['cost']:,} {item['resource']}{sc}")
                    else:
                        cost_txt = f"{item['cost']:,}" if item.get('cost') else '?'
                        res_txt = item.get('resource') or '?'
                        log(f"   🚫 {item['name']} | {cost_txt} {res_txt}{sc} -> Loại: {item.get('reject_reason')}")
                if not diag:
                    raw_tokens = [f'{t}@{py}' for t, _px, py in page_words if py >= scan_start]
                    log(f"   [OCR thô trang {page_idx}]: {', '.join(raw_tokens[:16]) or '(trống)'}")
            return accepted

        def _body_sig(page_words, scan_start):
            body = [w for w in page_words if w[2] >= scan_start]
            return self.ui.menu_signature(body if body else page_words)

        p0_rows = _log_page_scan(0, screen, words, start)
        for r in p0_rows:
            dedup = (self.canonical_key(r['name']), r['cost'], r['resource'])
            if dedup not in seen_keys:
                seen_keys.add(dedup)
                collected.append(dict(r, page=0))

        current_page = 0
        need_more = len(collected) < needed_builders or priority not in ('suggested', 'cost_min', 'cost_max')
        if need_more and priority != 'specific':
            prev_sig = _body_sig(words, start)
            for page in range(1, 4):
                self.check()
                if log:
                    log(f'[Thợ] ⬇️ Cuộn xuống trang {page}/3 để quét thêm công trình...')
                self.ui.scroll_menu('down', fast=True)
                current_page = page
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                page_headers = [y for text, _, y in words if any(k in text.lower() for k in self._HEADER_KEYWORDS)]
                page_start = min(page_headers) if page_headers else 45
                rows = _log_page_scan(page, screen, words, page_start)
                for r in rows:
                    dedup = (self.canonical_key(r['name']), r['cost'], r['resource'])
                    if dedup not in seen_keys:
                        seen_keys.add(dedup)
                        collected.append(dict(r, page=page))
                sig = _body_sig(words, page_start)
                if sig == prev_sig or len(collected) >= needed_builders * 2:
                    if log and sig == prev_sig:
                        log('[Thợ] ⏹ Danh sách không thay đổi sau khi cuộn xuống -> dừng quét.')
                    break
                prev_sig = sig
        elif priority == 'specific' and not collected:
            prev_sig = _body_sig(words, start)
            for page in range(1, 5):
                self.check()
                if log:
                    log(f"[Thợ] ⬇️ Cuộn xuống trang {page}/4 tìm công trình cụ thể...")
                self.ui.scroll_menu('down', fast=True)
                current_page = page
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                page_headers = [y for text, _, y in words if any(k in text.lower() for k in self._HEADER_KEYWORDS)]
                page_start = min(page_headers) if page_headers else 45
                rows = _log_page_scan(page, screen, words, page_start)
                for r in rows:
                    dedup = (self.canonical_key(r['name']), r['cost'], r['resource'])
                    if dedup not in seen_keys:
                        seen_keys.add(dedup)
                        collected.append(dict(r, page=page))
                sig = _body_sig(words, page_start)
                if sig == prev_sig or collected:
                    if log and sig == prev_sig:
                        log('[Thợ] ⏹ Danh sách không thay đổi sau khi cuộn xuống -> dừng quét.')
                    break
                prev_sig = sig
        self._last_menu_page = current_page

        if not collected:
            if custom_mode and priority == 'specific':
                spec_name = settings.get('building_upgrade_specific_target', 'công trình')
                self.specific_cooldown_until = time.monotonic() + 300
                msg = f"Không tìm thấy '{spec_name}' trong menu (có thể đã max cấp hoặc đang nâng). Tạm dừng 5 phút để chuyển sang tính năng khác."
                if log:
                    log(f"[Thợ] ⚠️ {msg}")
                if hasattr(self.village, 'village_state') and self.village.village_state:
                    self.village.village_state.set_builder_plan(f"Không tìm thấy '{spec_name}' ➔ Tạm dừng")
                return [], set(), msg
            if custom_mode and use_filter and priority in ('cost_min', 'cost_max'):
                collected = [dict(r, page=0) for r in self.menu_candidates(screen, words, start=start, end=795, custom_mode=False)]

        if not collected:
            return [], set(), ('Không tìm thấy công trình nào khớp bộ lọc tùy chọn; '
                               'kiểm tra danh sách công trình đã chọn và công trình đang nâng/max cấp.')

        ranked = self.sort_candidates(collected, priority=priority)

        virtual_wallet = {
            'gold': wallet.get('gold', 0),
            'elixir': wallet.get('elixir', 0),
            'dark_elixir': wallet.get('dark_elixir', 0),
        }
        plan = []
        needed_res = set()
        cands_pool = list(ranked)

        for b_idx in range(1, needed_builders + 1):
            if not cands_pool:
                break
            cand = cands_pool.pop(0)
            res = cand['resource']
            cost = cand['cost']
            avail = virtual_wallet.get(res, 0)
            if avail >= cost:
                virtual_wallet[res] -= cost
                plan.append({
                    'builder_idx': b_idx,
                    'candidate': cand,
                    'is_ready': True,
                    'cost': cost,
                    'resource': res,
                    'balance_after': virtual_wallet[res],
                })
            else:
                deficit = cost - avail
                needed_res.add(res)
                self.unaffordable[res] = min(cost, self.unaffordable.get(res, cost))
                plan.append({
                    'builder_idx': b_idx,
                    'candidate': cand,
                    'is_ready': False,
                    'cost': cost,
                    'resource': res,
                    'deficit': deficit,
                    'balance_after': avail,
                })

        res_symbols = {'gold': '🟡', 'elixir': '🟣', 'dark_elixir': '⚫'}
        items_summary = []
        for p in plan:
            c = p['candidate']
            sym = res_symbols.get(p['resource'], '')
            cost_str = f"{p['cost']/1_000_000:.1f}M".replace('.0M', 'M') if p['cost'] >= 1_000_000 else f"{p['cost']//1000}k"
            if p['is_ready']:
                items_summary.append(f"[{p['builder_idx']}] {c['name']} ({cost_str} {sym}) ✅")
            else:
                def_str = f"{p['deficit']/1_000_000:.1f}M".replace('.0M', 'M') if p['deficit'] >= 1_000_000 else f"{p['deficit']//1000}k"
                items_summary.append(f"[{p['builder_idx']}] {c['name']} ({cost_str} {sym}) ⏳ Thiếu {def_str}")

        is_maxed = getattr(self.village, 'walls_confirmed_maxed', None) is True if self.village else False
        reserve_count = 0 if is_maxed else (self.village.settings.get('reserve_builders', 0) if self.village and hasattr(self.village, 'settings') else 0)
        if is_maxed:
            summary_str = f"{needed_builders} thợ rảnh (tường đã Max → dùng hết thợ, duy trì 0): " + " | ".join(items_summary)
        else:
            summary_str = f"{needed_builders} thợ rảnh (dự trữ {reserve_count}): " + " | ".join(items_summary)
        if hasattr(self.village, 'village_state') and self.village.village_state:
            self.village.village_state.set_builder_plan(summary_str)

        if log:
            if is_maxed:
                log(f"📋 [Kế hoạch thợ] Đang có {needed_builders} thợ rảnh (Tường đã Max → sử dụng hết toàn bộ thợ khả dụng, duy trì 0). Lập kế hoạch phân công:")
            else:
                log(f"📋 [Kế hoạch thợ] Đang có {needed_builders} thợ rảnh (dự trữ {reserve_count}). Lập kế hoạch phân công:")
            res_names = {'gold': 'Vàng', 'elixir': 'Dầu hồng', 'dark_elixir': 'Dầu đen'}
            for p in plan:
                c = p['candidate']
                r_name = res_names.get(p['resource'], p['resource'])
                if p['is_ready']:
                    log(f"   🔹 Thợ {p['builder_idx']}/{needed_builders}: {c['name']} ({p['cost']:,} {r_name}) ➔ [✅ Đủ tiền, dư ví ảo: {p['balance_after']:,} {r_name}]")
                else:
                    log(f"   🔸 Thợ {p['builder_idx']}/{needed_builders}: {c['name']} ({p['cost']:,} {r_name}) ➔ [⏳ Chờ farm: thiếu {p['deficit']:,} {r_name}]")

        return plan, needed_res, ''

    def execute_builder_plan(self, plan, log, maintain_reserve=False,
                             before=None, builders_before=None):
        ready_items = [p for p in plan if p.get('is_ready')]
        if not ready_items:
            unready = [p for p in plan if not p.get('is_ready')]
            if unready:
                first_unready = unready[0]
                self.target_candidate = first_unready['candidate']
                self.unaffordable[first_unready['resource']] = first_unready['cost']
                return dict(
                    status='needs_resources',
                    resources=[first_unready['resource']],
                    costs={first_unready['resource']: first_unready['cost']},
                    target=first_unready['candidate'],
                    reason=f"Chờ farm thêm {first_unready['deficit']:,} {first_unready['resource']} cho {first_unready['candidate']['name']}."
                )
            return dict(status='done', reason='Không có công trình nào trong kế hoạch.')

        verified = 0
        self._step_verified = 0
        start_page = getattr(self, '_last_menu_page', None)
        hard_cap = getattr(self, '_step_started_at', time.monotonic()) + 180
        for index, item in enumerate(ready_items):
            if getattr(self, 'deadline', 0) > 0:
                self.deadline = min(hard_cap, max(self.deadline, time.monotonic() + 45))
            result = self._execute_builder_candidate(
                item['candidate'], before, builders_before, log,
                start_page=start_page if index == 0 else 0,
            )
            if result['status'] != 'progress':
                if result['status'] == 'uncertain' or verified == 0:
                    return result
                return dict(status='progress', count=verified,
                            reason=f'Đã xác minh {verified} công trình; dừng để lập lại kế hoạch: {result["reason"]}')
            verified += 1
            self._step_verified = verified
            if log:
                log(f'[Thợ] ✅ {result["reason"]}')
            if index == len(ready_items) - 1:
                break
            # Reuse the already-verified home snapshot when available.
            snap = getattr(self, '_last_verified_home', None)
            if isinstance(snap, tuple) and len(snap) == 3 and snap[2] == builders_before - 1:
                home, partial_reading, builders_before = snap
                before = self.vision.read_village_resources(home) if callable(getattr(self.vision, 'read_village_resources', None)) else partial_reading
            else:
                home = self.adb.screencap()
                if not self.village.is_in_main_village(home):
                    self.close_menu(home)
                    home = self.adb.screencap()
                if not self.village.is_in_main_village(home):
                    if log:
                        log('[Thợ] Đã xác minh một nâng cấp; chưa trở lại làng để kiểm tra thợ tiếp theo.')
                    break
                before = self.vision.read_village_resources(home)
                builders_before = self.observe_home_builders(home)
            self._last_verified_home = None
            if builders_before is None or not self.ui.ensure_menu_open():
                if log:
                    log('[Thợ] Đã xác minh một nâng cấp; chưa mở lại được menu thợ cho công trình tiếp theo.')
                break
        return dict(status='progress', count=verified,
                    reason=f'Đã xác minh khởi công {verified} công trình; còn lại sẽ lập kế hoạch từ số dư mới.')

    def _confirm_stable_on_second_frame(self, frame1, confirm, cand) -> bool:
        time.sleep(0.08)
        frame2 = self.adb.screencap()
        if (isinstance(frame1, np.ndarray) and isinstance(frame2, np.ndarray)
                and frame1.shape == frame2.shape == (900, 1600, 3)):
            cx, cy = confirm
            p1 = frame1[max(0, cy-45):min(900, cy+45), max(0, cx-120):min(1600, cx+120)]
            p2 = frame2[max(0, cy-45):min(900, cy+45), max(0, cx-120):min(1600, cx+120)]
            if p1.size and float(np.mean(np.abs(p1.astype(np.int16) - p2.astype(np.int16)))) < 4.0:
                return True
        return self.confirmation(frame2, cand) == confirm

    def _execute_builder_candidate(self, cand, before, builders_before, log, start_page=None):
        resource = cand['resource']
        if (type(builders_before) is not int or not isinstance(before, dict)
                or before.get('valid', {}).get(resource) is not True
                or before.get(resource, 0) < cand['cost']):
            return dict(status='blocked', reason='Chưa xác minh số thợ và tài nguyên trước khi nâng.')

        sought = self._seek_candidate_row(cand, start_page=start_page)
        if sought is None:
            return dict(status='blocked', reason=f'Không còn thấy hàng {cand["name"]} đúng giá; chưa bấm nâng.')
        # Verify the row on a fresh frame using only the narrow strip around sought['y'].
        live = self.adb.screencap()
        strip_top = max(50, int(sought['y']) - 28)
        strip_h = min(805 - strip_top, 90)
        rows = self.menu_candidates(
            live,
            self.words(live, (1170, strip_top, 430, strip_h)),
            start=strip_top - 15,
            end=strip_top + strip_h,
            custom_mode=True,
        )
        if not rows:
            rows = self.menu_candidates(live, self.words(live, (1170, 50, 430, 755)),
                                        start=45, end=795, custom_mode=True)
        row = next((r for r in rows
                    if self.canonical_key(r['name']) == self.canonical_key(cand['name'])
                    and r['cost'] == cand['cost'] and r['resource'] == resource
                    and abs(r['y'] - sought['y']) <= 25), None)
        if row is None:
            return dict(status='blocked', reason=f'Hàng {cand["name"]} đã dịch chuyển; chưa bấm nâng.')

        self.adb.tap(1380, row['y'] + 15, jitter=0, delay_after=0.4)
        frame1 = self.adb.screencap()
        confirm = self.confirmation(frame1, cand)
        if confirm is None or not self._confirm_stable_on_second_frame(frame1, confirm, cand):
            return dict(status='blocked', reason=f'Chưa thấy Confirm đúng giá của {cand["name"]}; chưa bấm nâng.')

        self.pending = dict(cand, before=before[resource], builders_before=builders_before,
                            account=getattr(self.village, 'current_account_key', None),
                            started_at=time.monotonic())
        if log:
            log(f'[Thợ] Đã bấm Confirm {cand["name"]} ({cand["cost"]:,} {resource}); đang xác minh giao dịch.')
        self.adb.tap(*confirm, jitter=0, delay_after=0.7)
        for _ in range(10):
            self.check()
            frame = self.adb.screencap()
            if self.village.is_in_main_village(frame):
                result = self.reconcile_pending(frame)
                if result['status'] == 'progress':
                    if isinstance(cand.get('duration_sec'), int) and cand['duration_sec'] > 0:
                        record = getattr(self.village, 'record_started_builder_upgrade', None)
                        if callable(record):
                            record(cand['name'], cand['duration_sec'])
                    return result
            else:
                close_btn = self.upgrade_close_button(frame)
                if close_btn:
                    self.adb.tap(*close_btn[:2], jitter=0, delay_after=0.25)
                else:
                    self.close_menu(frame)
            time.sleep(0.25)
        return dict(status='uncertain', reason=(
            f'Đã gửi nâng {cand["name"]} nhưng chưa khớp tiền trừ và số thợ giảm; '
            'giữ giao dịch chờ, không bấm nâng lặp.'))

    def publish_builders(self, count, total=None):
        state = getattr(self.village, 'village_state', None)
        if state is not None:
            if type(count) is int and 0 <= count <= 6:
                state.free_builders = count
            if type(total) is int and 1 <= total <= 6:
                state.total_builders = total

    def observe_home_builders(self, screen):
        count = self.ui.builders(screen)
        total = getattr(self.ui, 'total_builders_read', 6) or 6
        self.publish_builders(count, total)
        return count

    def reconcile_pending(self, screen=None, free=None):
        """Resolve a sent purchase from fresh home evidence without tapping again."""
        pending = self.pending
        if not isinstance(pending, dict):
            return dict(status='idle', reason='Không có giao dịch công trình chờ xác minh.')
        account = pending.get('account')
        if account is not None and account != getattr(self.village, 'current_account_key', None):
            return dict(status='uncertain', reason='Tài khoản đã đổi; giữ giao dịch cũ, không bấm nâng lặp.')
        resource = pending.get('resource')
        before = pending.get('before')
        cost = pending.get('cost')
        builders_before = pending.get('builders_before')
        if not (isinstance(before, int) and isinstance(cost, int) and
                isinstance(builders_before, int) and resource):
            return dict(status='uncertain', reason='Thiếu mốc xác minh giao dịch; không bấm nâng lặp.')
        matches = 0
        last_frame = None
        last_reading = None
        read_single = getattr(self.vision, 'read_single_resource', None)
        for _ in range(2):
            frame = screen if screen is not None else self.adb.screencap()
            screen = None
            if frame is None or not self.village.is_in_main_village(frame):
                break
            builders = free if free is not None else self.observe_home_builders(frame)
            free = None
            if builders != builders_before - 1:
                break
            reading = read_single(frame, resource) if callable(read_single) else self.vision.read_village_resources(frame)
            if (reading.get('valid', {}).get(resource) is True and
                    before - reading[resource] == cost):
                matches += 1
                last_frame = frame
                last_reading = reading
            else:
                break
        if matches == 2:
            if last_frame is not None:
                self._last_verified_home = (last_frame, last_reading, builders_before - 1)
            self.assignments.append(dict(pending, builders_after=builders_before - 1))
            self.pending = None
            self.target_candidate = None
            self.publish_builders(builders_before - 1)
            return dict(status='progress', reason=f'Đã xác minh nâng {pending["name"]} từ hai ảnh làng.')
        return dict(status='uncertain', reason=(
            f'Chưa xác minh nâng {pending.get("name", "công trình")}; giữ giao dịch chờ, '
            'không bấm nâng lần nữa.'))

    def step(self, log, *, maintain_reserve=False, custom_mode=None, preopened=None):
        now = time.monotonic()
        self._step_started_at = now
        self._step_verified = 0
        self._last_verified_home = None
        self.deadline = now + 90
        try:
            return self._timed_step(log, maintain_reserve=maintain_reserve,
                                    custom_mode=custom_mode, preopened=preopened)
        finally:
            self.deadline = 0

    def _timed_step(self, log, *, maintain_reserve, custom_mode, preopened):
        if self.pending is not None:
            result = self.reconcile_pending()
            self.last_status = result['reason']
            return result
        self.opened_menu = False
        try:
            result = self._step(log, maintain_reserve=maintain_reserve, custom_mode=custom_mode, preopened=preopened)
        except BotStoppedError:
            raise
        except Exception as exc:
            verified = getattr(self, '_step_verified', 0)
            if verified > 0 and self.pending is None:
                result = dict(
                    status='progress',
                    count=verified,
                    reason=f'Đã xác minh khởi công {verified} công trình trước khi hết thời gian kiểm tra tiếp ({exc}).',
                )
            else:
                result = dict(status='uncertain' if self.pending else 'blocked',
                              reason=f'Chưa hoàn tất phân công thợ ({exc}).')
        self.last_status = result.get('reason', self.last_status)
        if self.opened_menu and result['status'] != 'progress':
            # A direct upgrade can leave both its dialog and the builder menu.
            for _ in range(2):
                screen = self.adb.screencap()
                if self.village.is_in_main_village(screen):
                    break
                close = self.upgrade_close_button(screen)
                if close:
                    self.adb.tap(*close[:2], jitter=0, delay_after=.3)
                elif self.ui.close_dialog() is False:
                    break
        return result

    def _step(self, log, *, maintain_reserve, custom_mode=None, preopened=None):
        def target():
            if getattr(self.village, 'walls_confirmed_maxed', None) is True:
                return 0
            return int(self.village.settings.get('reserve_builders', 0)) if maintain_reserve else 0

        if custom_mode is None:
            custom_mode = self.custom_mode_enabled()

        if self.pending is not None:
            return self.reconcile_pending()

        self.check()
        screen = self.adb.screencap()
        if preopened is not None:
            if not self.ui.is_menu_open(screen):
                return dict(status='blocked', reason='Menu thợ đã thay đổi; kiểm tra lại từ làng.')
            wallet, before = preopened
            self.opened_menu = True
        else:
            if not self.close_menu(screen):
                return dict(status='blocked', reason='Chưa đóng được menu thợ.')
            screen = self.adb.screencap()
            if screen is None or not self.village.is_in_main_village(screen):
                return dict(status='blocked', reason='Chưa xác nhận làng chính trước khi nâng công trình.')
            wallet = self.vision.read_village_resources(screen)
            before = self.vision.read_village_resources(self.adb.screencap())
            self.opened_menu = True
            if not self.ui.ensure_menu_open():
                return dict(status='blocked', reason='Không mở được menu thợ thường.')
        first = self.regular_builders(self.adb.screencap())
        second = self.regular_builders(self.adb.screencap())
        if first is None or first != second:
            return dict(status='blocked', reason='Chưa đọc chắc số thợ thường; không tính Goblin Builder.')
        self.publish_builders(first)
        if first <= target():
            # Khi đã mở sẵn menu thợ trên màn hình thật và xác nhận hết thợ rảnh,
            # quét ngay danh sách thời gian thợ đang xây trước khi đóng menu (tránh đóng rồi mở lại ngay bị hụt).
            if isinstance(screen, np.ndarray) and screen.shape[:2] == (900, 1600):
                try:
                    self.scan_ongoing_builders(close_after=False)
                except Exception:
                    pass
            self.close_menu(self.adb.screencap())
            return dict(status='done', reason=f'Đã xác nhận {first} thợ thường rảnh; mức duy trì {target()}.')

        needed_builders = first - target()
        if self.deadline > 0 and needed_builders > 1:
            hard_cap = getattr(self, '_step_started_at', time.monotonic()) + 180
            self.deadline = min(hard_cap, max(self.deadline, time.monotonic() + 45 * needed_builders))
        if custom_mode:
            plan, needed, reason = self.plan_multi_builders(wallet, needed_builders=needed_builders, log=log, custom_mode=True)
            if not plan:
                self.close_menu(self.adb.screencap())
                return dict(status='needs_resources' if needed else 'blocked', reason=reason,
                            resources=list(needed), costs=self.unaffordable, target=self.target_candidate)
            res = self.execute_builder_plan(plan, log, maintain_reserve=maintain_reserve,
                                            before=before, builders_before=first)
            self.close_menu(self.adb.screencap())
            return res

        candidate, needed, reason = self.find_candidate(wallet, custom_mode=custom_mode)
        if candidate is None:
            self.close_menu(self.adb.screencap())
            return dict(status='needs_resources' if needed else 'blocked', reason=reason,
                        resources=list(needed), costs=self.unaffordable, target=self.target_candidate)
        resource = candidate['resource']
        if (before.get('valid', {}).get(resource) is not True
                or before[resource] != wallet[resource] or before[resource] < candidate['cost']):
            return dict(status='blocked', reason='Số dư trước nâng chưa ổn định hoặc không đủ.')
        # Re-read the visible row with robust fallback to prevent false "dịch chuyển" aborts.
        fresh = self.adb.screencap()
        rows = self.menu_candidates(fresh, self.words(fresh, (1170, 50, 430, 755)), custom_mode=False)
        target_ckey = self.canonical_key(candidate['name'])
        live = next((row for row in rows
                     if self.canonical_key(row['name']) == target_ckey
                     and row.get('resource') == candidate.get('resource')
                     and abs(row['y'] - candidate['y']) <= 35), None)
        if live is None:
            # Fallback by canonical key match alone
            live = next((row for row in rows if self.canonical_key(row['name']) == target_ckey), None)

        tap_x = live['x'] if live else candidate.get('x', 1350)
        tap_y = live['y'] if live else candidate.get('y')
        if tap_y is None:
            self.close_menu(fresh)
            return dict(status='blocked', reason='Hàng gợi ý còn dịch chuyển; chưa bấm nâng.')
        self.adb.tap(tap_x, tap_y, jitter=0, delay_after=.4)
        selected = self.adb.screencap()
        previous = self.confirmation(selected, candidate)
        if previous is None and not self.upgrade_close_button(selected):
            # Older interactions first select the building. Never close the
            # menu before checking for a direct Confirm over the builder list.
            if self.close_menu(selected):
                point = self.upgrade_button(self.adb.screencap(), candidate)
                if point is not None:
                    self.adb.tap(*point, jitter=0, delay_after=.5)
        confirmation = None
        for _ in range(5):
            self.check()
            confirmation = self.confirmation(self.adb.screencap(), candidate)
            if confirmation is not None and confirmation == previous:
                break
            previous = confirmation
            time.sleep(.2)
        else:
            return dict(status='blocked', reason=f'Chưa xác minh Confirm của {candidate["name"]} theo giá gợi ý.')
        if maintain_reserve and not self.village.settings.get('main_village_enabled', True):
            return dict(status='done', reason='Farm làng chính đã tắt.')
        if first <= target():
            return dict(status='done', reason='Đã đạt mức thợ rảnh vừa đặt.')
        self.pending = dict(candidate, before=before[resource], builders_before=first,
                            account=getattr(self.village, 'current_account_key', None),
                            started_at=time.monotonic())
        log(f'[Công trình] Nâng {candidate["name"]}: {candidate["cost"]:,} {resource}, thợ thường rảnh {first}.')
        self.adb.tap(*confirmation, jitter=0, delay_after=1)
        stable = 0
        for _ in range(8):
            self.check()
            after_screen = self.adb.screencap()
            if not self.village.is_in_main_village(after_screen):
                if not self.upgrade_close_button(after_screen):
                    self.close_menu(after_screen)
                stable = 0
                time.sleep(.25)
                continue
            after = self.vision.read_village_resources(after_screen)
            valid = (after.get('valid', {}).get(resource) is True
                     and before[resource]-after[resource] == candidate['cost'])
            stable = stable+1 if valid else 0
            if stable >= 2:
                break
            time.sleep(.25)
        if stable >= 2 and self.ui.ensure_menu_open():
            counts = [self.regular_builders(self.adb.screencap()) for _ in range(2)]
            if counts == [first-1, first-1]:
                self.assignments.append(dict(self.pending, builders_after=first-1))
                self.pending = None
                self.target_candidate = None
                self.publish_builders(first-1)
                if maintain_reserve:
                    self.close_menu(self.adb.screencap())
                return dict(status='progress', reason=f'Đã xác minh {candidate["name"]}; còn {first-1} thợ thường rảnh.')
        return dict(status='uncertain', reason='Đã gửi nâng nhưng chưa khớp tiền trừ và số thợ giảm; không nâng tiếp hoặc tự đóng.')

    def _row_has_price_below(self, screen, words, row_y):
        """Kiểm tra xem hàng tại row_y có phải là công trình chưa xây (có giá tiền + icon tài nguyên bên dưới ~23px) không."""
        for token, px, py in words:
            if px > 1350 and row_y + 12 <= py <= row_y + 35:
                # Nếu token là chuỗi thời gian (chứa d/h/m/s) thì không phải là giá tiền
                if self.parse_duration(token, allow_unitless_seconds=False) is not None:
                    continue
                norm_p = token.replace('O', '0').replace('o', '0').replace('l', '1').replace('I', '1').replace('i', '1')
                if re.search(r'[a-zA-Z]', norm_p):
                    continue
                digits = re.sub(r'[^0-9]', '', norm_p)
                if len(digits) >= 3:
                    if isinstance(screen, np.ndarray) and screen.shape[:2] == (900, 1600):
                        if self.currency(screen, (1560, max(0, py - 16), 38, 34)) is not None:
                            return True
                    else:
                        return True
        if isinstance(screen, np.ndarray) and screen.shape[:2] == (900, 1600):
            if self.currency(screen, (1560, max(0, row_y + 10), 38, 30)) is not None:
                return True
        return False

    def _extract_ongoing_jobs_from_words(self, screen, words, start):
        jobs = []
        rows = {}
        for text, x, y in words:
            if x >= 1400 and 45 <= y < start - 6:
                baseline = next((old for old in rows if abs(old - y) <= 14), y)
                rows.setdefault(baseline, []).append((x, text))

        for ry, row in sorted(rows.items(), key=lambda item: item[0]):
            if self._row_has_price_below(screen, words, ry):
                continue
            combined = ''.join(text for _, text in sorted(row)).strip()
            sec = self.parse_duration(combined, allow_unitless_seconds=True)
            if not sec:
                cleaned = re.sub(r'[iloO]', lambda m: '1' if m.group(0) in 'il' else '0', combined.lower())
                sec = self.ui.duration_seconds(cleaned)
            if not sec:
                for _, tok in sorted(row):
                    sec = self.parse_duration(tok, allow_unitless_seconds=True)
                    if not sec:
                        cleaned_tok = re.sub(r'[iloO]', lambda m: '1' if m.group(0) in 'il' else '0', tok.strip().lower())
                        sec = self.ui.duration_seconds(cleaned_tok)
                    if sec:
                        break
            if sec and sec > 0:
                name_tokens = [
                    (nx, ntext.strip()) for ntext, nx, ny in words
                    if 1175 <= nx <= 1410 and abs(ny - ry) <= 18
                ]
                name = ' '.join(t for _, t in sorted(name_tokens)).strip() if name_tokens else 'Công trình'
                name = re.sub(r'\s*[xX×]\s*\d+\s*$', '', name).strip() or 'Công trình'
                dur_str = ' '.join(t.strip() for _, t in sorted(row)).strip()
                jobs.append({
                    'name': name,
                    'y': ry,
                    'duration': dur_str,
                    'duration_str': dur_str,
                    'duration_sec': int(sec),
                })
        return jobs

    def scan_ongoing_builders(self, close_after=False):
        """Quét chính xác danh sách các thợ đang xây dựng ở phần đầu menu thợ (phía trên 'Suggested upgrades').
        Tự động cuộn lên đầu danh sách và tắt bộ lọc nếu đang bị lọc ẩn công trình."""
        if not self.ui.ensure_menu_open():
            return []
        screen = self.adb.screencap()
        words = self.words(screen, (1170, 50, 430, 755))

        def find_suggest(w_list):
            return [y for text, _, y in w_list if any(k in text.lower() for k in ('suggest', 'gợi ý'))]

        def find_any_header(w_list):
            return [y for text, _, y in w_list if any(k in text.lower() for k in ('suggest', 'filter', 'other', 'gợi ý', 'đã lọc', 'lọc'))]

        suggest_headers = find_suggest(words)
        if not suggest_headers:
            previous = self.ui.menu_signature(words) if words else None
            for _ in range(8):
                self.check()
                self.ui.scroll_menu('up', fast=True)
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                suggest_headers = find_suggest(words)
                any_headers = find_any_header(words)
                signature = self.ui.menu_signature(words)
                if suggest_headers or (any_headers and min(any_headers) < 420) or (signature and signature == previous):
                    break
                previous = signature

            # Nếu vẫn đang ở chế độ 'Filtered upgrades' (hoặc bị ẩn 'Suggested upgrades' do Sort/Filter),
            # đặt lại bộ lọc về mặc định để thấy đầy đủ mọi thợ đang xây (kể cả Dầu hồng / Dầu đen).
            if not find_suggest(words) and isinstance(screen, np.ndarray) and screen.shape[:2] == (900, 1600):
                self.reset_menu_filter()
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                if not find_suggest(words):
                    previous = self.ui.menu_signature(words) if words else None
                    for _ in range(6):
                        self.check()
                        self.ui.scroll_menu('up', fast=True)
                        screen = self.adb.screencap()
                        words = self.words(screen, (1170, 50, 430, 755))
                        if find_suggest(words):
                            break
                        signature = self.ui.menu_signature(words)
                        if signature and signature == previous:
                            break
                        previous = signature

        headers = find_any_header(words)
        priced_candidates = self.menu_candidates(screen, words, start=45, end=795, custom_mode=True)
        first_priced_y = min((r['y'] for r in priced_candidates), default=None)

        if headers:
            start = min(headers)
            if first_priced_y is not None:
                start = min(start, first_priced_y - 8)
        elif first_priced_y is not None:
            start = first_priced_y - 8
        else:
            # Tối đa 6 thợ xây * 49px chỉ nằm trong vùng y < 430; tuyệt đối không quét xuống 795
            start = 430

        jobs_By_y = {}
        if start > 55:
            for j in self._extract_ongoing_jobs_from_words(screen, words, start):
                jobs_By_y[j['y']] = j

            # Quét bổ sung với scale=2 ở vùng đầu menu để đọc chính xác các bộ đếm ngắn như '39s'
            if isinstance(screen, np.ndarray) and screen.shape[:2] == (900, 1600):
                top_h = min(450, max(120, int(start) - 35))
                words_s2 = self.ui.ocr_positions(screen, (1170, 50, 430, top_h), scale=2)
                if words_s2:
                    s2_headers = find_any_header(words_s2)
                    s2_start = min(start, min(s2_headers)) if s2_headers else start
                    for j in self._extract_ongoing_jobs_from_words(screen, words_s2, s2_start):
                        matched_y = next((oy for oy in jobs_By_y if abs(oy - j['y']) <= 18), None)
                        if matched_y is None:
                            jobs_By_y[j['y']] = j
                        elif j['duration_sec'] < jobs_By_y[matched_y]['duration_sec']:
                            jobs_By_y[matched_y] = j

        jobs = [jobs_By_y[k] for k in sorted(jobs_By_y.keys())]
        if jobs:
            self._last_ongoing_scan = (time.monotonic(), list(jobs))
            if callable(getattr(self.village, 'save_account_builder_timers', None)):
                self.village.save_account_builder_timers(jobs)

        if close_after:
            self.close_menu(self.adb.screencap())
        return jobs

    def get_shortest_ongoing_builder_seconds(self):
        """Scan active ongoing construction timers appearing above headers in the builder menu.
        Returns the shortest seconds remaining (int), or None if no timers parsed."""
        recent = getattr(self, '_last_ongoing_scan', None)
        if isinstance(recent, tuple) and len(recent) == 2 and time.monotonic() - recent[0] < 10.0:
            elapsed = max(0, int(round(time.monotonic() - recent[0])))
            recent_durs = [
                max(1, int(j['duration_sec']) - elapsed)
                for j in (recent[1] or [])
                if isinstance(j.get('duration_sec'), int) and j['duration_sec'] > 0
            ]
            if recent_durs:
                return min(recent_durs)

        jobs = self.scan_ongoing_builders(close_after=True)
        durations = [int(j['duration_sec']) for j in jobs if isinstance(j.get('duration_sec'), int) and j['duration_sec'] > 0]
        if durations:
            return min(durations)
        if callable(getattr(self.village, 'get_account_shortest_builder_seconds', None)):
            saved_sec = self.village.get_account_shortest_builder_seconds()
            if isinstance(saved_sec, int) and saved_sec > 0:
                return saved_sec
        return None
