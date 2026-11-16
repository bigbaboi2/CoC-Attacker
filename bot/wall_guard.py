"""Capacity-aware thresholds and hysteresis for wall maintenance."""


def wall_thresholds(settings, capacities=None):
    """Clamp unreachable settings to 99.5% of verified storage capacity."""
    capacities = capacities or {}
    allowed = settings.get('wall_resource', 'both')
    result = {}
    for name in ('gold', 'elixir'):
        if allowed not in ('both', name):
            continue
        configured = int(settings.get('min_' + name + '_to_upgrade_wall', 3000000))
        if configured <= 0:
            continue
        capacity = capacities.get(name)
        capped = type(capacity) is int and capacity > 0 and configured >= capacity
        effective = max(1, capacity - max(1, capacity // 200)) if capped else configured
        result[name] = dict(configured=configured, effective=effective,
                            capacity=capacity if type(capacity) is int and capacity > 0 else None,
                            capped=capped)
    return result


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

    def observe(self, settings, readings, result, capacities=None):
        if not settings.get('auto_upgrade_walls', True) or not settings.get('wall_hold_enabled', True):
            self.clear()
            return False
        if (result.get('status') == 'maxed'
                or getattr(settings, 'walls_confirmed_maxed', False)
                or (isinstance(settings, dict) and settings.get('walls_confirmed_maxed', False))):
            self.clear()
            return False
        details = wall_thresholds(settings, capacities)
        thresholds = {name: item['effective'] for name, item in details.items()}
        self.resources.intersection_update(thresholds)
        valid = readings.get('valid', {})
        high = set()
        for name, threshold in thresholds.items():
            cap = details[name]['capacity']
            trigger = min(threshold * 1.25, cap * .995) if cap else threshold * 1.25
            if valid.get(name) is True and readings.get(name, 0) >= trigger:
                high.add(name)
        if (result.get('status') in self.FAILURES or self.active
                or any(details[name]['capped'] for name in high)):
            self.resources.update(high)
        if not self.resources:
            self.clear()
            return False
        self.active = True
        low = all(valid.get(name) is True and readings.get(name, 0) <
                  (min(thresholds[name] * 1.10, details[name]['capacity'] * .90)
                   if details[name]['capacity'] else thresholds[name] * 1.10)
                  for name in self.resources)
        self.low_reads = self.low_reads + 1 if low else 0
        if self.low_reads >= 2:
            self.clear()
        return self.active
