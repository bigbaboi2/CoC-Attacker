"""Research upgrades from the home village Army Upgrades tab."""
import re
import time
from pathlib import Path
import cv2
import numpy as np


class ArmyUpgrader:
    # Army filter uses the same fixed 1600x900 builder-menu layout.
    FILTERS = {'elixir': (670, 218), 'dark_elixir': (901, 218),
               'troops': (670, 358), 'spells': (901, 358), 'siege': (1132, 358),
               'cost_max': (670, 587), 'cost_min': (901, 587)}

    def __init__(self, worker):
        self.w = worker
        self.adb = worker.adb
        self.village = worker.village
        self.pending = None
        self.log = None

    def trace(self, message):
        if callable(self.log):
            self.log("[Quân] " + message)

    def configure_filter(self):
        w = self.w
        self.adb.tap(1216, 33, jitter=0, delay_after=.3)
        screen = self.adb.screencap()
        if not w.is_filter_popup_open(screen):
            return False
        words = w.words(screen, (500, 90, 700, 600))
        text = ' '.join(t.lower() for t, _, _ in words)
        if not ('troop' in text and 'spell' in text and 'siege' in text):
            return False
        resources = self.village.settings.get('army_upgrade_resources', {})
        types = self.village.settings.get('army_upgrade_types', {})
        priority = self.village.settings.get('army_upgrade_priority', 'suggested')
        for key, point in self.FILTERS.items():
            sorting = key.startswith('cost_')
            # No selection in a group means all values in the game menu.
            # Avoid toggling every button when the user selected the full group.
            if sorting:
                desired = priority == key
            elif key in ('elixir', 'dark_elixir'):
                desired = (not all(resources.get(k, True) for k in ('elixir', 'dark_elixir'))
                           and bool(resources.get(key, True)))
            else:
                desired = (not all(types.get(k, True) for k in ('troops', 'spells', 'siege'))
                           and bool(types.get(key, True)))
            screen = self.adb.screencap()
            active = w.is_filter_button_active(screen, point, sorting=sorting)
            if active is None:
                return False
            if active != desired:
                self.adb.tap(*point, jitter=0, delay_after=.25)
                screen = self.adb.screencap()
                if w.is_filter_button_active(screen, point, sorting=sorting) != desired:
                    return False
        self.adb.tap(1216, 33, jitter=0, delay_after=.3)
        return not w.is_filter_popup_open(self.adb.screencap())

    def rows(self, screen, words):
        result = []
        for name, x, y in words:
            if not 1230 <= x <= 1460 or not 70 < y < 770:
                continue
            if not re.search('[a-zA-Z]', name) or re.search(r'upgrade|suggest|filter', name, re.I):
                continue
            parts = []
            for token, px, py in words:
                if px > 1350 and y + 10 <= py <= y + 36:
                    norm = token.translate(str.maketrans('OoIl', '0011'))
                    if re.fullmatch(r'[\d\s,.]+', norm):
                        digits = re.sub(r'\D', '', norm)
                        if digits:
                            parts.append((px, py, digits))
            if not parts:
                continue
            cost = int(''.join(p[2] for p in sorted(parts)))
            resource = self.w.currency(screen, (1560, parts[0][1] - 16, 38, 34))
            if cost and resource in ('elixir', 'dark_elixir'):
                result.append(dict(name=name.strip(), y=y, cost=cost, resource=resource))
        return sorted(result, key=lambda r: r['y'])

    def confirm(self, screen, candidate):
        w = self.w
        words = w.words(screen, (220, 20, 1230, 850))
        text = ' '.join(t for t, _, _ in words).lower()
        if any(s in text for s in ('gems', 'goblin', 'finish now', 'buy resources')):
            return None
        title = w.normalized(' '.join(t for t, _, y in words if y < 300))
        if w.normalized(candidate['name']) not in title:
            return None
        # Require the exact price and the expected resource beside the same
        # green confirmation control; do not accept generic Upgrade buttons.
        for token, x, y in words:
            digits = re.sub(r'\D', '', token.translate(str.maketrans('OoIl', '0011')))
            if y < 500 or digits != str(candidate['cost']):
                continue
            if w.currency(screen, (max(0, x-100), max(0, y-45), 240, 90)) != candidate['resource']:
                continue
            crop = screen[max(0,y-35):min(900,y+35), max(0,x-100):min(1600,x+100)]
            import cv2
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            green = cv2.inRange(hsv, (35, 80, 70), (95, 255, 255))
            if (green > 0).mean() >= .08:
                return (x, y)
        return None

    @staticmethod
    def research_slot(screen, footer):
        """1/1 is a free regular lab; 1/2 means only the goblin is free."""
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return None
        match = re.fullmatch(r'\s*([012])\s*[/|7]\s*([12])\s*', footer)
        if not match:
            return None
        available, total = int(match[1]), int(match[2])
        if total == 1 and available in (0, 1):
            return 'available' if available == 1 else 'busy'
        if total == 2 and available in (0, 1):
            return 'goblin'
        return None

    def home_lab_status(self, screen):
        """Read the research counter next to the builder button on home HUD."""
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return None
        text = ''.join(self.w.ui.texts(screen, (1490, 670, 90, 45)))
        slot = self.research_slot(screen, text)
        if slot is not None:
            return slot
        ocr = self.w.vision._get_ocr()
        if not ocr:
            return None
        try:
            # Detection can miss this small HUD badge; direct recognition of
            # the number alone works without the text-detection stage.
            result = ocr(screen[678:708, 1502:1575], use_det=False)
            return self.research_slot(screen, ''.join(getattr(result, 'txts', []) or []))
        except Exception:
            return None

    def read_research_slot(self, screen):
        """Read the counter in progressively tighter regions, then match digits."""
        observations = []
        for roi in ((1280, 830, 110, 65), (1300, 850, 60, 30), (1265, 825, 140, 70)):
            raw = ''.join(self.w.ui.texts(screen, roi))
            observations.append(raw)
            slot = self.research_slot(screen, raw)
            if slot:
                return slot, raw
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return None, ' / '.join(observations)
        region = cv2.cvtColor(screen[843:885, 1288:1380], cv2.COLOR_BGR2GRAY)
        scores = {}
        root = Path(__file__).resolve().parents[1] / 'local_assets'
        for name, label in (('lab_slot_available.png', 'available'), ('lab_slot_goblin.png', 'goblin')):
            template = cv2.imread(str(root / name), cv2.IMREAD_GRAYSCALE)
            if template is None:
                continue
            best = 0.0
            for scale in (.7, .8, .9, 1., 1.1, 1.2, 1.3):
                needle = cv2.resize(template, None, fx=scale, fy=scale)
                if needle.shape[0] <= region.shape[0] and needle.shape[1] <= region.shape[1]:
                    best = max(best, cv2.minMaxLoc(cv2.matchTemplate(region, needle, cv2.TM_CCOEFF_NORMED))[1])
            scores[label] = best
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) == 2 and ranked[0][1] >= .88 and ranked[0][1] - ranked[1][1] >= .05:
            return ranked[0][0], f"ảnh mẫu {ranked[0][0]} ({ranked[0][1]:.2f})"
        return None, ' / '.join(observations)

    def reconcile_pending(self):
        """Verify a previously sent upgrade without ever sending it twice."""
        pending = self.pending
        if not isinstance(pending, dict):
            return dict(status='uncertain', reason='Thiếu thông tin giao dịch trước; không nâng lặp.')
        if pending.get('account') != getattr(self.village, 'current_account_key', None):
            return dict(status='uncertain', reason='Tài khoản đã thay đổi khi xác minh nâng quân; không nâng lặp.')
        self.trace(f"Đang kiểm tra lại {pending['name']} sau khi bấm nâng; chưa bấm thêm lần nào.")
        stable = 0
        last_amount = None
        for _ in range(4):
            screen = self.adb.screencap()
            if screen is None:
                break
            if self.w.ui.is_menu_open(screen):
                self.w.close_menu(screen)
                screen = self.adb.screencap()
            if screen is not None and self.village.is_in_main_village(screen):
                reading = self.w.vision.read_village_resources(screen)
                resource = pending['resource']
                amount = reading.get(resource) if reading.get('valid', {}).get(resource) is True else None
                last_amount = amount
                if amount is not None and pending['before'] - amount == pending['cost']:
                    stable += 1
                    if stable >= 2:
                        self.pending = None
                        return dict(status='progress', reason=f"Đã xác minh nâng {pending['name']}; trừ {pending['cost']:,} {resource}.")
                else:
                    stable = 0
            time.sleep(.25)
        detail = f"số dư đọc được {last_amount:,}" if last_amount is not None else 'chưa đọc được số dư làng'
        return dict(status='uncertain', reason=(
            f"Chưa xác minh nâng {pending['name']} ({pending['cost']:,} {pending['resource']}; {detail}). "
            'Sẽ kiểm tra lại ở lượt kế tiếp, không bấm nâng lại.'))

    def step(self):
        w = self.w
        w.deadline = time.monotonic() + 60
        if self.pending:
            return self.reconcile_pending()
        if not any(self.village.settings.get('army_upgrade_resources', {}).get(k, True) for k in ('elixir', 'dark_elixir')) or not any(self.village.settings.get('army_upgrade_types', {}).get(k, True) for k in ('troops', 'spells', 'siege')):
            return dict(status='idle', reason='Chưa chọn tài nguyên hoặc loại nâng quân.')
        self.trace('Đang kiểm tra làng chính và số dư trước khi mở danh sách nâng quân.')
        screen = self.adb.screencap()
        if not self.village.is_in_main_village(screen):
            return dict(status='blocked', reason='Chưa xác nhận làng chính để nâng quân.')
        wallet = w.vision.read_village_resources(screen)
        before = w.vision.read_village_resources(self.adb.screencap())
        self.trace('Đang mở menu thợ và chuyển sang Army Upgrades.')
        if not w.ui.ensure_menu_open():
            return dict(status='blocked', reason='Không mở được menu thợ.')
        try:
            self.adb.tap(1335, 850, jitter=0, delay_after=.5)
            screen = self.adb.screencap()
            heading = ' '.join(w.ui.texts(screen, (1170, 0, 430, 55))).lower()
            if 'army' not in heading or 'upgrade' not in heading:
                return dict(status='blocked', reason='Chưa xác nhận menu Army Upgrades.')
            slots = []
            for _ in range(2):
                screen = self.adb.screencap()
                slot, footer = self.read_research_slot(screen)
                slots.append(slot)
            if slots[0] is None or slots[0] != slots[1]:
                return dict(status='blocked', reason=f'Chưa đọc chắc trạng thái phòng nghiên cứu (OCR: {footer!r}).')
            if slots[0] == 'goblin':
                return dict(status='idle', lab_busy=True, reason='Phòng nghiên cứu thường đang bận; chỉ còn yêu tinh xanh, chuyển sang việc tiếp theo.')
            if slots[0] == 'busy':
                return dict(status='idle', lab_busy=True, reason='Phòng nghiên cứu đang bận; chuyển sang việc tiếp theo.')
            self.trace('Phòng nghiên cứu đang rảnh; đang áp dụng bộ lọc và tìm mục phù hợp.')
            if not self.configure_filter():
                return dict(status='blocked', reason='Chưa xác nhận được bộ lọc nâng quân.')
            previous = None
            for _ in range(12):
                screen = self.adb.screencap()
                words = w.words(screen, (1170, 50, 430, 755))
                rows = self.rows(screen, words)
                if rows:
                    candidate = rows[0]
                    resource, cost = candidate['resource'], candidate['cost']
                    self.trace(f"Đã chọn {candidate['name']}: cần {cost:,} {resource}; đang đối chiếu số dư và xác nhận.")
                    if wallet.get('valid', {}).get(resource) is not True or before.get('valid', {}).get(resource) is not True or wallet[resource] != before[resource]:
                        return dict(status='blocked', reason='Chưa đọc chắc số dư nâng quân.')
                    if wallet[resource] < cost:
                        return dict(status='needs_resources', resources=[resource], costs={resource: cost}, target=candidate,
                                    reason=f'Chờ farm {cost:,} {resource} để nâng {candidate["name"]}.')
                    fresh = self.adb.screencap()
                    if candidate not in self.rows(fresh, w.words(fresh, (1170, 50, 430, 755))):
                        return dict(status='blocked', reason='Hàng nâng quân chưa ổn định.')
                    self.adb.tap(1380, candidate['y'] + 8, jitter=0, delay_after=.6)
                    point = self.confirm(self.adb.screencap(), candidate)
                    if not point or point != self.confirm(self.adb.screencap(), candidate):
                        return dict(status='blocked', reason='Chưa xác minh giá và nút nâng quân.')
                    self.pending = dict(candidate, before=before[resource],
                                        account=getattr(self.village, 'current_account_key', None))
                    self.trace(f"Đã xác minh nút nâng {candidate['name']}; đang gửi lệnh và kiểm tra số dư sau nâng.")
                    self.adb.tap(*point, jitter=0, delay_after=1)
                    return self.reconcile_pending()
                sig = w.ui.menu_signature(words)
                if sig == previous:
                    break
                previous = sig
                w.ui.scroll_menu('down', fast=True)
            return dict(status='idle', reason='Không tìm thấy mục nâng quân phù hợp.')
        finally:
            if not self.pending:
                w.close_menu(self.adb.screencap())
