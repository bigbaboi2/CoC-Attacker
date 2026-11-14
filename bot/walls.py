# -*- coding: utf-8 -*-
"""Verified wall upgrades on the observed 1600x900 layout."""
import re
import cv2
import numpy as np
import time
import hashlib
import json
import unicodedata
from core.adb_client import BotStoppedError
from core.stats_store import StatsStore
from pathlib import Path
from collections import deque, OrderedDict
from functools import wraps
from datetime import datetime, timezone, timedelta

class SelectionTimeout(RuntimeError):
    pass


def selection_stage(name):
    """Inclusive timings: nested stages overlap and must not be summed."""
    def decorate(method):
        @wraps(method)
        def timed(self, *args, **kwargs):
            self._selection_checkpoint()
            if self._selection_deadline is None:
                return method(self, *args, **kwargs)
            started = time.monotonic()
            try:
                return method(self, *args, **kwargs)
            finally:
                stages = self._selection_metrics['stages']
                stages[name] = round(stages.get(name, 0) + time.monotonic()-started, 3)
        return timed
    return decorate


def frame_analysis(method):
    """Reuse analysis only for identical pixels, arguments and method."""
    @wraps(method)
    def cached(self, screen, *args, **kwargs):
        self._selection_checkpoint()
        if not isinstance(screen, np.ndarray):
            return method(self, screen, *args, **kwargs)
        # Hash only pixels actually consumed by this analysis. Village
        # animations outside the ROI must not invalidate a menu/title result.
        area = screen
        name = method.__name__
        if name in ('texts', 'ocr_positions'):
            x, y, w, h = args[0] if args else kwargs['roi']
            area = screen[y:y+h, x:x+w]
        elif screen.shape[:2] == (900, 1600):
            if name == 'level':
                area = screen[450:880, 200:1380]
            elif name == 'wall_controls_present':
                area = screen[620:790, 200:1150]
            elif name in ('wall_label_ys', 'menu_words'):
                area = screen[50:805, 1170:1600]
            elif name == 'menu_wall_level':
                top = max(0, int(args[0])+10)
                area = screen[top:min(900, top+50), 1175:1220]
            elif name == 'is_menu_open':
                area = screen[:180, 1000:1600]
            elif name == 'cached_menu_row':
                hint = args[0] if args else kwargs['cached']
                relative_y = hint.get('relative_y')
                if isinstance(relative_y, (int, float)) and .05 <= relative_y <= .9:
                    top = max(50, min(700, int(relative_y*900)-45))
                    area = screen[top:min(900, top+170), 1175:1600]
        key = (method.__name__, screen.shape, screen.dtype.str,
               hashlib.blake2b(np.ascontiguousarray(area), digest_size=16).digest(),
               repr(args), repr(sorted(kwargs.items())))
        if key in self._analysis_cache:
            self._analysis_cache.move_to_end(key)
            value = self._analysis_cache[key]
            return dict(value) if isinstance(value, dict) else value
        value = method(self, screen, *args, **kwargs)
        self._analysis_cache[key] = dict(value) if isinstance(value, dict) else value
        while len(self._analysis_cache) > 48:
            self._analysis_cache.popitem(last=False)
        return value
    return cached


class WallUpgrader:
    # Coordinates belong to the 1600x900 ADB game frame, not MuMu's desktop
    # window (whose title bar / display scaling can change independently).
    FIXED_UPGRADE_POINTS = {'gold': (886, 747), 'elixir': (1060, 747)}
    FIXED_RESOURCE_ICONS = {'gold': (949, 647), 'elixir': (1123, 647)}
    FIXED_UPGRADE_LAYOUTS = (
        ('with_rings', 0),
        ('no_rings', 87),
    )
    FIXED_LAYOUT_POINTS = {
        'layout_icon': (1115, 665),
        'edit_layout': (489, 812),
        'remove_all': (1419, 146),
        'confirm_remove_all': (940, 555),
        'cancel_edit': (1419, 645),
        'confirm_discard': (940, 545),
        'close_editor': (1520, 72),
    }

    @staticmethod
    def max_wall_level_for_th(th_level):
        if not isinstance(th_level, int) or th_level < 2:
            return None
        return th_level if th_level <= 8 else th_level + 1

    def parse_layout_inventory_cards(self, screen, builder_base=False):
        """Parse the first inventory cards in Village Edit Mode after Remove All."""
        if not isinstance(screen, np.ndarray) or not screen.size:
            return None
        h, w = screen.shape[:2]
        roi = (0, 715, min(1200, w), min(185, h - 715)) if (h, w) == (900, 1600) else (0, 0, w, h)
        words = self.ocr_positions(screen, roi, scale=1)
        if not words:
            return None
        norm_map = str.maketrans({'i': '1', 'I': '1', 'l': '1', '|': '1', 's': '5', 'S': '5', 'O': '0', 'o': '0'})
        levels = []
        counts = []
        for token, x, y in words:
            cleaned = token.strip()
            lvl_match = re.search(r'leve[li1!|][\s:.\-]*([0-9iIlSsOo]+)', cleaned, re.I)
            if lvl_match:
                digits = lvl_match.group(1).translate(norm_map)
                if digits.isascii() and digits.isdigit() and 1 <= int(digits) <= 25:
                    levels.append((int(digits), x, y))
                    continue
            cnt_match = re.match(r'^[x\u00d7X]\s*([0-9iIlSsOo]+)$', cleaned)
            if cnt_match:
                digits = cnt_match.group(1).translate(norm_map)
                if digits.isascii() and digits.isdigit() and int(digits) >= 1:
                    counts.append((int(digits), x, y))
        if not levels:
            return None
        levels.sort(key=lambda item: item[1])
        dedup_levels = []
        for lvl, lx, ly in levels:
            if not dedup_levels or abs(lx - dedup_levels[-1][1]) > 45:
                dedup_levels.append((lvl, lx, ly))
        cards = []
        for lvl, lx, ly in dedup_levels:
            matched_cnt = None
            for cnt, cx, cy in counts:
                if abs(cx - lx) <= 45 and cy < ly - 20:
                    matched_cnt = cnt
                    break
            cards.append(dict(level=lvl, count=matched_cnt, x=lx, y=ly))
        wall_groups = []
        th_level = None
        for card in cards:
            cnt = card['count']
            if cnt is not None and cnt >= (1 if builder_base else 15):
                wall_groups.append(dict(level=card['level'], count=cnt, x=card['x']))
            elif wall_groups:
                if cnt is None and th_level is None:
                    th_level = card['level']
                break
        if not wall_groups:
            return None
        distinct_levels = sorted({grp['level'] for grp in wall_groups})
        min_level = distinct_levels[0]
        max_level = distinct_levels[-1]
        min_available = sum(grp['count'] for grp in wall_groups if grp['level'] == min_level)
        if builder_base or len(distinct_levels) >= 2:
            maxed = False
        else:
            max_allowed = self.max_wall_level_for_th(th_level)
            maxed = max_allowed is not None and min_level >= max_allowed
        return dict(
            maxed=maxed,
            min_level=min_level,
            max_level=max_level,
            available=min_available,
            wall_groups=wall_groups,
            th_level=th_level,
        )

    def _dismiss_layout_obstacle(self, log):
        """Dismiss only a recognized obstacle notice, then verify it disappeared."""
        for _ in range(2):
            frame = self.adb.screencap()
            if frame is None:
                return False
            words = ' '.join(self.texts(frame, (330, 180, 940, 480), scale=1)).lower()
            normalized = ''.join(c for c in unicodedata.normalize('NFD', words)
                                 if unicodedata.category(c) != 'Mn')
            compact = re.sub(r'[^a-z]', '', normalized)
            if 'obstacle' not in compact and 'chuongngai' not in compact:
                return True
            button = self.vision.match_template(frame, 'okay', .75, roi=(500, 440, 600, 330))
            if not button:
                button = self.vision.find_okay_button(frame)
            if not button:
                log('[Wall] Thấy cảnh báo chướng ngại nhưng chưa nhận diện được Okay.')
                return False
            self.adb.tap(button[0], button[1], jitter=0, delay_after=.5)
            log('[Wall] Đã bấm Okay để bỏ qua cảnh báo chướng ngại.')
        frame = self.adb.screencap()
        words = ' '.join(self.texts(frame, (330, 180, 940, 480), scale=1)).lower() if frame is not None else ''
        normalized = ''.join(c for c in unicodedata.normalize('NFD', words)
                             if unicodedata.category(c) != 'Mn')
        compact = re.sub(r'[^a-z]', '', normalized)
        return frame is not None and 'obstacle' not in compact and 'chuongngai' not in compact

    def verify_walls_via_layout_editor(self, log):
        """Fast fixed-coordinate wall verification via Layout Editor -> Edit layout -> Remove All."""
        pts = self.FIXED_LAYOUT_POINTS
        if not self.ensure_menu_open():
            return None
        entered_editor = False
        edit_opened = False
        cleanup_safe = True
        verified = None
        try:
            self.adb.tap(*pts['layout_icon'], jitter=0, delay_after=0.8)
            entered_editor = True
            if not self._dismiss_layout_obstacle(log):
                cleanup_safe = False
                return None
            self.adb.tap(*pts['edit_layout'], jitter=0, delay_after=1.0)
            edit_opened = True
            if not self._dismiss_layout_obstacle(log):
                cleanup_safe = False
                return None
            self.adb.tap(*pts['remove_all'], jitter=0, delay_after=0.6)
            if not self._dismiss_layout_obstacle(log):
                cleanup_safe = False
                return None
            self.adb.tap(*pts.get('confirm_remove_all', (940, 555)), jitter=0, delay_after=0.8)
            if not self._dismiss_layout_obstacle(log):
                cleanup_safe = False
                return None
            screen = self.adb.screencap()
            result = self.parse_layout_inventory_cards(screen)
            verified = result
        finally:
            if entered_editor and cleanup_safe:
                try:
                    if edit_opened:
                        self.adb.tap(*pts['cancel_edit'], jitter=0, delay_after=0.6)
                        self.adb.tap(*pts['confirm_discard'], jitter=0, delay_after=0.7)
                    self.adb.tap(*pts['close_editor'], jitter=0, delay_after=0.7)
                    is_main = getattr(self.manager, 'is_in_main_village', None)
                    if callable(is_main) and not is_main(self.adb.screencap()):
                        log('[Wall] Chưa xác nhận về làng chính sau khi hủy chỉnh sửa.')
                        verified = None
                except BotStoppedError:
                    raise
                except Exception:
                    verified = None
            elif entered_editor:
                log('[Wall] Popup chưa được đóng chắc chắn; giữ nguyên giao diện để thử khôi phục ở lượt sau.')
        if verified is None:
            return None
        acc_key = getattr(self.manager, 'current_account_key', None)
        if not acc_key:
            log('[Wall] Chưa xác minh tài khoản; không ghi trạng thái tường.')
            return None
        v_state = getattr(self.manager, 'village_state', None)
        if v_state is not None:
            inv = {grp['level']: grp['count'] for grp in verified['wall_groups']}
            if hasattr(v_state, 'replace_wall_inventory'):
                v_state.replace_wall_inventory('main', inv)
            else:
                for grp in verified['wall_groups']:
                    v_state.record_wall_group('main', grp['level'], grp['count'])
        maxed = bool(verified['maxed'])
        self.manager.walls_confirmed_maxed = maxed
        if hasattr(self.manager, 'verified_maxed_accounts'):
            if maxed:
                self.manager.verified_maxed_accounts[acc_key] = dict(
                    th_level=verified.get('th_level'), min_level=verified['min_level'], verified_at=time.time())
            else:
                self.manager.verified_maxed_accounts.pop(acc_key, None)
        if v_state is not None:
            v_state.record_walls_maxed(maxed, verified.get('th_level'))
        if maxed:
            th_note = f" (TH{verified['th_level']})" if verified.get('th_level') else ''
            self.manager.wall_status = f"Đã xác minh qua Layout Editor: toàn bộ tường đạt Level {verified['min_level']}{th_note} (Max)."
        else:
            levels_desc = ', '.join(f"Lv{g['level']}×{g['count']}" for g in verified['wall_groups'])
            self.manager.wall_status = f"Xác minh Layout Editor ({levels_desc}): tường CHƯA max, còn {verified['available']} đoạn Level {verified['min_level']}."
        log('[Wall] ' + self.manager.wall_status)
        if hasattr(self.manager, 'save_wall_target'):
            self.manager.save_wall_target(None)
        if hasattr(self.manager, 'settings') and self.manager.settings:
            self.manager.settings.set('farm_finish_enabled', maxed)
            if maxed and self.manager.settings.get('farm_finish_resources') not in ('gold_elixir', 'gold', 'elixir'):
                self.manager.settings.set('farm_finish_resources', 'gold_elixir')
        return verified
    def __init__(self, manager):
        self.manager = manager
        self.adb = manager.adb
        self.vision = manager.vision
        self.settings = manager.settings
        self._analysis_cache = OrderedDict()
        self._selection_deadline = None
        self._selection_metrics = None
        self._wall_template_cache = {}

    def _selection_checkpoint(self):
        deadline = self._selection_deadline
        if deadline is None:
            return
        now = time.monotonic()
        if now >= deadline:
            raise SelectionTimeout('Hết ngân sách 30 giây tìm/chọn tường')
        if now >= self._selection_started + 15 and not self._selection_metrics['recovery']:
            self._selection_metrics['recovery'] = 1
            self._selection_log('[Wall] Hết 15 giây đường nhanh; phục hồi trong ngân sách chung 30 giây.')

    def _selection_ocr(self):
        self._selection_checkpoint()
        if self._selection_metrics is not None and self._selection_deadline is not None:
            self._selection_metrics['ocr'] += 1

    @frame_analysis
    def texts(self, screen, roi, scale=2):
        if screen is None: return []
        x, y, w, h = roi
        ocr = self.vision._get_ocr()
        if ocr is None: return []
        try:
            crop = screen[y:y+h, x:x+w]
            if scale != 1:
                crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
            self._selection_ocr()
            result = ocr(crop, use_det=True)
            texts = getattr(result, 'txts', None)
            return list(texts) if texts is not None else []
        except SelectionTimeout: raise
        except Exception: return []

    @frame_analysis
    def level(self, screen, fast=False):
        header=self.header_roi(screen)
        title_x = 450
        if fast and isinstance(screen, np.ndarray):
            tiles = [tile for tile in self.toolbar_tiles(screen) if tile[2] >= 110 and tile[3] >= 110]
            if tiles:
                centers = sorted(x+w//2 for x, y, w, h in tiles)
                center = (centers[(len(centers)-1)//2] + centers[len(centers)//2])/2
                title_x = max(200, min(650, int(center)-350))
        rois = ((title_x,header[1],700,header[3]),) if fast else (
            (450,header[1],700,header[3]),header, (200,500,1150,135))
        for roi in rois:
            for filtered in (True,False):
                text = ' '.join(self.title_texts(screen, roi) if filtered else self.texts(screen,roi,scale=2))
                if not re.search(r'\b(?:wa[li1][li1]|t\u01b0\u1eddng)', text, re.I):
                    continue
                match = re.search(r'\bwa[li1][li1]\s*\(?\s*leve[li1!|][\s:.\-–—(]*([0-9iIl|oOsS]+)(?![a-zA-Z0-9])', text, re.I)
                if not match:
                    match = re.search(r'leve[li1!|][\s:.\-–—(]*([0-9iIl|oOsS]+)(?![a-zA-Z0-9])', text, re.I)
                if not match:
                    match = re.search(r'c[aấâ]p\s*\(?([0-9iIl|oOsS]+)(?![a-zA-Z0-9])', text, re.I)
                if match:
                    number = match.group(1).translate(str.maketrans({'i':'1','I':'1','l':'1','|':'1','o':'0','O':'0','s':'5','S':'5'}))
                    if 1 <= int(number) <= 25:
                        return int(number)
        return None

    @frame_analysis
    def wall_controls_present(self,screen):
        if screen is None:return False
        return bool(self.wall_template(screen,'upmore',.76,roi=(200,620,950,170)) or
                    self.wall_template(screen,'addwall',.86,roi=(200,620,950,170)))

    def wall_template(self, screen, name, threshold, roi):
        """Menu-open and menu-closed toolbars can use different UI scales."""
        match = self.vision.match_template(screen, name, threshold, roi=roi)
        if match or not isinstance(screen, np.ndarray):
            return match
        template = self.vision.load_template(name)
        if not isinstance(template, np.ndarray):
            return None
        x, y, w, h = roi
        area = screen[y:y+h, x:x+w]
        for scale in (.9, .85, .95, 1.05, 1.1):
            self._selection_checkpoint()
            key = (name, scale)
            if key not in self._wall_template_cache:
                self._wall_template_cache[key] = cv2.resize(template, None, fx=scale, fy=scale)
            needle = self._wall_template_cache[key]
            nh, nw = needle.shape[:2]
            if area.shape[0] < nh or area.shape[1] < nw:
                continue
            _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(area, needle, cv2.TM_CCOEFF_NORMED))
            if score >= threshold:
                return (x+point[0]+nw//2, y+point[1]+nh//2, nw, nh, score)
        return None

    def selection_level(self,screen):
        observed=self.level(screen)
        self._selection_level_fallback=False
        evidence=getattr(self,'_menu_selection_evidence',None)
        if observed is None and isinstance(evidence,dict) and self.wall_controls_present(screen):
            self._selection_level_fallback=True
            return evidence['level']
        return observed

    def header_roi(self, screen):
        tiles = self.toolbar_tiles(screen) if screen is not None else []
        top = min(tile[1] for tile in tiles) if tiles else 626
        return (200, max(450, top-68), 1150, 65)

    def builders(self, screen, builder_base=False):
        self.paid_builder_visible = False
        if builder_base:
            parts = self.texts(screen, (1410, 675, 80, 43))
            text = ''.join(parts).replace(' ', '')
            match = re.search(r'([0-4])\s*[/|]\s*([1-4])', text)
            if match and int(match.group(1)) <= int(match.group(2)):
                raw_free = int(match.group(1))
                raw_total = int(match.group(2))
                self.total_builders_read = raw_total
                return min(raw_total, raw_free)
            if screen is not None:
                value = self.vision.read_white_number(screen[677:708, 1425:1446])
                tot_val = self.vision.read_white_number(screen[677:708, 1455:1476])
                if tot_val is not None and 1 <= tot_val <= 4:
                    self.total_builders_read = tot_val
                if value is not None and tot_val is not None and 0 <= value <= tot_val <= 4 and tot_val >= 1:
                    return value
            if screen is not None:
                ocr = self.vision._get_ocr()
                if ocr:
                    try:
                        res = ocr(screen[660:720, 1400:1560])
                        if res and hasattr(res, 'txts') and res.txts:
                            for t in res.txts:
                                m = re.search(r'([0-4])\s*[/|]\s*([1-4])', t)
                                if m and int(m.group(1)) <= int(m.group(2)):
                                    self.total_builders_read = int(m.group(2))
                                    return int(m.group(1))
                    except Exception:
                        pass
            return None

        if hasattr(screen, 'shape') and screen.shape[:2] == (900, 1600):
            # Read only the face, not the grass behind the portrait.
            head = cv2.cvtColor(screen[650:670, 1447:1470], cv2.COLOR_BGR2HSV)
            self.paid_builder_visible = (cv2.inRange(head, (30, 90, 110), (85, 255, 255)) > 0).mean() > .35
        def regular(count):
            return max(0, count - int(self.paid_builder_visible))
        parts = self.texts(screen, (1410, 675, 80, 43))
        text = ''.join(parts).replace(' ', '')
        match = re.search(r'([0-7])\s*[/7|]\s*([1-8sS])', text)
        if match:
            total_raw = match.group(2).lower()
            raw_total = 5 if total_raw == 's' else int(total_raw)
            raw_free = int(match.group(1))
            if raw_total > 6 or self.paid_builder_visible:
                self.paid_builder_visible = True
                reg_total = min(6, max(1, raw_total - 1))
                reg_free = max(0, raw_free - 1)
                self.total_builders_read = reg_total
                return reg_free
            self.total_builders_read = raw_total
            return regular(min(raw_total, raw_free))
        if screen is not None:
            value = self.vision.read_white_number(screen[677:708, 1425:1446])
            tot_val = self.vision.read_white_number(screen[677:708, 1455:1476])
            if tot_val is not None:
                self.total_builders_read = min(6, max(1, tot_val - 1)) if self.paid_builder_visible else min(6, tot_val)
            if value is not None and 0 <= value <= 6: return regular(value)
        if screen is not None:
            ocr = self.vision._get_ocr()
            if ocr:
                try:
                    res = ocr(screen[660:720, 1400:1560])
                    if res and hasattr(res, 'txts') and res.txts:
                        for t in res.txts:
                            m = re.search(r'([0-6])\s*/\s*([1-6sS])', t)
                            if m: return regular(int(m.group(1)))
                except Exception: pass
        return None

    @staticmethod
    def duration_seconds(text):
        text = text.strip().lower().replace(' ', '')
        if not re.fullmatch(r'(?:\d+[dhms])+', text):
            return None
        return sum(int(value)*dict(d=86400, h=3600, m=60, s=1)[unit]
                   for value, unit in re.findall(r'(\d+)([dhms])', text))

    def wait_for_builder(self, free, log):
        seconds = None
        if free is not None and self.ensure_menu_open():
            previous = None
            for _ in range(18):
                screen = self.adb.screencap()
                words = self.ocr_positions(screen, (1170, 50, 430, 755))
                signature = self.menu_signature(words)
                if signature and signature == previous:
                    break
                previous = signature
                self.scroll_menu('up', fast=True)
            # Only timers ABOVE Suggested upgrades are active jobs. Suggested
            # construction times are never a reason to postpone wall upgrades.
            boundaries = [y for text, _, y in words if 'suggest' in text.lower()]
            if boundaries:
                end = min(boundaries)
                rows = {}
                for text, x, y in words:
                    if x < 1400 or not 55 < y < end:
                        continue
                    baseline = next((old for old in rows if abs(old-y) <= 12), y)
                    rows.setdefault(baseline, []).append((x, text))
                durations = [self.duration_seconds(''.join(text for _, text in sorted(row))) for row in rows.values()]
                durations = [value for value in durations if value is not None and value > 0]
                if durations:
                    seconds = min(durations)
            self.close_dialog()
        if seconds is None:
            seconds = 300
            reason = ('Chưa đọc được số thợ thường.' if free is None else 'Không đủ thợ thường rảnh; chưa đọc được giờ hoàn thành.')
            reason += ' Kiểm tra lại sau 5 phút.'
        else:
            hours, minutes = divmod((seconds+59)//60, 60)
            reason = f'Thiếu thợ thường; thợ gần nhất còn {hours} giờ {minutes} phút.'
        retry = time.time()+seconds+10
        stamp = datetime.fromtimestamp(retry, timezone(timedelta(hours=7))).strftime('%H:%M %d/%m')
        reason += f' Lượt kiểm tra nâng tường tiếp theo: {stamp} (giờ Việt Nam).'
        self.manager.set_wall_wait(retry, reason)
        log('[Wall] '+reason)

    def amount(self, screen, roi):
        parts = [t.replace(' ', '').replace(',', '').replace('.','').translate(str.maketrans({'I':'1','i':'1','l':'1','O':'0','o':'0'})) for t in self.texts(screen, roi)]
        text = ''.join(t for t in parts if t.isascii() and t.isdigit())
        return int(text) if text.isascii() and text.isdigit() else None

    def close_dialog(self):
        screen = self.adb.screencap()
        if screen is not None:
            if self.vision.wall_confirmation(screen):
                from pathlib import Path
                cancel = self.vision.match_template(screen, str(Path(__file__).resolve().parents[1]/'local_assets'/'wall_cancel.png'), .90, roi=(400, 450, 450, 230))
                if cancel:
                    self.adb.tap(cancel[0], cancel[1], jitter=0, delay_after=.5)
                    return
            close = self.vision.find_close_button(screen)
            if close:
                self.adb.tap(close[0], close[1], jitter=0, delay_after=.6)
                return
            okay = self.vision.find_okay_button(screen) if hasattr(self.vision, 'find_okay_button') else None
            if okay:
                self.adb.tap(okay[0], okay[1], jitter=0, delay_after=.5)
                return
        # No low-confidence X search on the village/HUD while a popup is still
        # opening: that used to send an unrelated tap toward the top of screen.
        return False

    @frame_analysis
    def ocr_positions(self, screen, roi, scale=2):
        if screen is None: return []
        x, y, w, h = roi
        ocr = self.vision._get_ocr()
        if ocr is None: return []
        try:
            crop = screen[y:y+h, x:x+w]
            if scale != 1:
                crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
            self._selection_ocr()
            result = ocr(crop, use_det=True)
            texts = getattr(result, 'txts', None)
            boxes = getattr(result, 'boxes', None)
            if texts is None or boxes is None or len(texts) != len(boxes): return []
            return [(text, x + int(box[:, 0].mean() / scale), y + int(box[:, 1].mean() / scale)) for text, box in zip(texts, boxes)]
        except SelectionTimeout: raise
        except Exception: return []

    @frame_analysis
    def menu_words(self, screen, precise=False):
        if screen is None:
            return []
        if precise:
            return self.ocr_positions(screen, (1170,50,430,755))
        ys = self.wall_label_ys(screen)
        strip = cv2.cvtColor(screen[70:790,1240:1415], cv2.COLOR_BGR2HSV)
        white = cv2.inRange(strip,(0,0,165),(180,90,255))
        fingerprint = hashlib.blake2b(cv2.resize(white,(44,180),interpolation=cv2.INTER_NEAREST).tobytes(),digest_size=8).hexdigest()
        words = [('__page:'+fingerprint,0,0)]
        for y in ys:
            top = max(50,y-23)
            words.extend(self.ocr_positions(screen,(1235,top,365,min(85,805-top))))
        return words

    @frame_analysis
    def wall_label_ys(self, screen):
        # Match the short Wall label first. Most menu pages contain no walls;
        # they do not need the detection and recognition of 30+ unrelated lines.
        area = cv2.cvtColor(screen[60:805,1230:1420], cv2.COLOR_BGR2GRAY)
        ys = []
        for name in ('wall', 'wall_1', 'wall_2'):
            template = self.vision.load_template(name)
            if template is None:
                continue
            gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            for scale in (.8,.9,1.,1.1,1.2):
                needle = cv2.resize(gray, None, fx=scale, fy=scale)
                h,w = needle.shape
                scores = cv2.matchTemplate(area, needle, cv2.TM_CCOEFF_NORMED)
                for _ in range(8):
                    _,score,_,point = cv2.minMaxLoc(scores)
                    if score < .76:
                        break
                    y = point[1]+h//2+60
                    if all(abs(y-old)>22 for old in ys):
                        ys.append(y)
                    scores[max(0,point[1]-22):point[1]+23,:] = -1
        return sorted(ys)

    def visible_lower_wall(self, screen, cached_level):
        if not isinstance(screen,np.ndarray) or screen.shape[:2] != (900,1600):
            return False
        # Wall-label templates can also match "Warden". Require the parsed
        # Wall x<count> row before interpreting a badge as a lower wall group.
        return any(row.get('upgrade_level') is not None and row['upgrade_level']-1 < cached_level
                   for row in self.menu_rows(screen, self.menu_words(screen)))

    @selection_stage('scan')
    def scan_wall_rows(self, precise=False, both_directions=False, first_match=False,
                       target_available=None):
        seen = []
        for direction in ('down','up'):
            previous = None
            found_in_direction = False
            # Read the page reached by the sixth swipe as well. Previously the
            # loop swiped six times but never inspected that final page.
            for step in range(7):
                self._selection_checkpoint()
                screen = self.adb.screencap()
                words = self.menu_words(screen, precise=precise)
                rows = self.menu_rows(screen, words, read_level=not first_match)
                signature = self.menu_signature(words)
                if rows:
                    current_offset = getattr(self.manager, '_wall_menu_offset', 0)
                    for r in rows:
                        if 'scroll_down' not in r:
                            r['scroll_down'] = current_offset
                    seen.extend(rows)
                    found_in_direction = True
                    if first_match:
                        return seen
                    if (type(target_available) is int
                            and any(row['available'] == target_available for row in rows)):
                        return seen
                elif found_in_direction and target_available is None:
                    break
                if not signature or signature == previous:
                    break
                previous = signature
                if step < 6:
                    self.scroll_menu(direction)
            if seen and not both_directions and target_available is None:
                break
        return seen

    def menu_rows(self, screen, words=None, read_level=True):
        words = words if words is not None else self.ocr_positions(screen, (1170, 50, 430, 755))
        rows = []
        for text, x, y in words:
            if not re.search(r'\b(?:wa[li1][li1]|t[uư][oơ\u1edd]ng)', text.strip(), re.I): continue
            pieces = []
            for number, nx, ny in words:
                value = re.sub(r'[ ,.]', '', number).translate(str.maketrans({'I':'1','i':'1','l':'1','O':'0','o':'0'}))
                if nx > 1300 and y - 15 <= ny <= y + 60 and value.isascii() and value.isdigit():
                    pieces.append((value, nx, ny))
            if not pieces: continue
            baseline = max(item[2] for item in pieces)
            cost_text = ''.join(item[0] for item in sorted(pieces, key=lambda item: item[1]) if abs(item[2] - baseline) < 14)
            if not cost_text or int(cost_text) <= 0: continue
            title = ' '.join(t for t, _, ty in sorted(words, key=lambda item: item[1]) if abs(ty-y) < 14)
            count_match = re.search(r'(?:wa[li1][li1]|t[uư][oơ\u1edd]ng)\s*[x×]\s*([0-9iIl]+)', title, re.I)
            if count_match:
                available = int(count_match.group(1).translate(str.maketrans({'i':'1', 'I':'1', 'l':'1'})))
            elif (re.fullmatch(r'(?:wa[li1][li1]|t[uư][oơ\u1edd]ng)', text.strip(), re.I)
                  and not re.search(r'(?:wa[li1][li1]|t[uư][oơ\u1edd]ng)\s*[x×]', title, re.I)):
                # Builder's single remaining wall is shown as "Wall", without x1.
                available = 1
            else:
                continue
            if available < 1:
                continue
            row = dict(cost=int(cost_text), x=x, y=y, available=available)
            if read_level and isinstance(screen,np.ndarray) and screen.shape[:2] == (900,1600):
                upgrade_level = self.menu_wall_level(screen, y)
                if upgrade_level is not None:
                    row['upgrade_level'] = upgrade_level
            rows.append(row)
        return rows

    @frame_analysis
    def menu_wall_level(self, screen, title_y):
        """Read the next-level badge, with a tiny direct-recognition crop."""
        top = max(0,int(title_y)+10)
        crop = screen[top:min(900,top+50),1175:1220]
        mask = cv2.inRange(cv2.cvtColor(crop,cv2.COLOR_BGR2HSV),(0,0,160),(180,90,255))
        contours,_ = cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        clean = mask.copy(); clean[:] = 0
        for contour in contours:
            x,y,w,h = cv2.boundingRect(contour)
            if 10 <= x <= 25 and 10 <= y <= 30 and 4 <= h <= 12 and 1 <= w <= 14:
                clean[y:y+h,x:x+w] = mask[y:y+h,x:x+w]
        points = cv2.findNonZero(clean)
        if points is None:
            return None
        x,y,w,h = cv2.boundingRect(points)
        canvas = cv2.copyMakeBorder(255-clean[y:y+h,x:x+w],4,4,4,4,cv2.BORDER_CONSTANT,value=255)
        canvas = cv2.resize(canvas,None,fx=6,fy=6)
        ocr = self.vision._get_ocr()
        if ocr is None:
            return None
        try:
            self._selection_ocr()
            result = ocr(cv2.cvtColor(canvas,cv2.COLOR_GRAY2BGR),use_det=False)
            texts = getattr(result,'txts',None)
            text = ''.join(texts or ()).strip().translate(str.maketrans({'|':'1','I':'1','i':'1','l':'1'}))
            if text.isascii() and text.isdigit() and 2 <= int(text) <= 25:
                return int(text)
            # A thin white badge border was merged with the leading 1, making
            # a real 16 read as "[G". Retry the actual glyphs without the border;
            # normalize G only in this numeric badge crop, never in free text.
            if clean.shape[1] > 12:
                clean[:, :12] = 0
                points = cv2.findNonZero(clean)
                if points is not None:
                    x, y, w, h = cv2.boundingRect(points)
                    canvas = cv2.copyMakeBorder(255-clean[y:y+h, x:x+w], 4, 4, 4, 4,
                                               cv2.BORDER_CONSTANT, value=255)
                    canvas = cv2.resize(canvas, None, fx=6, fy=6)
                    self._selection_ocr()
                    result = ocr(cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR), use_det=False)
                    text = ''.join(getattr(result, 'txts', None) or ()).strip().translate(
                        str.maketrans({'|':'1', 'I':'1', 'i':'1', 'l':'1', 'G':'6'}))
                    if text.isascii() and text.isdigit() and 2 <= int(text) <= 25:
                        return int(text)
        except SelectionTimeout:
            raise
        except Exception:
            pass
        return None

    @selection_stage('rank')
    def rank_wall_groups(self, rows, log):
        self._ranked_selection = None
        groups = {}
        for row in rows:
            old = groups.get(row['cost'])
            if old is None or row.get('upgrade_level') is not None:
                groups[row['cost']] = dict(row)
        unknown = [row for row in groups.values() if row.get('upgrade_level') is None]
        last_selected = None
        for index, row in enumerate(unknown):
            self._selection_checkpoint()
            # Never assume an unreadable badge means a higher-level wall.
            found = self.seek_cost(row['cost'])
            if found is None:
                return None
            selected, level = self.choose_row(found,close=True)
            if level is None:
                log('[Wall] Chưa đọc rõ cấp của một nhóm; bỏ lượt để tránh ưu tiên sai cấp.')
                return None
            row['upgrade_level'] = level+1
            last_selected = (selected, level, dict(found))
            if index < len(unknown)-1 and not self.ensure_menu_open():
                return None
        ranked = sorted(groups.values(),key=lambda row:(row['upgrade_level'],row['cost']))
        v_state = getattr(self.manager, 'village_state', None)
        if v_state is not None:
            for r in ranked:
                lvl = r.get('upgrade_level')
                cnt = r.get('available')
                if isinstance(lvl, int) and lvl > 1 and isinstance(cnt, int) and cnt > 0:
                    v_state.record_wall_group('main', lvl - 1, cnt)
        if last_selected is not None:
            if last_selected[2]['cost'] == ranked[0]['cost']:
                self._ranked_selection = last_selected
            elif not self.ensure_menu_open():
                return None
        return ranked

    def scroll_menu(self, direction, fast=False):
        self._selection_checkpoint()
        if self._selection_metrics is not None and self._selection_deadline is not None:
            self._selection_metrics['scrolls'] += 1
        if fast:
            self._cached_scroll_steps = getattr(self, '_cached_scroll_steps', 0)+1
        if direction == 'down':
            self.adb.swipe(1410, 710, 1410, 310, duration_ms=550, jitter=0)
        else:
            self.adb.swipe(1410, 310, 1410, 710, duration_ms=550, jitter=0)
        offset = getattr(self.manager, '_wall_menu_offset', 0)
        if not isinstance(offset, int):
            offset = 0
        self.manager._wall_menu_offset = max(0, offset + (1 if direction == 'down' else -1))
        time.sleep(.35 if fast else .45)

    @frame_analysis
    def cached_menu_row(self, screen, cached):
        relative_y = cached.get('relative_y')
        if not isinstance(relative_y, (int,float)) or not .05 <= relative_y <= .9:
            return None
        top = max(50, min(700, int(relative_y*900)-45))
        words = self.ocr_positions(screen, (1235,top,365,130))
        return next((row for row in self.menu_rows(screen, words)
                     if row['cost'] == cached.get('cost')), None)

    @staticmethod
    def menu_signature(words):
        # Builder countdowns change every second even when the list cannot move.
        # Ignore those values when detecting the top/bottom of the menu.
        return tuple(re.sub(r'\s+', '', text.lower()) for text, _, _ in words
                     if not re.fullmatch(r'(?:[0-9oil|s]+\s*[dhms]\s*)+', text.strip(), re.I))

    @frame_analysis
    def is_menu_open(self, screen):
        """Check if builder suggestions menu is open on the right (specifically anchored at y < 55)."""
        if screen is None: return False
        close = self.vision.find_close_button(screen)
        # Builder suggestions menu close button is specifically at (1574, 30) with y < 55!
        if close and close[0] > 1530 and close[1] < 55: return True
        ocr = self.vision._get_ocr()
        if ocr:
            try:
                self._selection_ocr()
                res = ocr(screen[10:90, 1170:1580])
                if res and hasattr(res, 'txts') and res.txts:
                    for t in res.txts:
                        if any(k in t.lower() for k in ('building', 'hero', 'suggest', 'other')):
                            return True
            except SelectionTimeout: raise
            except Exception: pass
        return False

    @selection_stage('open_menu')
    def ensure_menu_open(self):
        """Ensures the builder suggestions menu is open on the right, avoiding double-tap toggle."""
        screen = self.adb.screencap()
        if self.is_menu_open(screen):
            return True
        self._selection_checkpoint()
        self.adb.tap(1455, 690, jitter=0, delay_after=0.3)
        for _ in range(7):
            self._selection_checkpoint()
            time.sleep(0.35)
            screen = self.adb.screencap()
            if self.is_menu_open(screen):
                self.manager._wall_menu_offset = 0
                return True
        # Retry once if not open after 2.5s
        self._selection_checkpoint()
        self.adb.tap(1455, 690, jitter=0, delay_after=0.3)
        for _ in range(5):
            self._selection_checkpoint()
            time.sleep(0.35)
            screen = self.adb.screencap()
            if self.is_menu_open(screen):
                self.manager._wall_menu_offset = 0
                return True
        return False

    @selection_stage('seek_row')
    def seek_cost(self, cost, available=None):
        # Start where the menu actually is. Only reverse at a verified boundary.
        def matches(row):
            return (row is not None and row['cost'] == cost
                    and (available is None or row['available'] == available))
        screen = self.adb.screencap()
        cached = (getattr(self.manager, '_lowest_wall_target', None)
                  if self.settings.get('wall_lowest_first', True) else None)
        if isinstance(cached, dict) and cached.get('cost') == cost:
            direct = self.cached_menu_row(screen, cached)
            if matches(direct):
                self._cache_visible_on_open = True
                return direct
        words = self.menu_words(screen)
        visible = self.menu_rows(screen, words, read_level=available is None)
        for row in visible:
            if matches(row):
                if isinstance(cached, dict) and cached.get('cost') == cost:
                    self._cache_visible_on_open = True
                return row
        if isinstance(cached, dict) and cached.get('cost') == cost:
            hint = cached.get('scroll_down', 0)
            if isinstance(hint, int) and 0 < hint <= 12:
                previous = self.menu_signature(words)
                for _ in range(hint):
                    self.scroll_menu('down', fast=True)
                    screen = self.adb.screencap()
                    direct = self.cached_menu_row(screen, cached)
                    if matches(direct):
                        return direct
                    words = self.menu_words(screen)
                    visible = self.menu_rows(screen, words, read_level=available is None)
                    for row in visible:
                        if matches(row):
                            return row
                    signature = self.menu_signature(words)
                    if not signature or signature == previous:
                        break
                    previous = signature
            # A changed page/list height can shift the remembered row. Check a
            # small neighborhood, then let the caller rebuild the full cache.
            for direction in ('up','up','down','down','down','down'):
                self.scroll_menu(direction, fast=True)
                screen = self.adb.screencap()
                rows = self.menu_rows(screen, self.menu_words(screen), read_level=available is None)
                match = next((row for row in rows if matches(row)), None)
                if match:
                    return match
            return None
        offset = getattr(self.manager, '_wall_menu_offset', 0)
        if not isinstance(offset, int):
            offset = 0
        if visible:
            first = 'up' if cost > max(row['cost'] for row in visible) else 'down'
        else:
            first = 'up' if offset > 0 else 'down'
        for direction in (first, 'up' if first == 'down' else 'down'):
            previous = None
            for _ in range(12):
                self._selection_checkpoint()
                screen = self.adb.screencap()
                words = self.menu_words(screen)
                rows = [row for row in self.menu_rows(screen, words, read_level=available is None) if matches(row)]
                if rows:
                    return rows[0]
                signature = self.menu_signature(words)
                if not signature or signature == previous:
                    break
                previous = signature
                self.scroll_menu(direction)
        return None

    @selection_stage('settle_row')
    def stable_menu_row(self, row):
        previous = row
        consecutive = True
        expected = row.get('upgrade_level')
        time.sleep(.25)
        for _ in range(5):
            self._selection_checkpoint()
            frame = self.adb.screencap()
            self.last_menu_row_frame = frame
            if not self.is_menu_open(frame):
                return None
            hint = dict(cost=row['cost'],relative_y=previous['y']/900)
            current = self.cached_menu_row(frame,hint)
            if current is not None and current['available'] != row['available']:
                current = None
            if current is None:
                top = max(50, min(680, int(previous['y']) - 60))
                words = self.ocr_positions(frame, (1235, top, 365, 140))
                current = next((candidate for candidate in self.menu_rows(frame, words, read_level=expected is not None)
                                if candidate['cost'] == row['cost'] and candidate['available'] == row['available']), None)
            if current is None:
                current = next((candidate for candidate in self.menu_rows(frame, self.menu_words(frame), read_level=expected is not None)
                                if candidate['cost'] == row['cost'] and candidate['available'] == row['available']), None)
            if current is not None and current['available'] == row['available'] and (expected is None or current.get('upgrade_level') in (None, expected)):
                if (consecutive and current['available'] == previous['available']
                        and abs(current['y']-previous['y']) <= 8
                        and abs(current['x']-previous['x']) <= 12):
                    return current
                previous = current
                consecutive = True
            else:
                consecutive = False
            time.sleep(.15)
        return None

    @selection_stage('choose_and_verify')
    def choose_row(self, row, close=False):
        self._menu_selection_evidence=None
        self._selection_mismatch = False
        self._selection_row_tapped = False
        stable = self.stable_menu_row(row)
        if stable is None:
            return None, None
        row.update(stable)
        self._selection_checkpoint()
        if self._selection_metrics is not None:
            self._selection_metrics['row_taps'] += 1
        self._selection_row_tapped = True
        self.record_wall_tap('menu-row', getattr(self,'last_menu_row_frame',None),
                             (row['x'],row['y']),dict(cost=row['cost'],available=row['available']))
        self.adb.tap(row['x'], row['y'], jitter=0, delay_after=.5)
        if close:
            fresh = self.adb.screencap()
            self.save_wall_frame('wall-menu-row-after.png', fresh)
            if self.is_menu_open(fresh):
                button = self.vision.find_close_button(fresh)
                if not button or not (button[0] > 1530 and button[1] < 55):
                    return None, None
                self._selection_checkpoint()
                self.adb.tap(button[0], button[1], jitter=0, delay_after=.5)
        # Confirm a settled wall title. Exact contour lists fluctuate with map
        # animations/discount tags; Upgrade geometry is checked separately later.
        previous = None
        for attempt in range(6):
            self._selection_checkpoint()
            screen = self.adb.screencap()
            if attempt == 0:
                self.save_wall_frame('wall-selection-after-close.png', screen)
            if screen is not None and not self.is_menu_open(screen):
                level = self.level(screen, fast=True)
                expected = row.get('upgrade_level')
                if level is not None and type(expected) is int and level != expected-1:
                    self._selection_mismatch = True
                    return None, None
                controls = self.wall_controls_present(screen)
                if level is not None and level == previous and controls:
                    self._menu_selection_evidence=dict(level=level,cost=row['cost'],available=row['available'])
                    self.last_selection = screen
                    return screen, level
                previous = level
                # Two settled observations with live wall controls can use
                # the verified menu badge when title OCR alone is unreadable.
                if (not self.settings.get('wall_lowest_first', True)
                        and attempt >= 1 and level is None and controls
                        and type(expected) is int and 2 <= expected <= 25):
                    self._menu_selection_evidence = dict(level=expected-1, cost=row['cost'],
                                                         available=row['available'])
                    self.last_selection = screen
                    return screen, expected-1
            time.sleep(.15)
        # Background animations may change pixels; wall title + closed menu is
        # still required, and the final Upgrade tap gets a separate fresh check.
        if screen is not None and not self.is_menu_open(screen):
            level = self.level(screen)
            if level is not None and self.wall_controls_present(screen):
                if type(row.get('upgrade_level')) is int and level != row['upgrade_level']-1:
                    self._selection_mismatch = True
                    return None, None
                self._menu_selection_evidence=dict(level=level,cost=row['cost'],available=row['available'])
                self.last_selection = screen
                return screen, level
            destination=row.get('upgrade_level')
            if (not self.settings.get('wall_lowest_first', True)
                    and type(destination) is int and 2<=destination<=25 and self.wall_controls_present(screen)):
                self._menu_selection_evidence=dict(level=destination-1,cost=row['cost'],available=row['available'])
                self.last_selection=screen
                return screen,destination-1
        self.save_wall_frame('wall-selection-unverified.png', screen)
        return None, None

    def select_from_menu(self, log):
        self._selection_started = getattr(self, '_lookup_budget_started', None) or time.monotonic()
        self._selection_deadline = self._selection_started + 30
        self._selection_log = log
        self._selection_metrics = dict(ocr=0, scrolls=0, row_taps=0, recovery=0,
                                       reason='selected', stages={})
        try:
            selected = self._select_from_menu(log)
            if selected is None:
                self._selection_metrics['reason'] = 'unverified'
            return selected
        except SelectionTimeout as exc:
            self._selection_metrics['reason'] = 'timeout'
            self._selection_timed_out = True
            self._selection_deadline = None
            log(f'[Wall] {exc}; đóng menu, hoãn lượt nâng.')
            self.close_dialog()
            self.manager.deselect_all()
            val = getattr(self.manager, '_wall_no_target_count', 0)
            no_target_count = (val if isinstance(val, int) else 0) + 1
            self.manager._wall_no_target_count = no_target_count
            if no_target_count >= 3:
                log(f'[Wall] Đã thử tìm tường {no_target_count} lần bị timeout/không thấy; kiểm tra xác minh qua Layout Editor...')
                verified = self.verify_walls_via_layout_editor(log)
                if isinstance(verified, dict):
                    self.manager._wall_no_target_count = 0
                    if verified.get('maxed') is True:
                        self.manager.walls_confirmed_maxed = True
                        self.manager.wall_result = dict(status='maxed', reason=self.manager.wall_status)
                        return None
                    self.manager.walls_confirmed_maxed = False
                    self.manager.wall_result = dict(status='selection_failed', reason=self.manager.wall_status)
                    return None
            return None
        finally:
            self._selection_deadline = None
            elapsed = time.monotonic() - self._selection_started
            self._selection_metrics.update(seconds=round(elapsed, 3), overrun=round(max(0, elapsed-30), 3))
            history = getattr(self.manager, 'wall_selection_history', None)
            if not isinstance(history, deque):
                history = self.manager.wall_selection_history = deque(maxlen=100)
            history.append(dict(self._selection_metrics))
            values = sorted(item['seconds'] for item in history)
            p50 = values[(len(values)-1)//2]
            p95 = values[(95*len(values)+99)//100-1]
            recoveries = sum(item['recovery'] for item in history)
            self.manager.wall_selection_metrics = dict(self._selection_metrics, p50=p50, p95=p95,
                                                        recoveries=recoveries, samples=len(history))
            log(f'[Wall] Chọn nhóm {elapsed:.1f}s; OCR {self._selection_metrics["ocr"]}, '
                f'cuộn {self._selection_metrics["scrolls"]}, chọn {self._selection_metrics["row_taps"]}; '
                f'{self._selection_metrics["reason"]}; p50/p95 {p50:.1f}/{p95:.1f}s; '
                f'phục hồi {recoveries}/{len(history)} lượt; vượt ngân sách {max(0, elapsed-30):.1f}s.')
            try:
                folder = Path(__file__).resolve().parents[1]/'supervision'/'runtime'
                folder.mkdir(parents=True, exist_ok=True)
                (folder/'wall-selection-timing.json').write_text(
                    json.dumps(self.manager.wall_selection_metrics, ensure_ascii=False, indent=2, default=StatsStore.json_default), encoding='utf-8')
            except (OSError, TypeError, ValueError):
                pass

    def reverify_selection_inventory(self, log):
        """The 30-second row budget must not interrupt Layout Editor cleanup."""
        self._selection_deadline = None
        return self.verify_walls_via_layout_editor(log)

    def _select_from_menu(self, log):
        self._selection_timed_out = False
        lookup_started = time.monotonic()
        self._cached_scroll_steps = 0
        # 1. Clean state: close any lingering dialogs
        for _ in range(3):
            screen = self.adb.screencap()
            if self.is_menu_open(screen):
                break
            if not self.vision.is_in_main_village(screen) or self.vision.has_open_dialog(screen):
                self.manager.close_popups_and_dialogs(screen)
                time.sleep(0.4)
            else:
                break
        self.manager.deselect_all()
        time.sleep(0.3)

        # 2. Open builder suggestions menu cleanly
        if not self.ensure_menu_open():
            log('[Wall] Không mở được Menu thợ.')
            return None
        opening = self.adb.screencap()
        self.manager._wall_menu_offset = 0
        self._cache_visible_on_open = False
        scan_both_directions = False
        lowest_first = self.settings.get('wall_lowest_first', True)
        inventory = getattr(getattr(self.manager, 'village_state', None), 'wall_inventory', {})
        inventory = inventory.get('main', {}) if isinstance(inventory, dict) else {}
        known_levels = [int(level) for level, count in inventory.items()
                        if str(level).isdigit() and int(count) > 0]
        known_lowest = min(known_levels) if lowest_first and known_levels else None
        known_count = (inventory.get(known_lowest, inventory.get(str(known_lowest)))
                       if known_lowest is not None else None)
        if lowest_first and (known_lowest is None or type(known_count) is not int or known_count <= 0):
            log('[Wall] Chưa có số đoạn/cấp tường đã xác minh; quét Layout Editor trước khi chọn.')
            self.reverify_selection_inventory(log)
            return None

        # 3. Check cached lowest wall target
        cached = (getattr(self.manager, '_lowest_wall_target', None)
                  if self.settings.get('wall_lowest_first', True) else None)
        if (isinstance(cached, dict) and known_lowest is not None
                and (cached.get('level') != known_lowest or cached.get('available') != known_count)):
            cached = None
            self.manager._lowest_wall_target = None
            self.manager.save_wall_target(None)
        if isinstance(cached, dict) and time.time()-cached.get('scanned_at', time.time()) >= 1800:
            cached = None
            self.manager._lowest_wall_target = None
        if isinstance(cached, dict) and self.visible_lower_wall(opening,cached['level']):
            log('[Wall] Thấy nhóm cấp thấp hơn nhóm đã nhớ; cập nhật ưu tiên cấp thấp trước.')
            cached = None
            self.manager._lowest_wall_target = None
            self.manager.save_wall_target(None)
        if cached:
            row = self.seek_cost(cached['cost'], cached['available'])
            if row:
                if lowest_first:
                    row.pop('upgrade_level', None)
                elif row.get('upgrade_level') is None:
                    row['upgrade_level'] = cached['level'] + 1
                self._selection_row_tapped = False
                selected, level = self.choose_row(row, close=True)
                if level == cached['level']:
                    self.available_walls = row['available']
                    self.remember_group(row, level)
                    log(f"[Wall] Ưu tiên nhóm thấp nhất đã kiểm tra: level {level}, còn {row['available']} đoạn.")
                    log(f'[Wall] Bộ nhớ tìm lại nhóm trong {time.monotonic()-lookup_started:.1f}s, '
                        f'cuộn nhanh {self._cached_scroll_steps} lần.')
                    return selected
                if level is None and self._selection_row_tapped and not getattr(self, '_selection_mismatch', False):
                    self.manager._lowest_wall_target = None
                    self.manager.save_wall_target(None)
                    log('[Wall] Đã bấm dòng nhưng chưa xác minh được tường. Bỏ tọa độ cũ; lượt sau sẽ quét lại.')
                    return None
                if self._selection_metrics is not None:
                    self._selection_metrics['recovery'] = 1
                scan_both_directions = True
                log('[Wall] Dòng đã lưu chưa ổn định hoặc sai cấp. Quét lại trong ngân sách còn lại.')
            self.manager._lowest_wall_target = None
            self.manager.save_wall_target(None)
            if not self.ensure_menu_open():
                return None
            opening = self.adb.screencap()

        self._full_scan_at = time.time()
        self._menu_open_signature = self.menu_signature(self.menu_words(opening))
        first_match = not lowest_first
        all_seen_rows = (self.scan_wall_rows(both_directions=True, first_match=first_match,
                                             target_available=known_count)
                         if scan_both_directions else self.scan_wall_rows(
                             first_match=first_match, target_available=known_count))
        if not all_seen_rows:
            log('[Wall] Chưa thấy nhóm qua quét nhanh; dùng OCR đầy đủ để kiểm tra lại.')
            all_seen_rows = self.scan_wall_rows(precise=True, both_directions=scan_both_directions,
                                                first_match=first_match, target_available=known_count)
        elif (lowest_first and not any(row['available'] == known_count for row in all_seen_rows)):
            log(f'[Wall] Quét nhanh chưa đọc được nhóm {known_count} đoạn; tìm lại bằng OCR đầy đủ.')
            all_seen_rows.extend(self.scan_wall_rows(
                precise=True, both_directions=True, target_available=known_count))

        if not all_seen_rows:
            val = getattr(self.manager, '_wall_no_target_count', 0)
            no_target_count = (val if isinstance(val, int) else 0) + 1
            self.manager._wall_no_target_count = no_target_count
            if no_target_count >= 3:
                log(f'[Wall] Đã thử tìm tường {no_target_count} lần bình thường không thấy; kiểm tra xác minh qua Layout Editor (Chỉnh sửa làng)...')
                verified = self.verify_walls_via_layout_editor(log)
                if isinstance(verified, dict):
                    self.manager._wall_no_target_count = 0
                    if verified.get('maxed') is True:
                        self.manager.walls_confirmed_maxed = True
                        self.manager.wall_result = dict(status='maxed', reason=self.manager.wall_status)
                        return None
                    self.manager.walls_confirmed_maxed = False
                    self.manager.wall_result = dict(status='selection_failed', reason=self.manager.wall_status)
                    return None
            self.close_dialog()
            if no_target_count < 3:
                log(f'[Wall] Không tìm được nhóm tường nào trong danh sách (lần {no_target_count}/3); thử lại ở lượt sau.')
            else:
                log('[Wall] Không tìm được nhóm tường nào trong danh sách.')
            self.manager.wall_result = dict(status='no_target', reason=self.manager.wall_status)
            return None

        self.manager._wall_no_target_count = 0

        target = self.choose_wall_candidate(all_seen_rows, lowest_first, known_lowest, known_count)
        if target is None:
            log(f'[Wall] Layout Editor đã xác minh cấp {known_lowest} × {known_count}; '
                'Menu thợ chưa đọc được dòng khớp sau hai cách OCR, giữ làng và tìm lại, chưa bấm nâng.')
            self.close_dialog()
            return None
        min_cost = target['cost']
        # Check if current screen still has the min_cost row
        fresh_screen = self.adb.screencap()
        fresh_words = self.menu_words(fresh_screen)
        current_rows = [r for r in self.menu_rows(fresh_screen, fresh_words, read_level=False)
                        if r['cost'] == min_cost and (not lowest_first or type(known_count) is not int
                                                      or r['available'] == known_count)]
        if current_rows:
            target_row = current_rows[0]
        else:
            target_row = self.seek_cost(min_cost, known_count if lowest_first else None)

        if not target_row:
            self.close_dialog()
            log(f'[Wall] Không cuộn lại được tới nhóm tường giá {min_cost:,}.')
            return None

        # 7. Select the lowest wall row and close menu
        if (lowest_first and type(known_count) is int
                and target_row['available'] == known_count):
            # Badge OCR can read the destination 9 as 19. The verified count
            # identifies this row; use the wall title after selection for level.
            target_row.pop('upgrade_level', None)
        elif target_row.get('upgrade_level') is None and target.get('upgrade_level') is not None:
            target_row['upgrade_level'] = target['upgrade_level']
        selected, level = self.choose_row(target_row, close=True)
        if level is None:
            time.sleep(0.4)
            selected = self.adb.screencap()
            level = self.selection_level(selected)

        self.available_walls = target_row['available']
        if level is not None:
            expected_level = known_lowest if lowest_first else None
            if expected_level is not None and level != expected_level:
                log(f'[Wall] Cấp dòng menu {expected_level} khác tường đã chọn {level}; '
                    'không nâng nhóm này, kiểm tra lại lượt sau.')
                self.manager._lowest_wall_target = None
                self.manager.save_wall_target(None)
                self.manager.deselect_all()
                self.reverify_selection_inventory(log)
                return None
            self.remember_group(target_row, level)
            v_state = getattr(self.manager, 'village_state', None)
            if v_state is not None and isinstance(level, int) and target_row.get('available'):
                v_state.record_wall_group('main', level, int(target_row['available']))
            label = 'level thấp nhất' if lowest_first else 'nhóm OCR thấy trước'
            log(f"[Wall] Chọn {label} {level}; giá {min_cost:,}, còn {target_row['available']} đoạn.")
        else:
            if selected is not None:
                folder = Path(__file__).resolve().parents[1]/'supervision'/'runtime'
                folder.mkdir(exist_ok=True)
                cv2.imwrite(str(folder/'wall-selection-unrecognized.png'), selected)
            log('[Wall] Chưa xác minh được cấp tường sau khi đóng menu; dừng.')
            return None

        return selected

    @staticmethod
    def choose_wall_candidate(rows, lowest_first, known_lowest=None, known_count=None):
        if not lowest_first:
            # First OCR hit wins; skip ranking every group.
            return rows[0]
        # The badge OCR can add a spurious leading digit (9 -> 19). Require
        # the verified Layout Editor count, then check the selected wall title.
        if type(known_lowest) is not int or type(known_count) is not int or known_count <= 0:
            return None
        matching = [row for row in rows if row['available'] == known_count]
        return min(matching, key=lambda row: row['cost']) if matching else None

    def remember_group(self, row, level):
        offset = row.get('scroll_down', getattr(self.manager, '_wall_menu_offset', 0))
        previous = getattr(self.manager, '_lowest_wall_target', None)
        previous = previous if isinstance(previous, dict) else {}
        if getattr(self, '_cache_visible_on_open', False):
            offset = previous.get('scroll_down', offset)
        self.manager._lowest_wall_target = dict(
            level=level, cost=row['cost'], available=row['available'],
            relative_x=row['x']/1600, relative_y=row['y']/900,
            scroll_down=offset if isinstance(offset, int) else 0,
            origin_signature=getattr(self, '_menu_open_signature', previous.get('origin_signature')),
            scanned_at=getattr(self, '_full_scan_at', previous.get('scanned_at', time.time())))
        self.manager.save_wall_target(self.manager._lowest_wall_target)

    def inventory_count(self, unit_cost):
        screen = self.adb.screencap()
        if not self.is_menu_open(screen):
            self.adb.tap(1455, 690, jitter=0, delay_after=0.6)
        previous = None
        found = None
        for step in range(16):
            screen = self.adb.screencap()
            words = self.ocr_positions(screen, (1170, 50, 430, 755))
            signature = self.menu_signature(words)
            for text, x, y in words:
                match = re.search(r'wa[li1][li1]\s*[x×]\s*(\d+)', text, re.I)
                if not match: continue
                for price, px, py in words:
                    number = re.sub(r'[ ,.]', '', price).translate(str.maketrans({'I':'1','i':'1','l':'1','O':'0','o':'0'}))
                    if y <= py <= y + 60 and number.isascii() and number.isdigit() and int(number) == unit_cost:
                        found = int(match.group(1))
                        break
                if found is not None: break
            if found is not None: break
            if signature == previous and step > 1: break
            previous = signature
            self.scroll_menu('down')
        self.close_dialog()
        return found

    def quantity(self, screen):
        for roi in (self.header_roi(screen), (200,500,1150,135)):
            for filtered in (True,False):
                text = ' '.join(self.title_texts(screen,roi) if filtered else self.texts(screen,roi,scale=2))
                match = re.search(r'''wa[li1][li1].*?[xX×][\s'"‘’“”:.]*([0-9sSoOiIl|B]+)''', text, re.I)
                if match:
                    number = match.group(1).translate(str.maketrans(
                        {'s':'5','S':'5','o':'0','O':'0','i':'1','I':'1','l':'1','|':'1','B':'8','b':'8'}))
                    if number.isdigit() and 1 <= int(number) <= 1000:
                        return int(number)
        return None

    def title_texts(self, screen, roi):
        if screen is None:
            return self.texts(screen, roi, scale=2)
        x, y, width, height = roi
        # The pale yellow title sits over an animated village. Keep the glyphs
        # and remove background details that OCR was merging into letters/digits.
        crop = screen[y+(18 if height == 65 else 0):y+height, x:x+width]
        mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV),
                           (15, 0, 180), (45, 140, 255))
        mask = cv2.copyMakeBorder(mask, 10, 10, 10, 10, cv2.BORDER_CONSTANT)
        title = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        return self.texts(title, (0, 0, title.shape[1], title.shape[0]), scale=2)

    def toolbar_tiles(self, screen):
        """Locate actual beige action tiles, including their current size/offset."""
        if screen is None or screen.shape[:2] != (900, 1600):
            return []
        crop = screen[580:880, 200:1380]
        edges = cv2.Canny(crop, 60, 140)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        tiles = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if not (90 <= w <= 230 and 90 <= h <= 230):
                continue
            polygon = cv2.approxPolyDP(contour, .025 * cv2.arcLength(contour, True), True)
            if len(polygon) > 8 or cv2.contourArea(contour) < .8 * w * h:
                continue
            inner = crop[y+10:y+h-10, x+10:x+w-10]
            beige = cv2.inRange(cv2.cvtColor(inner, cv2.COLOR_BGR2HSV), (10, 15, 110), (40, 160, 255))
            if (beige > 0).mean() < .45:
                continue
            tile = (x + 200, y + 580, w, h)
            if all(abs(tile[0] - old[0]) > 10 or abs(tile[1] - old[1]) > 10 for old in tiles):
                tiles.append(tile)
        return sorted(tiles)

    def add_button(self, screen, step):
        # Count the width of the enabled green numeral group instead of matching
        # a +1 template against the leading glyphs of +10.
        for tile in self.toolbar_tiles(screen):
            if not self.wall_template(screen, 'addwall', .80, roi=tile):
                continue
            x, y, w, h = tile
            region = screen[y+int(h*.28):y+int(h*.58), x+int(w*.52):x+w-6]
            hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, (35, 100, 100), (90, 255, 255))
            points = cv2.findNonZero(mask)
            if points is None:
                continue
            _, _, glyph_width, glyph_height = cv2.boundingRect(points)
            if glyph_height < h*.10 or glyph_width < w*.08:
                continue
            actual_step = 10 if glyph_width >= w*.25 else 1
            if actual_step == step:
                return (x+w//2, y+h//2)
        return None

    def upgrade_button(self, screen, icon):
        self._upgrade_hit = None
        tiles = self.toolbar_tiles(screen)
        candidates = list(tiles)
        # A green discount tag can break the resource tile's outer contour.
        # Recover its column from the observed row spacing, then still require
        # the resource icon, beige interior and Upgrade label inside that tile.
        gaps = [right[0]-left[0] for left, right in zip(tiles, tiles[1:])
                if abs(left[1]-right[1]) <= 5 and abs(left[2]-right[2]) <= 5
                and left[2]*1.05 <= right[0]-left[0] <= left[2]*1.4]
        if gaps:
            pitch = min(gaps)
            for x, y, w, h in tiles:
                if not y < icon[1] < y+.4*h:
                    continue
                column = round((icon[0]-x-.9*w)/pitch)
                inferred = (round(x+column*pitch), y, w, h)
                if 200 <= inferred[0] <= 1380-w and inferred not in candidates:
                    candidates.append(inferred)
        for tile in candidates:
            x, y, w, h = tile
            if not (x + .65*w < icon[0] < x+w and y < icon[1] < y+.4*h):
                continue
            inner = screen[y+10:y+h-10, x+10:x+w-10]
            beige = cv2.inRange(cv2.cvtColor(inner, cv2.COLOR_BGR2HSV), (10,15,110), (40,160,255))
            if (beige > 0).mean() < .45:
                continue
            labels = self.ocr_positions(screen, (x, y + h//2, w, h//2))
            for text, label_x, label_y in labels:
                if (text.strip().lower() == 'upgrade'
                        and x+12 < label_x < x+w-12
                        and y+h*.55 < label_y < y+h-10):
                    point = (int(label_x), int(label_y))
                    self._upgrade_hit = dict(tile=tile, point=point, label=text,
                                             resource_icon=tuple(icon[:2]), inferred=tile not in tiles)
                    return point
        return None

    def upgrade_price(self,screen,resource):
        """Read price/color only beside the requested currency icon."""
        if not isinstance(screen,np.ndarray) or screen.shape[:2]!=(900,1600):return None,None
        icon=self.wall_template(screen,'goldwall' if resource=='gold' else 'elixerdwall',.86,
                                        roi=(200,610,1180,220))
        if not icon:return None,None
        x,y=icon[:2]
        amount=self.amount(screen,(max(0,x-155),y-22,140,44))
        return amount,self.price_is_red(screen,icon)

    def price_is_red(self,screen,icon):
        x,y=icon[:2]
        crop=screen[max(0,y-15):y+14,max(0,x-130):x-12]
        hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
        red=cv2.inRange(hsv,(0,95,140),(12,255,255))|cv2.inRange(hsv,(170,95,140),(179,255,255))
        contours,_=cv2.findContours(red,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        glyphs=[c for c in contours if 7<=cv2.boundingRect(c)[3]<=29 and cv2.contourArea(c)>=8]
        is_red=len(glyphs)>=2 and sum(cv2.contourArea(c) for c in glyphs)>=30
        return is_red

    def fit_upgrade_budget(self,screen,resource,target,unit_cost,balance,log,cap=None):
        """Fallback: inspect total; only Remove Wall is tapped until affordable."""
        if type(unit_cost) is not int or unit_cost<=0 or not 1<=target<=1000:return None
        cap=target if cap is None else cap
        mode=self.settings.get('wall_quantity_button_mode','ocr')
        seen=None;stable=0;unknown=0;waiting=None;waiting_reads=0
        for _ in range(1002):
            self.adb._guard_input()
            total,red=self.upgrade_price(screen,resource)
            if waiting is not None and total==waiting:
                waiting_reads+=1
                if waiting_reads>=4:
                    log('[Wall] Giá tổng chưa đổi sau -1; dừng dự phòng, không gửi lặp nút.')
                    return None
                time.sleep(.2);screen=self.adb.screencap();continue
            waiting=None;waiting_reads=0
            if type(total) is int and total>0 and total%unit_cost==0:
                count=total//unit_cost
                available=getattr(self,'available_walls',1000)
                if type(available) is int and 1<=count<=available:
                    target=count
            if red is None or total is None and not red:
                stable=0;seen=None
                unknown+=1
                if unknown>=3:return None
            elif red or type(total) is int and (total>balance or target>cap):
                if target<=1:
                    log(f'[Wall] Giá {resource} của một đoạn vẫn đỏ/thiếu tiền; kết thúc lượt nâng.')
                    return None
                point=self.quantity_button(screen,-1,mode)
                if not point:return None
                self.adb.tap(*point,jitter=0,delay_after=.25)
                waiting=total
                target-=1;seen=None;stable=0;unknown=0
                log(f'[Wall] Dự phòng giá: ô {resource} đỏ/vượt ngưỡng, bấm -1; dự kiến còn {target} đoạn.')
            elif type(total) is int and total==unit_cost*target and total<=balance:
                signature=(total,target)
                stable=stable+1 if signature==seen else 1
                seen=signature
                if stable>=2:
                    log(f'[Wall] Dự phòng giá: xác minh {target} đoạn, {total:,} {resource}, chữ không đỏ trên hai ảnh.')
                    return screen,target,total
            else:
                stable=0;seen=None
                unknown+=1
                if unknown>=3:return None
            time.sleep(.15)
            screen=self.adb.screencap()
        return None

    def fixed_upgrade_point(self, screen, resource, icon):
        if screen is None or screen.shape[:2] != (900,1600) or resource not in self.FIXED_UPGRADE_POINTS:
            return None
        if not icon:
            return None
        for layout, shift in self.FIXED_UPGRADE_LAYOUTS:
            expected_icon = self.FIXED_RESOURCE_ICONS[resource]
            if max(abs(icon[0]-expected_icon[0]-shift), abs(icon[1]-expected_icon[1])) > 18:
                continue
            base_point = self.FIXED_UPGRADE_POINTS[resource]
            point = (base_point[0]+shift,base_point[1])
            tile = ((809 if resource == 'gold' else 983)+shift, 626, 155, 153)
            x,y,w,h = tile
            inner = screen[y+15:y+h-12,x+12:x+w-12]
            beige = cv2.inRange(cv2.cvtColor(inner,cv2.COLOR_BGR2HSV),(10,15,110),(40,160,255))
            if (beige > 0).mean() < .45:
                continue
            self._upgrade_hit = dict(tile=tile,point=point,resource_icon=tuple(icon[:2]),
                                     source='fixed_1600x900',layout=layout)
            return point
        return None

    def record_wall_tap(self, stage, screen, point, details):
        """Keep the actual input point on the exact frame that authorized it."""
        if screen is None or not hasattr(screen, 'shape'):
            return
        try:
            folder = Path(__file__).resolve().parents[1]/'supervision'/'runtime'
            folder.mkdir(exist_ok=True)
            marked = screen.copy()
            tile = details.get('tile')
            if tile:
                x,y,w,h = tile
                cv2.rectangle(marked, (x,y), (x+w,y+h), (0,255,0), 2)
            cv2.drawMarker(marked, point, (0,0,255), cv2.MARKER_CROSS, 25, 2)
            cv2.putText(marked, f'{stage}: {point[0]}, {point[1]}',
                        (max(10,point[0]-120), max(30,point[1]-30)),
                        cv2.FONT_HERSHEY_SIMPLEX, .65, (0,0,255), 2)
            cv2.imwrite(str(folder/f'wall-{stage}-before.png'), screen)
            cv2.imwrite(str(folder/f'wall-{stage}-tap.png'), marked)
            (folder/f'wall-{stage}-tap.json').write_text(json.dumps(dict(
                timestamp=datetime.now(timezone.utc).isoformat(), point=point, **details), indent=2, default=StatsStore.json_default), encoding='utf-8')
        except (OSError, TypeError, ValueError):
            pass

    def save_wall_frame(self, name, screen):
        if isinstance(screen,np.ndarray) and screen.size:
            try:
                folder = Path(__file__).resolve().parents[1]/'supervision'/'runtime'
                folder.mkdir(exist_ok=True)
                cv2.imwrite(str(folder/name), screen)
            except (OSError,cv2.error):
                pass

    def save_verification(self):
        try:
            folder = Path(__file__).resolve().parents[1]/'supervision'/'runtime'
            folder.mkdir(exist_ok=True)
            (folder/'wall-last-verification.json').write_text(
                json.dumps(self.last_upgrade_verification, indent=2, default=StatsStore.json_default), encoding='utf-8')
        except (OSError, TypeError, ValueError):
            pass

    def open_checked_upgrade(self, resource, level, quantity, total, resources, single, log):
        options = dict(single=True) if single else {}
        first = self.prepare_upgrade(resource, level, quantity, total, **options)
        if first is None:
            log('[Wall] Chưa sẵn sàng bấm Upgrade: '+getattr(self,'last_prepare_reason','chưa xác minh ô tài nguyên')+'.')
            return None
        baseline = self.vision.read_village_resources(self.last_quantity_screen)
        if not baseline.get('valid', {}).get(resource, False) or baseline[resource] < total:
            log('[Wall] Chưa xác minh số dư ngay trước nâng; không bấm Upgrade.')
            return None
        # HUD OCR takes time. Locate the label again on a new frame afterwards,
        # rechecking quantity, level and price instead of reusing an old point.
        final = self.prepare_upgrade(resource, level, quantity, total, **options)
        if final is None or max(abs(final[0]-first[0]), abs(final[1]-first[1])) > 8:
            log('[Wall] Vị trí Upgrade/nhóm tường chưa ổn định giữa hai ảnh; đọc lại, không bấm tọa độ cũ.')
            return None
        resources[resource] = baseline[resource]
        details = dict(getattr(self, '_upgrade_hit', None) or {})
        details.pop('point', None)
        details.update(resource=resource, quantity=quantity, level=level, total=total, first_point=first)
        self.record_wall_tap('upgrade', self.last_quantity_screen, final, details)
        log(f'[Wall] Bấm giữa nhãn Upgrade tại ({final[0]}, {final[1]}), '
            f'{quantity} đoạn, {total:,} {resource}.')
        self.adb.tap(*final, jitter=0, delay_after=.8)
        confirm = self.adb.screencap()
        self.save_wall_frame('wall-upgrade-after.png', confirm)
        return confirm

    def confirmation_controls(self, screen):
        if screen is None:
            return None
        if self.vision.wall_confirmation(screen):
            button = self.vision.match_template(screen, 'okay', .80, roi=(750,450,450,250))
            if button:
                return ('bulk', tuple(button[:2]))
        button = self.vision.match_template(screen, 'confirmwall', .85, roi=(900,700,400,150))
        if button:
            title = ' '.join(self.texts(screen, (350,25,900,80))).lower()
            if 'upgrade' in title and 'wall' in title:
                return ('single', tuple(button[:2]))
        return None

    def wait_for_wall_confirmation(self, initial, log, timeout=5):
        """Wait for the actual popup and a settled Confirm/Okay, never time alone."""
        started = time.monotonic()
        previous = None
        screen = initial
        for _ in range(20):
            self.adb._guard_input()
            self.last_confirmation = screen
            if self.dismiss_resource_purchase(screen,log):
                return None
            current = self.confirmation_controls(screen)
            if (current is not None and previous is not None and current[0] == previous[0]
                    and max(abs(current[1][0]-previous[1][0]), abs(current[1][1]-previous[1][1])) <= 4):
                log(f'[Wall] Popup xác nhận đã hiện và nút ổn định sau {time.monotonic()-started:.1f}s.')
                return screen
            previous = current
            if time.monotonic()-started >= timeout:
                break
            time.sleep(.2)
            screen = self.adb.screencap()
        log('[Wall] Chưa thấy popup/nút Confirm ổn định; không bấm thêm hoặc dò nút X trên làng.')
        self.save_wall_frame('wall-popup-timeout.png', screen)
        return None

    def dismiss_resource_purchase(self,screen,log):
        if screen is None:
            return False
        # Resource purchase is a separate dialog, never a wall confirmation.
        # Check the title first so normal popup polling does not OCR its body.
        title=' '.join(self.texts(screen,(300,130,1000,190))).lower()
        if not re.search(r'you need more|need more (?:gold|elixir)|buy (?:the )?missing|kh[oô]ng [dđ][uủ]',title):
            return False
        self._resource_purchase_blocked=True
        self._wall_retryable=False
        self.save_wall_frame('wall-resource-purchase-blocked.png',screen)
        log('[Wall] Hiện bảng thiếu tài nguyên/mua bằng gem; thoát bảng, bỏ lượt nâng, không mua.')
        self.adb.keyevent(4)
        time.sleep(.3)
        return True

    @staticmethod
    def quantity_plan(current, target, available):
        """Fewest +10/+1/-1 taps, never selecting more than the known inventory."""
        queue = deque([(current, [])])
        seen = {current}
        while queue:
            count, plan = queue.popleft()
            if count == target:
                return plan
            for step in (10, 1, -1):
                next_count = count + step
                if 1 <= next_count <= available and next_count not in seen:
                    seen.add(next_count)
                    queue.append((next_count, plan + [step]))
        return []

    def enter_more(self, screen):
        if self.is_menu_open(screen):
            return None
        more = self.wall_template(screen,'upmore',.76,roi=(200,620,950,170))
        point = tuple(more[:2]) if more else None
        if point is None:
            if self.quantity(screen) is not None:
                return screen
            point = next(((x,y) for text,x,y in self.ocr_positions(screen,(200,620,950,170))
                          if 'more' in text.lower()),None)
        if point is not None:
            self.record_wall_tap('more',screen,point,{})
            self.adb.tap(*point,jitter=0,delay_after=.5)
            started = time.monotonic()
            for _ in range(10):
                fresh = self.adb.screencap()
                self.save_wall_frame('wall-more-after.png',fresh)
                if self.quantity(fresh) is not None:
                    return fresh
                if time.monotonic()-started >= 3:
                    break
                time.sleep(.15)
        return None

    def remove_button(self, screen):
        for tile in self.toolbar_tiles(screen):
            if self.wall_template(screen, 'removewall', .80, roi=tile):
                x, y, w, h = tile
                return (x + w//2, y + h//2)
        return None

    def fixed_quantity_button(self, screen, step):
        """Canonical Add/Remove centers; validate the tile without running OCR."""
        if screen is None or screen.shape[:2] != (900,1600) or step not in (-1,1,10):
            return None
        base_x={-1:285,10:460,1:634}[step]
        for _,shift in self.FIXED_UPGRADE_LAYOUTS:
            tile=(base_x+shift,626,155,153)
            label=self.wall_template(screen,'removewall' if step==-1 else 'addwall',.80,roi=tile)
            if not label or abs(label[0]-(base_x+shift+77))>12 or not 725<=label[1]<=765:
                continue
            x,y,w,h=tile
            inner=screen[y+15:y+h-12,x+12:x+w-12]
            beige=cv2.inRange(cv2.cvtColor(inner,cv2.COLOR_BGR2HSV),(10,15,110),(40,160,255))
            if (beige>0).mean()<.45:
                continue
            if step>0:
                glyph=screen[y+int(h*.28):y+int(h*.58),x+int(w*.52):x+w-6]
                mask=cv2.inRange(cv2.cvtColor(glyph,cv2.COLOR_BGR2HSV),(35,100,100),(90,255,255))
                pixels=cv2.findNonZero(mask)
                if pixels is None:
                    continue
                _,_,gw,gh=cv2.boundingRect(pixels)
                if gh<h*.10 or gw<w*.08 or (10 if gw>=w*.25 else 1)!=step:
                    continue
            return (base_x+shift+77,702)
        return None

    def quantity_button(self,screen,step,mode):
        if mode=='fixed':
            btn = self.fixed_quantity_button(screen,step)
            if btn: return btn
        return self.add_button(screen,step) if step>0 else self.remove_button(screen)

    def adjust_quantity_fixed(self,screen,target,current,log):
        available=getattr(self,'available_walls',max(current,target))
        if type(available) is not int or not 1<=current<=available<=1000 or not 1<=target<=available:
            return None
        plan=self.quantity_plan(current,target,available)
        if current==target:
            return screen
        if not plan:
            return None
        anchor=self.fixed_quantity_button(screen,plan[0])
        if anchor is None and 10 in plan:
            alt_plan = self.quantity_plan(current, target, min(available, target))
            if not alt_plan:
                alt_plan = [1] * (target - current) if target > current else [-1] * (current - target)
            if alt_plan:
                alt_anchor = self.fixed_quantity_button(screen, alt_plan[0])
                if alt_anchor is not None:
                    plan = alt_plan
                    anchor = alt_anchor
        if anchor is None:
            log('[Wall] Chưa khớp bố cục nút cố định; bỏ lượt chọn.')
            return None
        centers={-1:362,10:537,1:711}
        shift=anchor[0]-centers[plan[0]]
        points=[(centers[step]+shift,702) for step in plan]
        gap=max(50,min(1000,int(self.settings.get('wall_quantity_fixed_gap_ms',120))))
        # One bounded input sequence: no screenshots or count OCR between taps.
        self.adb.tap_batch(points,jitter=0,interval_ms=gap,delay_after=.15)
        self.quantity_trace=[dict(before=current,steps=plan,target=target,points=points,
                                  button_mode='fixed',verified=False,gap_ms=gap)]
        log(f'[Wall] Cố định: gửi {len(plan)} lần bấm ({", ".join(f"{step:+d}" for step in plan)}), dự kiến {current} → {target}; đối chiếu sau giao dịch.')
        return self.adb.screencap()

    def adjust_quantity(self, screen, target, log, allow_partial=False):
        self.quantity_trace = []
        mode=self.settings.get('wall_quantity_button_mode','ocr')
        mode='fixed' if mode=='fixed' else 'ocr'
        current = self.quantity(screen)
        if mode=='fixed':
            res = self.adjust_quantity_fixed(screen,target,current,log) if current is not None else None
            if res is not None:
                return res
            log('[Wall] Không khớp bố cục nút cố định; chuyển sang nhận diện tuần tự.')
            mode = 'ocr'
        for _ in range(120):
            if current == target: return screen
            if current is None: return None
            available = getattr(self, 'available_walls', max(current, target))
            if not isinstance(available, int) or not current <= available <= 1000:
                return None
            plan = self.quantity_plan(current, target, available)
            if not plan:
                return None
            expected = plan[0]
            if expected > 0:
                button = self.quantity_button(screen,expected,mode)
                if not button and expected == 10:
                    # A newly opened/reflowed toolbar can take another frame.
                    time.sleep(.15)
                    screen = self.adb.screencap()
                    if self.quantity(screen) != current:
                        return None
                    button = self.quantity_button(screen,10,mode)
                if not button and expected == 10:
                    expected = 1
                    button = self.quantity_button(screen,1,mode)
                if not button:
                    if allow_partial and current > 1:
                        log(f'[Wall] Không thấy nút thêm; giữ {current}/{target} đoạn đã xác nhận và kiểm tra giá thực tế.')
                        return screen
                    log('[Wall] Không thấy nút thêm tường đang bật; dừng chọn.')
                    return None
            else:
                button = self.quantity_button(screen,-1,mode)
                if not button: return None
                expected = -1
            self.adb.tap(button[0], button[1], jitter=0, delay_after=0.15)
            deadline = time.monotonic() + 3
            expected_count = current + expected
            confirmed = 0
            observations = []
            while True:
                fresh = self.adb.screencap()
                new_count = self.quantity(fresh)
                observations.append(new_count)
                # Header digits animate after adding walls. An intermediate
                # frame can read 12 as 121; never plan the next tap from it.
                confirmed = confirmed + 1 if new_count == expected_count else 0
                if confirmed >= 2:
                    break
                if time.monotonic() >= deadline and (confirmed == 0 or len(observations) >= 5):
                    break
                time.sleep(.12)
            self.quantity_trace.append(dict(before=current, step=expected, button=button,
                                            expected=expected_count, readings=observations, button_mode=mode))
            if confirmed < 2:
                folder = Path(__file__).resolve().parents[1]/'supervision'/'runtime'
                folder.mkdir(exist_ok=True)
                if fresh is not None and hasattr(fresh, 'shape'):
                    cv2.imwrite(str(folder/'wall-quantity-failed.png'), fresh)
                import json
                (folder/'wall-quantity-failed.json').write_text(
                    json.dumps(dict(target=target, taps=self.quantity_trace), indent=2, default=StatsStore.json_default), encoding='utf-8')
                log(f'[Wall] Bấm {expected:+d} tại ({button[0]}, {button[1]}), '
                    f'số đoạn {current} -> {new_count}, dự kiến {current+expected}; dừng để không nâng sai số lượng.')
                return None
            log(f'[Wall] Chọn tường {current} → {new_count}/{target} ({expected:+d}).')
            current = new_count
            screen = fresh
        return None

    def single_selected(self, screen, level):
        """A single wall has Upgrade More, not the bulk Add/Remove toolbar."""
        if screen is None or self.is_menu_open(screen) or self.selection_level(screen) != level:
            return False
        if self.wall_template(screen,'upmore',.86,roi=(200,620,950,170)):
            return True
        if self.quantity(screen) is not None:
            return False
        return any('more' in text.lower() for text, _, _ in
                   self.ocr_positions(screen, (200, 620, 950, 170)))

    def prepare_upgrade(self, resource, level, quantity, total, single=False):
        """Revalidate selection and locate the live tile immediately before input."""
        self.last_prepare_reason='chưa có ảnh bộ chọn'
        for _ in range(3):
            screen = self.adb.screencap()
            self.last_prepare_screen = screen
            if screen is None or self.is_menu_open(screen):
                self.last_prepare_reason='ảnh/menu chọn tường chưa sẵn sàng'
                time.sleep(.25)
                continue
            fixed=not single and self.settings.get('wall_quantity_button_mode','ocr')=='fixed'
            actual_level=self.selection_level(screen)
            selection_valid = (self.single_selected(screen, level) if single else
                               actual_level == level and (fixed or self.quantity(screen) == quantity))
            if not selection_valid:
                self.last_prepare_reason=f'tiêu đề/nhóm đang chuyển trạng thái (cấp đọc {actual_level}, cần {level})'
                self.save_wall_frame('wall-prepare-selection-failed.png',screen)
                time.sleep(.25)
                continue
            icon = self.wall_template(screen, 'goldwall' if resource == 'gold' else 'elixerdwall', .86,
                                              roi=(200, 610, 1180, 220))
            if icon and not self.price_is_red(screen,icon) and (fixed or self.amount(screen, (max(0, icon[0]-155), icon[1]-22, 140, 44)) == total):
                button = self.fixed_upgrade_point(screen, resource, icon)
                if button is None:
                    button = self.upgrade_button(screen, icon)
                if button:
                    self.last_quantity_screen = screen
                    self.last_prepare_reason='đã xác minh'
                    return button
                self.last_prepare_reason='chưa khớp bố cục nút Upgrade trong ô '+resource
            else:
                self.last_prepare_reason='chưa đọc rõ biểu tượng/giá trong ô '+resource
            self.save_wall_frame('wall-prepare-button-failed.png',screen)
            time.sleep(.2)
        return None

    def run(self, screen, current_gold, current_elixir, log_callback=None, eligible_resources=None, force_single=False):
        self._wall_retryable = False
        self._verified_lowest_selection = False
        self._transaction_submitted = False
        self._wall_popup_pending = False
        self._resource_purchase_blocked = False
        self.last_upgrade = None
        external_log = log_callback or (lambda msg: None)
        def log(message):
            self.manager.wall_status = message.removeprefix('[Wall] ')
            external_log(message)
        if not self.settings.get('auto_upgrade_walls', True): return False
        if screen is None or screen.shape[:2] != (900, 1600):
            log('[Wall] Kích thước màn hình chưa đúng chuẩn 1600x900; bỏ qua.')
            return False

        resources = dict(gold=current_gold, elixir=current_elixir)
        allowed = self.settings.get('wall_resource', 'both')
        choices = [name for name in ('gold', 'elixir') if allowed in ('both', name)
                   and (resources[name] >= int(self.settings.get('min_' + name + '_to_upgrade_wall', 3000000))
                        if eligible_resources is None else name in eligible_resources and resources[name] > 0)]
        if not choices:
            log('[Wall] Tài nguyên dưới ngưỡng; không mở nâng cấp.')
            return False

        free = self.builders(screen)
        village_state = getattr(self.manager, 'village_state', None)
        if village_state is not None and type(free) is int:
            village_state.free_builders = free
        # Walls finish immediately. A regular builder must be available,
        # but the configured reserve applies to jobs that occupy a builder.
        if free is None or free == 0:
            self.manager.wall_result = dict(status='waiting_builder', reason='Chờ thợ thường')
            if getattr(self, 'paid_builder_visible', False):
                log('[Wall] Bộ đếm có Goblin Builder trả gem; chỉ tính thợ thường, không thuê bằng gem.')
            log(f'[Wall] Thợ rảnh: {free if free is not None else "chưa đọc được"}. Bỏ qua nâng tường.')
            self.wait_for_builder(free, log)
            return False
        self.manager.set_wall_wait(0, 'Có thợ thường rảnh; đang kiểm tra tường và tài nguyên.')

        selected = None
        evidence = getattr(self, '_menu_selection_evidence', None)
        if force_single and getattr(self, '_reuse_single_selection', False) and isinstance(evidence, dict):
            self._reuse_single_selection = False
            if time.monotonic() >= self._lookup_budget_started + 30:
                log('[Wall] Hết ngân sách phục hồi; hoãn lượt dự phòng.')
                return False
            fresh = self.adb.screencap()
            if (self.single_selected(fresh, evidence['level'])
                    and time.monotonic() < self._lookup_budget_started + 30):
                selected = fresh
                log('[Wall] Dùng tiếp tường đơn đã chọn cho lượt dự phòng.')
        if selected is None:
            selected = self.select_from_menu(log)
        if selected is None:
            self._wall_retryable = True
            return False
        before_level = self.selection_level(selected)
        if before_level is None or self.is_menu_open(selected):
            log('[Wall] Chưa xác minh tường và menu đã đóng; không bấm nâng.')
            self._wall_retryable = True
            return False
        self._verified_lowest_selection = True

        resource = None
        unit_cost = None
        prices = {}
        needs_price_fallback=getattr(self,'_selection_level_fallback',False) is True
        for name in choices:
            icon = self.wall_template(selected, 'goldwall' if name == 'gold' else 'elixerdwall', 0.86, roi=(200, 610, 1180, 220))
            if icon:
                value = self.amount(selected, (max(0, icon[0] - 155), icon[1] - 22, 140, 44))
                if value is None:
                    evidence=getattr(self,'_menu_selection_evidence',None) or getattr(self.manager,'_lowest_wall_target',None)
                    if isinstance(evidence,dict) and evidence.get('level')==before_level and type(evidence.get('cost')) is int:
                        value=evidence['cost']
                        needs_price_fallback=True
                        log(f'[Wall] Giá một đoạn chưa rõ; dùng giá nhóm đã xác minh {value:,}, sẽ đối chiếu giá tổng trước Upgrade.')
                if value is not None and value > 0:
                    prices[name] = value
                if resource is None and value is not None and 0 < value <= resources[name]:
                    resource, unit_cost = name, value

        if resource is None:
            if prices:
                self.manager.wall_result = dict(status='unaffordable', reason='Không đủ tài nguyên một đoạn')
                log('[Wall] Không đủ tiền nâng thêm: '+', '.join(f'{name} còn {resources[name]:,}, cần {price:,}/đoạn' for name,price in prices.items()))
            else:
                log('[Wall] Chưa đọc được giá nâng bằng tài nguyên cho phép; không bấm Upgrade.')
                self._wall_retryable = True
            self.manager.deselect_all()
            return False

        available = getattr(self, 'available_walls', 1)
        max_affordable = max(1, resources[resource] // unit_cost)
        if force_single:
            target = 1
        else:
            target = min(available, max_affordable)
            log(f'[Wall] Chế độ tối đa theo tiền: Khả dụng {available} đoạn, đủ tiền nâng {max_affordable} đoạn -> Mục tiêu {target} đoạn.')

        target = max(1, target)

        if force_single:
            target = 1
            screen = selected if self.single_selected(selected, before_level) else None
            log('[Wall] Chế độ dự phòng: chọn đúng một đoạn, bỏ qua nút +10/+1.')
        else:
            screen = self.enter_more(selected)
        if screen is None:
            log('[Wall] Chưa nhận diện được bộ chọn số đoạn.')
            self._wall_retryable = True
            self.manager.deselect_all()
            return False

        screen = screen if force_single else self.adjust_quantity(screen, target, log)
        if screen is None:
            self._wall_retryable = True
            self.manager.deselect_all()
            return False

        icon = self.wall_template(screen, 'goldwall' if resource == 'gold' else 'elixerdwall', 0.86, roi=(200, 610, 1180, 220))
        if not icon:
            self._wall_retryable = True
            return False
        self.last_quantity_screen = screen
        fixed=not force_single and self.settings.get('wall_quantity_button_mode','ocr')=='fixed'
        total = unit_cost*target if fixed else self.amount(screen, (max(0, icon[0] - 155), icon[1] - 22, 140, 44))
        observed_total,red=self.upgrade_price(screen,resource)
        if (needs_price_fallback or red is True or type(observed_total) is int and observed_total!=unit_cost*target
                or total!=unit_cost*target or total>resources[resource]):
            fitted=self.fit_upgrade_budget(screen,resource,target,unit_cost,resources[resource],log,cap=available)
            if fitted is None:
                self._wall_retryable=True
                self.manager.deselect_all()
                return False
            screen,target,total=fitted
            self.last_quantity_screen=screen
        if total != unit_cost * target or total > resources[resource]:
            log(f'[Wall] Tổng giá đọc được {total}, dự kiến {unit_cost * target}; không xác nhận.')
            self._wall_retryable = True
            self.manager.deselect_all()
            return False

        log(f'[Wall] Đã chọn {target} đoạn, tổng {total:,} {resource}.')
        confirm = self.open_checked_upgrade(resource, before_level, target, total, resources, force_single, log)
        if confirm is None:
            self._wall_retryable = True
            self.manager.deselect_all()
            return False
        confirm = self.wait_for_wall_confirmation(confirm, log)
        if confirm is None:
            if self._resource_purchase_blocked:
                self.manager.set_wall_wait(time.time()+120,'Thiếu tài nguyên sau chuỗi bấm cố định; đã thoát mua gem, bỏ lượt nâng.')
                return False
            self._wall_retryable = False
            self._wall_popup_pending = True
            self.manager.set_wall_wait(time.time()+30, 'Popup nâng tường phản hồi chậm; bỏ lượt nâng, bot kiểm tra lại giao diện.')
            return False
        self.last_confirmation = confirm

        if self.vision.wall_confirmation(confirm):
            body = ''.join(self.texts(confirm, (450, 355, 700, 110))).lower()
            normalized = re.sub(r'[^a-z0-9]', '', body)
            match = re.search(r'wallsfor(\d+)(gold|elixir)', normalized)
            button = self.vision.match_template(confirm, 'okay', 0.80, roi=(750, 450, 450, 250))
            if not match or (not fixed and int(match.group(1)) != total) or match.group(2) != resource or not button:
                log('[Wall] Giá/loại tài nguyên trong xác nhận chưa khớp; hủy.')
                self._wall_retryable = True
                self.close_dialog()
                return False
            self.manager._pending_wall_transaction = dict(level=before_level,
                before_count=self.available_walls, quantity=target, resource=resource,
                balance_before=resources[resource])
            self._transaction_submitted = True
            self.record_wall_tap('confirm', confirm, tuple(button[:2]), dict(resource=resource, quantity=target, total=total))
            log(f'[Wall] Xác nhận nâng tại ({button[0]}, {button[1]}).')
            self.adb.tap(button[0], button[1], jitter=0, delay_after=1.0)
        else:
            title = ' '.join(self.texts(confirm, (350, 25, 900, 80))).lower()
            button = self.vision.match_template(confirm, 'confirmwall', 0.85, roi=(900, 700, 400, 150))
            if 'wall' not in title or 'upgrade' not in title or not button:
                log('[Wall] Chưa xác nhận hộp thoại nâng tường.')
                self._wall_retryable = True
                self.close_dialog()
                return False
            hsv = cv2.cvtColor(confirm[770:835, 1180:1245], cv2.COLOR_BGR2HSV)
            hue, sat, val = cv2.split(hsv)
            gold = ((hue >= 15) & (hue <= 40) & (sat > 120) & (val > 100)).sum()
            elixir = ((hue >= 125) & (hue <= 175) & (sat > 100) & (val > 80)).sum()
            actual = 'gold' if gold > max(40, elixir) else ('elixir' if elixir > max(40, gold) else None)
            if actual != resource:
                log('[Wall] Loại tài nguyên trong Confirm chưa khớp; hủy.')
                self.close_dialog()
                return False
            if force_single:
                next_level = re.search(r'level\s*(\d+)', title, re.I)
                confirm_price = self.amount(confirm, (1020, 765, 170, 35))
                if not next_level or int(next_level.group(1)) != before_level + 1 or confirm_price != total:
                    log('[Wall] Hộp xác nhận một đoạn chưa khớp cấp/giá; bỏ lượt, tiếp tục bot.')
                    self.close_dialog()
                    return False
            self.manager._pending_wall_transaction = dict(level=before_level,
                before_count=self.available_walls, quantity=target, resource=resource,
                balance_before=resources[resource])
            self._transaction_submitted = True
            self.record_wall_tap('confirm', confirm, tuple(button[:2]), dict(resource=resource, quantity=target, total=total))
            log(f'[Wall] Xác nhận nâng tại ({button[0]}, {button[1]}).')
            self.adb.tap(button[0], button[1], jitter=0, delay_after=1.0)

        after = self.adb.screencap()
        if self.dismiss_resource_purchase(after,log):
            self.manager._pending_wall_transaction = None
            self.manager.set_wall_wait(time.time()+120,'Game báo thiếu tài nguyên; đã thoát mua gem, bỏ lượt nâng.')
            return False
        if self.paid_builder_dialog(after):
            self.manager._pending_wall_transaction = None
            log('[Wall] Game báo hết thợ thường và đề nghị Goblin Builder; hủy, không tiêu gem.')
            close_path = str(Path(__file__).resolve().parents[1]/'local_assets'/'goblin_builder_close.png')
            close = self.vision.match_template(after, close_path, .90, roi=(1300, 120, 200, 160))
            if close:
                self.adb.tap(close[0], close[1], jitter=0, delay_after=.4)
            self.wait_for_builder(0, log)
            return False
        success = self.verify_upgrade(after, resources, resource, total, before_level, target, unit_cost, log)
        self.manager.deselect_all()
        if success:
            self.manager._pending_wall_transaction = None
            self.last_upgrade = dict(resource=resource, quantity=target, total=total, prices=prices)
            state = getattr(self.manager, 'village_state', None)
            if (not getattr(self, '_layout_inventory_verified', False) and state is not None
                    and isinstance(getattr(state, 'wall_inventory', None), dict)):
                counts = state.wall_inventory.get('main', {})
                if isinstance(counts, dict) and before_level in counts:
                    state.record_wall_group('main', before_level, max(0, counts[before_level] - target))
                    state.record_wall_group('main', before_level + 1,
                                            counts.get(before_level + 1, 0) + target)
            cached = getattr(self.manager, '_lowest_wall_target', None)
            if isinstance(cached, dict) and cached.get('level') == before_level:
                cached['available'] = max(0, self.available_walls - target)
                if cached['available'] == 0:
                    self.manager._lowest_wall_target = None
                self.manager.save_wall_target(self.manager._lowest_wall_target)
        else:
            # The confirm may already have spent currency. Never route an
            # uncertain submitted transaction into a second purchase.
            self.manager.set_wall_wait(0, 'Giao dịch tường chưa rõ; xác minh lại qua Layout Editor ngay lượt kế tiếp.')
        return success

    def verify_upgrade(self, after, resources, resource, total, before_level, target, unit_cost, log):
        """Two exact debits plus a level or inventory change, never debit alone."""
        self._layout_inventory_verified = False
        stable_debits = 0
        stable_levels = 0
        for attempt in range(8):
            after_level = self.level(after)
            after_resources = self.vision.read_village_resources(after)
            spent = (resources[resource] - after_resources[resource]
                     if after_resources.get('valid', {}).get(resource, False) else None)
            stable_debits = stable_debits+1 if spent == total else 0
            stable_levels = stable_levels+1 if after_level == before_level+1 and spent == total else 0
            if stable_debits >= 2 and stable_levels >= 2:
                break
            if attempt < 7:
                time.sleep(.25)
                after = self.adb.screencap()
        self.last_upgrade_verification = dict(expected=total, spent=spent, before=resources,
                                             after=after_resources, before_level=before_level, after_level=after_level,
                                             selected_quantity=target, stable_debits=stable_debits,
                                             stable_levels=stable_levels)
        success = stable_debits >= 2 and stable_levels >= 2
        inventory_verified = False
        if not success and stable_debits >= 2 and isinstance(getattr(self, 'available_walls', None), int):
            remaining = self.inventory_count(unit_cost)
            inventory_verified = (remaining is not None and self.available_walls - remaining == target
                                  and self.inventory_count(unit_cost) == remaining)
            if inventory_verified:
                success = True
                log(f'[Wall] Đã xác minh nhóm tường: {self.available_walls} -> {remaining} đoạn cấp {before_level}, đã nâng {target} đoạn.')
        if not success and isinstance(getattr(self, 'available_walls', None), int):
            # A HUD reading may differ from the actual debit. Verify the wall
            # inventory immediately instead of sleeping for a fixed 2 minutes.
            layout = self.verify_walls_via_layout_editor(log)
            if isinstance(layout, dict) and isinstance(layout.get('wall_groups'), list):
                remaining = sum(group['count'] for group in layout['wall_groups']
                                if group.get('level') == before_level)
                inventory_verified = self.available_walls - remaining == target
                if inventory_verified:
                    success = True
                    self._layout_inventory_verified = True
                    log(f'[Wall] Layout Editor xác nhận cấp {before_level}: '
                        f'{self.available_walls} -> {remaining} đoạn; đã nâng {target} đoạn.')
        if success:
            if not inventory_verified:
                log(f'[Wall] Đã xác minh {target} đoạn: cấp {before_level} -> {after_level}, đã dùng {spent:,} {resource}.')
        else:
            log(f'[Wall] Chưa đủ bằng chứng nâng {target} đoạn: tiền giảm {spent}/{total}, '
                f'cấp {before_level} -> {after_level}; bỏ lượt nâng, bot tiếp tục.')
        self.last_upgrade_verification.update(inventory_verified=inventory_verified, success=success)
        if success:
            if inventory_verified and stable_debits < 2:
                log(f'[Wall] Kiểm chứng hoàn tất bằng số tường còn lại; số tiền HUD đọc được '
                    f'{resources[resource]:,} → {after_resources[resource]:,} không khớp giá {total:,}.')
            else:
                log(f'[Wall] Kiểm chứng hoàn tất: chọn {target} đoạn × {unit_cost:,}; '
                    f'{resource} {resources[resource]:,} → {after_resources[resource]:,}, '
                    f'trừ đúng {total:,} trên {stable_debits} lần đọc liên tiếp; '
                    + ('đã đối chiếu số tường còn lại.' if inventory_verified else 'đã đối chiếu cấp mới.'))
        self.save_verification()
        return success

    def paid_builder_dialog(self, screen):
        path = str(Path(__file__).resolve().parents[1]/'local_assets'/'goblin_builder_busy.png')
        return self.vision.match_template(screen, path, .90, roi=(650, 150, 900, 400))

    def run_cycle(self, screen, current_gold, current_elixir, log_callback=None):
        self._cycle_upgraded = False
        self._transaction_submitted = False
        self.manager.wall_result = dict(status='selection_failed', reason='Đang xử lý tường')
        try:
            return self._run_cycle(screen, current_gold, current_elixir, log_callback)
        except BotStoppedError:
            raise
        except Exception as exc:
            message = f'[Wall] Lỗi xử lý tường ({type(exc).__name__}: {exc}); bỏ lượt nâng, bot tiếp tục.'
            self.manager.wall_status = message.removeprefix('[Wall] ')
            (log_callback or (lambda message: None))(message)
            if getattr(self, '_transaction_submitted', False):
                self.manager.set_wall_wait(0, 'Kết quả giao dịch chưa rõ; xác minh lại qua Layout Editor ở lượt kế tiếp.')
            try:
                self.close_dialog()
                self.manager.deselect_all()
            except BotStoppedError:
                raise
            except Exception:
                pass
            return bool(getattr(self, '_cycle_upgraded', False))
        finally:
            self._selection_deadline = None
            self._lookup_budget_started = None
            status = self.manager.wall_result['status']
            if getattr(self, '_transaction_submitted', False) and not getattr(self, 'last_upgrade', None):
                status = 'transaction_pending'
            elif getattr(self, '_wall_popup_pending', False):
                status = 'transaction_pending'
            self.manager.wall_result = dict(status=status, reason=self.manager.wall_status)

    def _run_cycle(self, screen, current_gold, current_elixir, log_callback=None):
        """Finish both eligible resource budgets before returning to combat."""
        log = log_callback or (lambda message: None)
        if not self.settings.get('auto_upgrade_walls', True):
            self.manager.wall_status = 'Đã tắt tự động nâng tường.'
            self.manager.wall_result['status'] = 'disabled'
            return False
        pending = getattr(self.manager, '_pending_wall_transaction', None)
        if isinstance(pending, dict):
            layout = self.verify_walls_via_layout_editor(log)
            if not isinstance(layout, dict) or not isinstance(layout.get('wall_groups'), list):
                self.manager.wall_status = 'Chưa đọc chắc kết quả giao dịch; sẽ xác minh lại, chưa nâng trùng.'
                self.manager.wall_result['status'] = 'transaction_pending'
                return False
            remaining = sum(group['count'] for group in layout['wall_groups']
                            if group.get('level') == pending['level'])
            if pending['before_count'] - remaining == pending['quantity']:
                self.manager._pending_wall_transaction = None
                self.manager.wall_status = 'Layout Editor xác nhận giao dịch tường trước đã hoàn tất.'
                self.manager.wall_result['status'] = 'upgraded'
                log('[Wall] ' + self.manager.wall_status)
                return True
            balance = {'gold': current_gold, 'elixir': current_elixir}.get(pending['resource'])
            if remaining == pending['before_count'] and balance == pending['balance_before']:
                self.manager._pending_wall_transaction = None
                log('[Wall] Tường và tài nguyên không đổi; thử nâng lại ngay.')
            else:
                self.manager.wall_status = 'Kết quả giao dịch tường còn mơ hồ; sẽ xác minh lại, chưa nâng trùng.'
                self.manager.wall_result['status'] = 'transaction_pending'
                return False
        retry = getattr(self.manager, '_wall_builder_retry_at', 0)
        if (isinstance(retry, (int, float)) and time.time() < retry
                and self.manager.refresh_wall_builder_wait(screen) is not True):
            self.manager.wall_result['status'] = 'waiting'
            last_notice = getattr(self.manager, '_wall_builder_notice_at', 0)
            if isinstance(last_notice, (int, float)) and time.time()-last_notice >= 60:
                log('[Wall] '+self.manager.wall_status)
                self.manager._wall_builder_notice_at = time.time()
            return False
        resources = dict(gold=current_gold, elixir=current_elixir)
        allowed = self.settings.get('wall_resource', 'both')
        eligible = [name for name in resources if allowed in ('both', name)
                    and resources[name] >= int(self.settings.get('min_' + name + '_to_upgrade_wall', 3000000))]
        if not eligible:
            self.manager.wall_status = 'Chưa đạt ngưỡng tài nguyên để mở lượt nâng tường.'
            self.manager.wall_result['status'] = 'below_threshold'
            return False
        upgraded = False
        self._cycle_upgraded = False
        single_mode = False
        single_upgrades = 0
        retrying = False
        for _ in range(500):
            self.adb._guard_input()
            self.manager.wall_result = dict(status='selection_failed', reason='Đang xác minh nhóm')
            if not retrying:
                self._lookup_budget_started = time.monotonic()
            retrying = False
            before = dict(resources)
            options = dict(eligible_resources=eligible)
            if single_mode:
                options['force_single'] = True
            if not self.run(screen, resources['gold'], resources['elixir'], log, **options):
                if getattr(self, '_wall_popup_pending', False):
                    return upgraded
                if (not getattr(self, '_wall_retryable', False)
                        or getattr(self, '_transaction_submitted', False) or single_mode):
                    if single_mode:
                        log('[Wall] Chưa xác minh được lượt một đoạn; bỏ nâng lần này, tiếp tục chu trình bot.')
                    self.close_dialog()
                    self.manager.deselect_all()
                    return upgraded
                evidence = getattr(self, '_menu_selection_evidence', None)
                if (not getattr(self, '_verified_lowest_selection', False)
                        or not isinstance(evidence, dict) or type(evidence.get('level')) is not int
                        or time.monotonic() >= self._lookup_budget_started + 30):
                    self.close_dialog()
                    self.manager.deselect_all()
                    log('[Wall] Chưa xác minh nhóm hoặc hết ngân sách; hoãn nâng, không lặp toàn bộ quy trình.')
                    return upgraded
                self.close_dialog()
                single_mode = True
                self._reuse_single_selection = True
                retrying = True
                log('[Wall] Nhóm đã xác minh; thử dự phòng một đoạn trong ngân sách còn lại.')
                screen = self.adb.screencap()
                if screen is None or not self.vision.is_in_main_village(screen):
                    return upgraded
                continue
            upgraded = True
            self._cycle_upgraded = True
            self.manager.wall_result = dict(status='upgraded', reason='Đã nâng và xác minh')
            if single_mode:
                single_upgrades += 1
                if single_upgrades >= 10:
                    log('[Wall] Đã nâng dự phòng 10 đoạn; tiếp tục chiến đấu, kiểm tra lại lượt sau.')
                    return upgraded
            screen = self.adb.screencap()
            if screen is None or not self.vision.is_in_main_village(screen):
                log('[Wall] Chưa trở lại làng sau nâng; kết thúc lượt kiểm tra.')
                return upgraded
            fresh = self.vision.read_village_resources(screen)
            if not all(fresh.get('valid', {}).get(name, False) for name in eligible):
                log('[Wall] Chưa đọc đủ tài nguyên sau nâng; không lặp lại giao dịch.')
                return upgraded
            resources = {name: fresh[name] for name in resources}
            if not any(resources[name] < before[name] for name in eligible):
                log('[Wall] Tài nguyên chưa giảm; dừng để tránh lặp giao dịch.')
                return upgraded
            last = getattr(self, 'last_upgrade', None)
            prices = last.get('prices', {}) if isinstance(last, dict) else {}
            if all(name in prices and resources[name] < prices[name] for name in eligible):
                message = '[Wall] Đã dùng hết phần có thể nâng; số dư không đủ một đoạn nữa. Kết thúc lượt nâng.'
                self.manager.wall_status = message.removeprefix('[Wall] ')
                log(message)
                return upgraded
            log(f"[Wall] Tiếp tục lượt nâng: vàng {resources['gold']:,}, dầu {resources['elixir']:,}.")
        log('[Wall] Đã đạt giới hạn 500 giao dịch trong một lượt; dừng nâng.')
        return upgraded
