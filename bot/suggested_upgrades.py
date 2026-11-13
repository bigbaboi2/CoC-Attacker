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

    def is_candidate_allowed(self, candidate, custom_mode=False):
        name = candidate.get('name', '')
        key = self.normalized(name)
        if key not in self.regular_names or key.startswith('wall'):
            return False

        settings = getattr(self.village, 'settings', None)
        ckey = self.canonical_key(name)

        # 1. Town Hall Safety Lock: Strictly forbid Town Hall if lock is on
        lock_th = True if settings is None else bool(settings.get('lock_townhall_upgrade', True))
        if lock_th and (ckey in ('townhall', 'town_hall') or 'town hall' in name.lower() or 'nhà chính' in name.lower()):
            return False

        if not custom_mode:
            return True
        if settings is None:
            return True

        # 2. Specific Target Upgrade Priority: Scan ONLY for that specific building!
        priority = settings.get('building_upgrade_priority', 'time_min')
        if priority == 'specific':
            spec_target = (settings.get('building_upgrade_specific_target') or '').strip().lower()
            if spec_target:
                canon_target = self.canonical_key(spec_target)
                canon_target = BUILDING_KEY_ALIASES.get(canon_target, canon_target)
                canon_cand = BUILDING_KEY_ALIASES.get(ckey, ckey)
                if canon_cand != canon_target:
                    return False
                return True

        allowed_res = settings.get('building_upgrade_resources') or {
            'gold': True, 'elixir': True, 'dark_elixir': True
        }
        resource = candidate.get('resource')
        if resource and not allowed_res.get(resource, True):
            return False
        category = BUILDING_KEY_TO_CATEGORY.get(ckey)
        categories_cfg = settings.get('building_upgrade_categories') or {
            'traps': True, 'resource': True, 'defense': True, 'army': True, 'heroes': True
        }
        if category and not categories_cfg.get(category, True):
            return False
        whitelist = settings.get('building_upgrade_whitelist')
        if isinstance(whitelist, dict) and ckey in whitelist and not whitelist[ckey]:
            return False
        return True

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

    def apply_menu_filter_if_needed(self):
        """Use fixed coordinates on the Builder Menu Filter popup when enabled."""
        settings = getattr(self.village, 'settings', None)
        if not settings or not settings.get('auto_upgrade_buildings', False):
            return
        if not settings.get('building_upgrade_use_menu_filter', True):
            return
        pts = self.FIXED_FILTER_POINTS
        self.check()
        screen = self.adb.screencap()
        if not self.is_filter_popup_open(screen):
            self.adb.tap(*pts['button'], jitter=0, delay_after=0.6)
            screen = self.adb.screencap()
        if not self.is_filter_popup_open(screen):
            return
        res_cfg = settings.get('building_upgrade_resources') or {'gold': True, 'elixir': True, 'dark_elixir': True}
        for res_key, pt_key in (('gold', 'res_gold'), ('elixir', 'res_elixir'), ('dark_elixir', 'res_dark_elixir')):
            desired = bool(res_cfg.get(res_key, True))
            active = self.is_filter_button_active(screen, pts[pt_key], sorting=False)
            if active is not None and active != desired:
                self.adb.tap(*pts[pt_key], jitter=0, delay_after=0.3)
                screen = self.adb.screencap()
        priority = settings.get('building_upgrade_priority', 'time_min')
        if priority == 'specific':
            spec_target = (settings.get('building_upgrade_specific_target') or '').strip().lower()
            canon_target = self.canonical_key(spec_target)
            canon_target = BUILDING_KEY_ALIASES.get(canon_target, canon_target)
            target_res_tuple = BUILDING_KEY_TO_RESOURCES.get(canon_target)
            if target_res_tuple:
                for res_key, pt_key in (('gold', 'res_gold'), ('elixir', 'res_elixir'), ('dark_elixir', 'res_dark_elixir')):
                    desired = (res_key in target_res_tuple)
                    active = self.is_filter_button_active(screen, pts[pt_key], sorting=False)
                    if active is not None and active != desired:
                        self.adb.tap(*pts[pt_key], jitter=0, delay_after=0.3)
                        screen = self.adb.screencap()
        if priority in ('cost_max', 'cost_min'):
            target_pt_key = 'sort_cost_max' if priority == 'cost_max' else 'sort_cost_min'
            active = self.is_filter_button_active(screen, pts[target_pt_key], sorting=True)
            if active is False:
                self.adb.tap(*pts[target_pt_key], jitter=0, delay_after=0.3)
                screen = self.adb.screencap()
        else:
            for pt_key in ('sort_cost_max', 'sort_cost_min'):
                active = self.is_filter_button_active(screen, pts[pt_key], sorting=True)
                if active is True:
                    self.adb.tap(*pts[pt_key], jitter=0, delay_after=0.3)
                    screen = self.adb.screencap()
        if self.is_filter_popup_open(screen):
            self.adb.tap(*pts['button'], jitter=0, delay_after=0.35)

    def reset_menu_filter(self):
        """Đặt lại bộ lọc menu thợ về mặc định (bật đủ 3 loại tài nguyên, tắt sắp xếp theo giá)
        để hiển thị đầy đủ toàn bộ các thợ đang xây dựng ở đầu danh sách."""
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
        for pt_key in ('res_gold', 'res_elixir', 'res_dark_elixir'):
            active = self.is_filter_button_active(screen, pts[pt_key], sorting=False)
            if active is False:
                self.adb.tap(*pts[pt_key], jitter=0, delay_after=0.25)
                screen = self.adb.screencap()
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
        # Bright elixir and gold are unambiguous; dark elixir requires purple.
        for name in ('elixir', 'gold', 'dark_elixir'):
            if counts[name] >= 12:
                return name
        return None

    def menu_candidates(self, screen, words, start=50, end=795, custom_mode=False):
        candidates = []
        for text, x, y in words:
            if not (1230 <= x <= 1460 and start+10 < y < end-20):
                continue
            name = re.sub(r'\s*[xX×]\s*\d+\s*$', '', text).strip()
            key = self.normalized(name)
            allowed_names = self.custom_regular_names if custom_mode else self.base_regular_names
            if key not in allowed_names or key.startswith('wall'):
                continue

            # New construction has its own placement flow and must never be
            # sent through the ordinary upgrade confirmation.
            if any(re.fullmatch(r'new', t.strip(), re.I) and 1230 <= nx <= 1450
                   and y + 4 <= ny <= y + 40 for t, nx, ny in words):
                continue

            # Duration parse (optional; never discard candidate if duration OCR fails)
            duration_sec = 0
            dur_candidates = [
                token for token, tx, ty in words if tx > 1410 and abs(ty-y) <= 18
            ]
            if dur_candidates:
                combined_dur = ''.join(
                    token for token, tx, ty in sorted(
                        [(t, tx, ty) for t, tx, ty in words if tx > 1410 and abs(ty-y) <= 18],
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
                if px > 1350 and y+10 <= py <= y+36:
                    if self.parse_duration(token, allow_unitless_seconds=False) is not None:
                        continue
                    norm_p = token.replace('O', '0').replace('o', '0').replace('l', '1').replace('I', '1').replace('i', '1')
                    if re.search(r'[a-zA-Z]', norm_p):
                        continue
                    digits = re.sub(r'[^0-9]', '', norm_p)
                    if digits:
                        price_tokens.append((px, py, digits))
            if not price_tokens:
                continue

            price_y = price_tokens[0][1]
            cost_str = ''.join(d for _, _, d in sorted(price_tokens, key=lambda p: p[0]))
            cost = int(cost_str) if cost_str.isdigit() else 0
            if cost <= 0:
                continue

            resource = self.currency(screen, (1560, max(0, price_y-16), 38, 34))
            if resource:
                row = dict(
                    name=name,
                    x=x,
                    y=y,
                    cost=cost,
                    resource=resource,
                    duration_sec=duration_sec,
                    category=self.category_of(name),
                )
                if self.is_candidate_allowed(row, custom_mode=custom_mode):
                    candidates.append(row)
        return sorted(candidates, key=lambda row: row['y'])

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
            self.apply_menu_filter_if_needed()

        # Check if already at top (headers 'suggest' or 'filter' visible near top)
        screen = self.adb.screencap()
        words = self.words(screen, (1170, 50, 430, 755))
        top_headers = [y for text, _, y in words if any(k in text.lower() for k in ('suggest', 'filter', 'gợi ý', 'đã lọc', 'lọc')) and y < 160]
        if not top_headers and not use_filter:
            previous = None
            for _ in range(6):
                self.check()
                self.ui.scroll_menu('up', fast=True)
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                headers = [y for text, _, y in words if any(k in text.lower() for k in ('suggest', 'filter', 'gợi ý', 'đã lọc', 'lọc')) and y < 160]
                signature = self.ui.menu_signature(words)
                if headers or signature == previous:
                    break
                previous = signature

        # In cost_min, cost_max or suggested: the game menu filter already ordered items!
        # Check page 0 candidates first for instantaneous selection
        headers = [y for text, _, y in words if any(k in text.lower() for k in ('suggest', 'filter'))]
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
            if signature == previous or not rows:
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
            return None, set(), 'Không tìm thấy công trình nào khớp bộ lọc tùy chọn.'

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

    def _seek_candidate_row(self, target):
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
        if re.search(r'\bgoblin builder\b|\bgems?\b|need more|buy resources|finish now', all_text):
            return None
        title = self.normalized(' '.join(text for text, _, y in words if y < 250))
        name = self.normalized(candidate['name'])
        ckey = self.canonical_key(candidate['name'])
        allowed_names = self.custom_regular_names if self.custom_mode_enabled() else self.base_regular_names
        if name not in allowed_names or name.startswith('wall'):
            return None

        has_close_btn = bool(self.upgrade_close_button(screen))
        name_matched = (
            (name in title)
            or (ckey in title)
            or any(self.normalized(part) in title for part in candidate['name'].split() if len(part) >= 3)
            or (has_close_btn and any(k in title for k in ('upgrade', 'level', 'cap', 'nangcap', 'tolevel')))
        )
        if not name_matched and not has_close_btn:
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
            self.apply_menu_filter_if_needed()

        screen = self.adb.screencap()
        words = self.words(screen, (1170, 50, 430, 755))
        top_headers = [y for text, _, y in words if any(k in text.lower() for k in ('suggest', 'filter', 'gợi ý', 'đã lọc', 'lọc')) and y < 160]
        if not top_headers and not (custom_mode and use_filter):
            previous = None
            for _ in range(6):
                self.check()
                self.ui.scroll_menu('up', fast=True)
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                headers = [y for text, _, y in words if any(k in text.lower() for k in ('suggest', 'filter', 'gợi ý', 'đã lọc', 'lọc')) and y < 160]
                signature = self.ui.menu_signature(words)
                if headers or signature == previous:
                    break
                previous = signature

        collected = []
        seen_keys = set()
        headers = [y for text, _, y in words if any(k in text.lower() for k in ('suggest', 'filter'))]
        start = min(headers) if headers else 45

        # Scan active ongoing jobs appearing above headers (group by right-side timer so green progress bars hiding names never skip jobs)
        ongoing_jobs = self._extract_ongoing_jobs_from_words(screen, words, start) if start > 55 else []
        state = getattr(self.village, 'village_state', None)
        if state is not None and ongoing_jobs:
            state.set_ongoing_upgrades(ongoing_jobs)
        if ongoing_jobs and any(j.get('duration_sec', 0) > 0 for j in ongoing_jobs):
            self._last_ongoing_scan = (time.monotonic(), list(ongoing_jobs))
            if callable(getattr(self.village, 'save_account_builder_timers', None)):
                self.village.save_account_builder_timers(ongoing_jobs)

        p0_rows = self.menu_candidates(screen, words, start=start, end=795, custom_mode=custom_mode)
        for r in p0_rows:
            dedup = (self.canonical_key(r['name']), r['cost'], r['resource'])
            if dedup not in seen_keys:
                seen_keys.add(dedup)
                collected.append(dict(r, page=0))

        need_more = len(collected) < needed_builders or priority not in ('suggested', 'cost_min', 'cost_max')
        if need_more and priority != 'specific':
            prev_sig = self.ui.menu_signature(words)
            for page in range(1, 4):
                self.check()
                self.ui.scroll_menu('down', fast=True)
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                rows = self.menu_candidates(screen, words, start=45, end=795, custom_mode=custom_mode)
                for r in rows:
                    dedup = (self.canonical_key(r['name']), r['cost'], r['resource'])
                    if dedup not in seen_keys:
                        seen_keys.add(dedup)
                        collected.append(dict(r, page=page))
                sig = self.ui.menu_signature(words)
                if sig == prev_sig or not rows or len(collected) >= needed_builders * 2:
                    break
                prev_sig = sig
        elif priority == 'specific' and not collected:
            prev_sig = self.ui.menu_signature(words)
            for page in range(1, 5):
                self.check()
                self.ui.scroll_menu('down', fast=True)
                screen = self.adb.screencap()
                words = self.words(screen, (1170, 50, 430, 755))
                rows = self.menu_candidates(screen, words, start=45, end=795, custom_mode=custom_mode)
                for r in rows:
                    dedup = (self.canonical_key(r['name']), r['cost'], r['resource'])
                    if dedup not in seen_keys:
                        seen_keys.add(dedup)
                        collected.append(dict(r, page=page))
                sig = self.ui.menu_signature(words)
                if sig == prev_sig or not rows or collected:
                    break
                prev_sig = sig

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
            return [], set(), 'Không tìm thấy công trình nào khớp bộ lọc tùy chọn.'

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

    def execute_builder_plan(self, plan, log, maintain_reserve=False):
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

        executed_count = 0
        current_page = 0
        for item in ready_items:
            cand = item['candidate']
            b_idx = item['builder_idx']
            cand_page = cand.get('page', 0)

            # Re-verify candidate row position on live screen to avoid misclicks
            live_scr = self.adb.screencap()
            live_words = self.words(live_scr, (1170, 50, 430, 755))
            live_rows = self.menu_candidates(live_scr, live_words, start=45, end=795, custom_mode=True)
            matched_row = next(
                (r for r in live_rows
                 if self.canonical_key(r['name']) == self.canonical_key(cand['name'])
                 and r['cost'] == cand['cost']
                 and r['resource'] == cand['resource']),
                None
            )

            if matched_row is not None:
                sought = matched_row
            elif cand_page != current_page:
                sought = self._seek_candidate_row(cand)
                current_page = cand_page
            else:
                sought = cand

            tap_x = 1380  # Center clickable zone of row (1190-1580)
            tap_y = (sought.get('y', cand.get('y', 200)) + 15) if sought else cand.get('y')
            if tap_y is None:
                if log:
                    log(f"[Thợ] ⚠️ Không tìm thấy vị trí hàng của {cand['name']}; dừng để lập lại kế hoạch.")
                break

            self.adb.tap(tap_x, tap_y, jitter=0, delay_after=0.4)
            selected = self.adb.screencap()
            conf = self.confirmation(selected, cand)
            if conf is None and not self.upgrade_close_button(selected):
                if self.close_menu(selected):
                    point = self.upgrade_button(self.adb.screencap(), cand)
                    if point is not None:
                        self.adb.tap(*point, jitter=0, delay_after=0.45)
                        conf = self.confirmation(self.adb.screencap(), cand)

            if conf is None:
                if log:
                    log(f"[Thợ] ⚠️ Không xác nhận được bảng nâng cấp cho {cand['name']}; hủy để đảm bảo an toàn.")
                scr = self.adb.screencap()
                close_btn = self.upgrade_close_button(scr)
                if close_btn:
                    self.adb.tap(*close_btn[:2], jitter=0, delay_after=0.3)
                break

            if log:
                log(f"🔨 [Thợ {b_idx}] Đang khởi công: {cand['name']} ({cand['cost']:,} {cand['resource']})...")
            self.adb.tap(*conf, jitter=0, delay_after=0.5)
            executed_count += 1
            self.assignments.append(dict(cand, builder_idx=b_idx))
            if isinstance(cand.get('duration_sec'), int) and cand['duration_sec'] > 0:
                if callable(getattr(self.village, 'record_started_builder_upgrade', None)):
                    self.village.record_started_builder_upgrade(cand['name'], cand['duration_sec'])

            time.sleep(0.4)
            scr = self.adb.screencap()
            if not self.village.is_in_main_village(scr):
                close_btn = self.upgrade_close_button(scr)
                if close_btn:
                    self.adb.tap(*close_btn[:2], jitter=0, delay_after=0.3)
                else:
                    self.ui.close_dialog()

            remaining_ready = [p for p in ready_items if p['builder_idx'] > b_idx]
            if remaining_ready:
                if not self.ui.ensure_menu_open():
                    if log:
                        log(f"[Thợ] ⚠️ Menu thợ bị đóng giữa chừng; dừng để lập lại kế hoạch cho các thợ còn lại.")
                    break
                current_page = 0

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
                reason=(
                    f"Đã khởi công {executed_count} công trình. Còn thợ chờ farm thêm "
                    f"{first_unready['deficit']:,} {first_unready['resource']} cho {first_unready['candidate']['name']}."
                )
            )

        if executed_count > 0:
            return dict(status='progress', count=executed_count, reason=f"Đã khởi công thành công {executed_count} công trình theo kế hoạch.")

        return dict(status='done', reason="Đã hoàn tất kiểm tra thợ.")

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

    def step(self, log, *, maintain_reserve=False, custom_mode=None, preopened=None):
        self.deadline = time.monotonic()+60
        try:
            return self._timed_step(log, maintain_reserve=maintain_reserve,
                                    custom_mode=custom_mode, preopened=preopened)
        finally:
            self.deadline = 0

    def _timed_step(self, log, *, maintain_reserve, custom_mode, preopened):
        if self.pending is not None:
            self.last_status = 'Giao dịch công trình chưa rõ; dừng để tránh nâng lặp.'
            return dict(status='uncertain', reason=self.last_status)
        self.opened_menu = False
        try:
            result = self._step(log, maintain_reserve=maintain_reserve, custom_mode=custom_mode, preopened=preopened)
        except BotStoppedError:
            raise
        except Exception as exc:
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
        if custom_mode:
            plan, needed, reason = self.plan_multi_builders(wallet, needed_builders=needed_builders, log=log, custom_mode=True)
            if not plan:
                self.close_menu(self.adb.screencap())
                return dict(status='needs_resources' if needed else 'blocked', reason=reason,
                            resources=list(needed), costs=self.unaffordable, target=self.target_candidate)
            res = self.execute_builder_plan(plan, log, maintain_reserve=maintain_reserve)
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
        self.pending = dict(candidate, before=before[resource], builders_before=first)
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
