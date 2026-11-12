"""Assign the Home Village Builder's Apprentice to the first listed upgrade."""

import time

import cv2
import numpy as np

from bot.walls import WallUpgrader


class ApprenticeAssigner:
    ASSETS = 'upgrade2/ui/'
    BADGES = (920, 280, 265, 245)
    FIRST_ROW = (1140, 325, 320, 105)
    CONFIRM = (570, 615, 460, 185)
    KEEP = (390, 515, 830, 150)

    def __init__(self, village):
        self.village = village
        self.vision = village.vision
        self.adb = village.adb
        self.ui = WallUpgrader(village)
        self.next_check = 0.0

    def match(self, screen, name, threshold, roi):
        return self.vision.match_template(screen, self.ASSETS + name, threshold, roi=roi)

    def tap(self, hit):
        self.adb.tap(int(hit[0]), int(hit[1]), jitter=0, delay_after=.35)

    def wait_for(self, read):
        for _ in range(4):
            screen = self.adb.screencap()
            hit = read(screen)
            if hit is not None:
                return screen, hit
            time.sleep(.2)
        return None, None

    def ready_badge(self, screen):
        if screen is None or screen.shape[:2] != (900, 1600):
            return None
        template = self.vision.load_template(self.ASSETS + 'apprentice_face')
        if template is None:
            return None
        # Match the face without the changing village background around it.
        face = template[18:57, 5:48]
        x, y, w, h = self.BADGES
        response = cv2.matchTemplate(screen[y:y+h, x:x+w], face, cv2.TM_CCOEFF_NORMED)
        _, score, _, (left, top) = cv2.minMaxLoc(response)
        if score < .82:
            return None
        cx, cy = x + left + face.shape[1] // 2, y + top + face.shape[0] // 2
        available = self.match(screen, 'apprentice_text', .82, (cx-175, cy-29, 141, 54))
        return (cx, cy) if available else None

    def first_assignment(self, screen):
        title = ' '.join(self.ui.texts(screen, (285, 12, 1030, 125), scale=1)).lower()
        if 'builder' not in title or 'apprentice' not in title:
            return None
        return self.match(screen, 'apprentice_assign', .88, self.FIRST_ROW)

    def confirm_button(self, screen):
        return self.match(screen, 'helper_confirm', .89, self.CONFIRM)

    def keep_checkbox(self, screen):
        """Locate the checkbox by its label and verify its state against the empty asset."""
        template = self.vision.load_template(self.ASSETS + 'apprentice_keep')
        if screen is None or template is None:
            return None, None
        label_start = 87
        x, y, w, h = self.KEEP
        response = cv2.matchTemplate(screen[y:y+h, x:x+w], template[:, label_start:],
                                     cv2.TM_CCOEFF_NORMED)
        _, score, _, (left, top) = cv2.minMaxLoc(response)
        if score < .9:
            return None, None
        bx, by = x + left - label_start, y + top
        patch = screen[by+18:by+61, bx+22:bx+65]
        blank = template[18:61, 22:65]
        point = (bx + 43, by + 40)
        if patch.shape == blank.shape:
            hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
            if np.mean(cv2.inRange(hsv, (32, 85, 95), (100, 255, 255)) > 0) > .025:
                return point, True
            if np.mean(np.max(cv2.absdiff(patch, blank), axis=2) > 32) < .04:
                return point, False
        return point, None

    def run(self, screen, log):
        if (time.monotonic() < self.next_check
                or not self.village.settings.get('auto_assign_apprentice', False)
                or not self.village.is_in_main_village(screen)):
            return False
        self.next_check = time.monotonic() + 120
        if not self.ui.ensure_menu_open():
            return True
        _, badge = self.wait_for(self.ready_badge)
        if not badge:
            self.ui.close_dialog()
            return True
        self.tap(badge)
        _, first = self.wait_for(self.first_assignment)
        if first is None:
            self.ui.close_dialog()
            return True
        self.tap(first)

        screen, confirm = self.wait_for(self.confirm_button)
        if confirm:
            checkbox, checked = self.keep_checkbox(screen)
            if checked is True:
                self.tap(checkbox)
                screen = self.adb.screencap()
                confirm = self.confirm_button(screen)
                _, checked = self.keep_checkbox(screen)
            if (confirm and checked is False and self.village.settings.get('auto_assign_apprentice', False)
                    and self.village.settings.get('main_village_enabled', True)):
                # A delayed transition must never result in a second confirmation.
                self.next_check = time.monotonic() + 900
                self.tap(confirm)
                screen = self.adb.screencap()
                if screen is not None and not self.confirm_button(screen):
                    log('[Phụ tá] Đã gửi giao cho công trình đầu tiên.')
                else:
                    log('[Phụ tá] Chưa xác minh giao việc; sẽ kiểm tra lại sau.')
            elif checked is None:
                log('[Phụ tá] Chưa đọc rõ ô giữ phân công; hoãn xác nhận.')
        self.ui.close_dialog()
        # The apprentice dialog can return to the builder list after closing.
        if self.ui.is_menu_open(self.adb.screencap()):
            self.ui.close_dialog()
        return True
