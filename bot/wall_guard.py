"""Hysteresis for wall maintenance; thresholds are not storage capacity."""


class WallFarmGuard:
    FAILURES = {'selection_failed', 'waiting_builder', 'waiting',
                'transaction_pending', 'unaffordable', 'no_target'}

    def __init__(self):
        self.active = False
        self.resources = set()
        self.low_reads = 0
        self.next_retry = 0.0

    def clear(self):
        self.active = False
        self.resources.clear()
        self.low_reads = 0
        self.next_retry = 0.0

    def observe(self, settings, readings, result):
        if not settings.get('auto_upgrade_walls', True) or not settings.get('wall_hold_enabled', True):
            self.clear()
            return False
        if (result.get('status') == 'maxed'
                or getattr(settings, 'walls_confirmed_maxed', False)
                or (isinstance(settings, dict) and settings.get('walls_confirmed_maxed', False))):
            self.clear()
            return False
        allowed = settings.get('wall_resource', 'both')
        thresholds = {name: int(settings.get('min_' + name + '_to_upgrade_wall', 3000000))
                      for name in ('gold', 'elixir') if allowed in ('both', name)}
        thresholds = {name: value for name, value in thresholds.items() if value > 0}
        self.resources.intersection_update(thresholds)
        valid = readings.get('valid', {})
        high = {name for name, threshold in thresholds.items()
                if valid.get(name) is True and readings.get(name, 0) * 100 >= threshold * 125}
        if result.get('status') in self.FAILURES or self.active:
            self.resources.update(high)
        if not self.resources:
            self.clear()
            return False
        self.active = True
        low = all(valid.get(name) is True and readings.get(name, 0) * 100 < thresholds[name] * 110
                  for name in self.resources)
        self.low_reads = self.low_reads + 1 if low else 0
        if self.low_reads >= 2:
            self.clear()
        return self.active
