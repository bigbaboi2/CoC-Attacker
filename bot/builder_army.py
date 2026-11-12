import re

import cv2
import numpy as np


COMBOS = {
    "current": ("Đội hình hiện tại trong game", ()),
    "witch_bomber": ("Phù thủy dơi + Bomber", ("night_witch", "night_witch", "bomber")),
    "pekka_bomber": ("Power P.E.K.K.A + Bomber", ("power_pekka", "power_pekka", "bomber")),
    "pekka_bomber_witch": ("Power P.E.K.K.A + Bomber + Phù thủy dơi", ("power_pekka", "bomber", "night_witch")),
    "pekka_hog": ("Power P.E.K.K.A + Trư kỵ sĩ bay", ("power_pekka", "hog_glider")),
    "pekka_hog_bomber": ("Power P.E.K.K.A + Trư kỵ sĩ bay + Bomber", ("power_pekka", "hog_glider", "bomber")),
}


REINFORCEMENT_CHOICES = {
    "current": "Giữ quân trong game",
    "power_pekka": "Power P.E.K.K.A",
    "bomber": "Bomber",
    "night_witch": "Phù thủy dơi",
    "hog_glider": "Trư kỵ sĩ bay",
    "beta_minion": "Beta Minion",
    "boxer_giant": "Boxer Giant",
    "raged_barbarian": "Raged Barbarian",
    "cannon_cart": "Cannon Cart",
    "drop_ship": "Drop Ship",
    "sneaky_archer": "Sneaky Archer",
    "baby_dragon": "Baby Dragon",
    "electrofire_wizard": "Electrofire Wizard",
}
REINFORCEMENT_KEYS = ("builder_base_reinforcement_1", "builder_base_reinforcement_2")


class BuilderArmy:
    """Edit individual camps in Choose Army, then verify the saved lineup."""

    CATALOG = {
        "bomber": (607, 615), "night_witch": (730, 743), "power_pekka": (858, 743),
        "hog_glider": (986, 615), "beta_minion": (480, 743), "boxer_giant": (480, 615),
        "raged_barbarian": (353, 615), "cannon_cart": (734, 615), "drop_ship": (860, 615),
        "sneaky_archer": (353, 743), "baby_dragon": (606, 743), "electrofire_wizard": (986, 743),
    }
    CAMPS = (367, 498, 629, 760, 891, 1022)
    REINFORCEMENTS = (1208, 1339)

    def __init__(self, battle):
        self.battle = battle
        self.base = battle.base
        self.adb = battle.adb
        self.unavailable_request = None
        self.verified_request = None
        self.required_level = None
        self.warned_locked_slots = set()

    def is_open(self, screen):
        return self.base.match(screen, ("choose_army_title",), (600, 80, 405, 110), .88)

    def lineup(self, screen):
        detected = self.battle.cards(screen, (295, 250, 1115, 166), .67)
        return {slot: kind for kind, (x, _) in detected
                for slot in self.CAMPS + self.REINFORCEMENTS if abs(slot-x) < 35}

    def slot_kind(self, screen, slot):
        cards = self.battle.cards(screen, (slot-58, 250, 116, 166), .67)
        return next((kind for kind, (x, _) in cards if abs(slot-x) < 35), None)

    def catalog_ready(self, screen, kind):
        x, y = self.CATALOG[kind]
        hsv = cv2.cvtColor(screen[y-17:y+21, x-30:x+30], cv2.COLOR_BGR2HSV)
        return np.mean(hsv[:, :, 1] > 65) > .22

    def remove_button(self, screen, slot):
        hit = self.base.match(screen, ("choose_army_remove",), (slot+6, 352, 55, 55), .80)
        if not hit:
            return None
        x, y = hit[:2]
        hsv = cv2.cvtColor(screen[y-10:y+10, x-13:x+13], cv2.COLOR_BGR2HSV)
        red = cv2.inRange(hsv, (0, 120, 110), (12, 255, 255)) | cv2.inRange(hsv, (170, 120, 110), (179, 255, 255))
        return hit if np.mean(red > 0) >= .27 else None

    def request(self):
        combo = self.base.settings.get("builder_base_combo", "current")
        combo = combo if combo in COMBOS else "current"
        return combo, tuple(self.base.settings.get(key, "current") for key in REINFORCEMENT_KEYS)

    def uses_current_lineup(self, request):
        if request != self.unavailable_request:
            self.unavailable_request = None
        if request != self.verified_request:
            self.verified_request = None
        combo, reinforcements = request
        return request in (self.unavailable_request, self.verified_request) or (
            combo == "current" and not any(role in self.CATALOG for role in reinforcements)
        )

    def barracks_requirement(self, screen):
        if self.base.match(screen, ("barracks_level_10_required",), (460, 205, 680, 105), .89):
            return 10
        text = " ".join(self.base.walls.texts(screen, (435, 220, 730, 82), scale=1))
        match = re.search(r"level\s*(\d+)\s*barracks\s*required", text, re.I)
        return int(match.group(1)) if match else None

    def replace(self, screen, slot, old, new):
        self.required_level = None
        empty = screen
        if old is not None:
            remove = self.remove_button(screen, slot)
            if not remove or old not in self.CATALOG:
                return None, "chưa xác nhận nút bỏ quân"
            self.base.tap(remove, .55)
            empty = self.adb.screencap()
            if not self.is_open(empty):
                return None, "chưa xác nhận màn hình đổi quân"
            if self.remove_button(empty, slot):
                empty = self.base.wait_for(lambda frame: self.is_open(frame) and not self.remove_button(frame, slot), 2)
                if empty is None:
                    return None, "ô quân chưa trống"
        if new is None:
            return empty, ""
        self.base.tap(self.CATALOG[new], .18)
        filled = self.adb.screencap()
        if self.is_open(filled):
            if not self.catalog_ready(empty, new):
                self.required_level = self.barracks_requirement(filled)
                if self.required_level is not None:
                    if old is not None and not self.remove_button(filled, slot):
                        self.base.tap(self.CATALOG[old], .4)
                    return None, f"game yêu cầu Builder Barracks cấp {self.required_level}"
            if self.slot_kind(filled, slot) == new:
                return filled, ""
            # The game fills the first open camp. Undo a fill in any other slot.
            for other in self.CAMPS + self.REINFORCEMENTS:
                if other != slot and not self.remove_button(empty, other):
                    added = self.remove_button(filled, other)
                    if added:
                        self.base.tap(added, .35)
                        filled = self.adb.screencap()
                        break
            if old is not None and self.is_open(filled) and not self.remove_button(filled, slot):
                if self.catalog_ready(filled, old):
                    self.base.tap(self.CATALOG[old], .4)
        return None, "game không xếp quân vào đúng ô, kiểm tra trại tiếp viện đã mở"

    @staticmethod
    def section_full(screen, x):
        hsv = cv2.cvtColor(screen[196:229, x:x+35], cv2.COLOR_BGR2HSV)
        return np.mean(cv2.inRange(hsv, (35, 75, 115), (90, 255, 255)) > 0) > .10

    def reinforcement_locked(self, editor, slot, occupied):
        if slot in occupied:
            return False
        if self.section_full(editor, 1095):
            return True
        # Locked placeholders are light even if another unlocked slot is empty.
        center = editor[275:325, slot-25:slot+25]
        return np.mean(np.max(np.abs(center.astype(np.int16) - (174, 186, 192)), axis=2) <= 8) > .9

    def check_locked_with_full_camps(self, editor, desired, lineup):
        # With both sections full the game can report a lock without changing camps.
        if not self.section_full(editor, 1095):
            return None
        for role in dict.fromkeys(desired.values()):
            if role in lineup.values():
                continue
            fresh = self.adb.screencap()
            if not self.is_open(fresh) or not self.section_full(fresh, 150) or not self.section_full(fresh, 1095):
                return None
            self.base.tap(self.CATALOG[role], .18)
            notice = self.adb.screencap()
            if self.is_open(notice):
                level = self.barracks_requirement(notice)
                if level is not None:
                    return role, level, notice
        return None

    def prepare(self, menu, combo, reinforcements, log):
        label, roles = COMBOS[combo]
        edit = self.base.match(menu, ("night_army/edit_army",), (160, 510, 650, 300), .82)
        if not edit:
            log("[BH] Chưa thấy Edit Army. Dừng tìm trận.")
            return None
        self.base.tap(edit, .5)
        editor = self.base.wait_for(self.is_open, 5)
        if editor is None:
            return None
        lineup = self.lineup(editor)
        occupied = {slot for slot in self.CAMPS + self.REINFORCEMENTS if self.remove_button(editor, slot)}
        camps = [slot for slot in self.CAMPS if slot in occupied]
        # The green Starting check verifies that there are no unfilled mandatory camps.
        starting_full = self.section_full(editor, 150)
        desired = {slot: roles[i % len(roles)] for i, slot in enumerate(camps)} if roles else {}
        for index, (slot, role) in enumerate(zip(self.REINFORCEMENTS, reinforcements), 1):
            if role not in self.CATALOG:
                self.warned_locked_slots.discard(slot)
                continue
            if self.reinforcement_locked(editor, slot, occupied):
                if slot not in self.warned_locked_slots:
                    log(f"[BH] Ô tiếp viện {index} chưa mở. Bỏ qua ô này và tiếp tục với đội hình khả dụng.")
                    self.warned_locked_slots.add(slot)
                continue
            self.warned_locked_slots.discard(slot)
            desired[slot] = role
        changed_slots = []
        unavailable = False
        if not starting_full:
            log("[BH] Game chưa xác nhận quân chính đã đầy. Dừng lượt tìm trận.")
        elif occupied != lineup.keys() or len(camps) < max(3, len(roles)):
            mismatch = [str(i) for i, slot in enumerate(self.CAMPS + self.REINFORCEMENTS, 1)
                        if (slot in occupied) != (slot in lineup)]
            detail = f" (ô {', '.join(mismatch)})" if mismatch else ""
            log(f"[BH] Game báo quân chính đã đầy, nhưng bot chưa đối chiếu đủ các thẻ{detail}.")
            return self.use_original(editor, combo, reinforcements, log)
        else:
            locked = self.check_locked_with_full_camps(editor, desired, lineup)
            if locked:
                role, level, notice = locked
                log(f"[BH] {REINFORCEMENT_CHOICES[role]} chưa mở khóa. Game yêu cầu Builder Barracks cấp {level}.")
                if self.lineup(notice) == lineup:
                    return self.use_original(notice, combo, reinforcements, log)
                log("[BH] Chưa xác nhận đội hình cũ. Dừng lượt tìm trận.")
                self.close(notice)
                return None
            for slot, role in desired.items():
                old = lineup.get(slot)
                if old == role:
                    continue
                editor = self.adb.screencap()
                if not self.is_open(editor):
                    break
                changed, reason = self.replace(editor, slot, old, role)
                if changed is None:
                    name = REINFORCEMENT_CHOICES[role]
                    place = f"tiếp viện {self.REINFORCEMENTS.index(slot)+1}" if slot in self.REINFORCEMENTS else "quân chính"
                    unavailable = self.required_level is not None
                    log(f"[BH] {place}: chưa đổi được sang {name} vì {reason}.")
                    break
                editor = changed
                changed_slots.append((slot, old, role))
            else:
                saved = self.lineup(editor)
                if all(saved.get(slot) == role for slot, role in desired.items()):
                    ready = self.reopen_battle_menu(editor)
                    if ready is not None:
                        self.verified_request = (combo, reinforcements)
                        log(f"[BH] Đã xác nhận đội hình {label} và các ô tiếp viện đã chọn.")
                    return ready
            for slot, old, role in reversed(changed_slots):
                frame = self.adb.screencap()
                if not self.is_open(frame) or self.replace(frame, slot, role, old)[0] is None:
                    break
            restored = self.adb.screencap()
            if unavailable and self.is_open(restored) and self.lineup(restored) == lineup:
                return self.use_original(restored, combo, reinforcements, log)
            log("[BH] Chưa xác nhận đội hình để tiếp tục. Dừng lượt tìm trận.")
        self.close(self.adb.screencap())
        return None

    def use_original(self, editor, combo, reinforcements, log):
        ready = self.reopen_battle_menu(editor)
        if ready is not None:
            self.unavailable_request = (combo, reinforcements)
            log("[BH] Đã xác nhận có thể dùng đội hình hiện tại. Tiếp tục tìm trận.")
        return ready

    def reopen_battle_menu(self, editor):
        self.close(editor)
        ready = self.base.wait_for(lambda frame: self.base.is_home(frame) or self.battle.find_now(frame), 5)
        if ready is not None and self.base.is_home(ready):
            return self.battle.open_menu(ready)
        return ready

    def close(self, screen):
        if self.is_open(screen):
            close = self.base.match(screen, ("choose_army_close",), (1380, 80, 125, 105), .87)
            if close:
                self.base.tap(close, .5)
