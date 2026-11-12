"""One-shot timer for actions in the current tool session."""
import time


class ToolActionTimer:
    def __init__(self):
        self.deadline = None
        self.action = None
        self.wait_for_battle = True

    def arm(self, delay_seconds, action, wait_for_battle=True):
        if delay_seconds <= 0 or action not in ('pause', 'close'):
            raise ValueError('Giờ hẹn hoặc hành động không hợp lệ.')
        self.deadline = time.monotonic()+delay_seconds
        self.action = action
        self.wait_for_battle = bool(wait_for_battle)

    def cancel(self):
        self.deadline = self.action = None

    def remaining(self):
        return None if self.deadline is None else max(0, self.deadline-time.monotonic())

    def take_due_action(self, in_battle=False):
        if self.deadline is None or time.monotonic() < self.deadline:
            return None
        if self.wait_for_battle and in_battle:
            return None
        action = self.action
        self.cancel()
        return action
