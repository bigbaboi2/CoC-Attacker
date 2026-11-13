"""Read actual storage capacities and finish a farm session at its target."""
import time
import json
from pathlib import Path

class ResourceCapacityReader:
    BAR_POINTS = {'gold': (1430, 45), 'elixir': (1430, 105), 'dark_elixir': (1430, 165)}

    def __init__(self, village):
        self.village = village
        self.adb = village.adb
        self.vision = village.vision
        self.cache = {}
        self._logged_accounts = set()

    def read(self, resources, log):
        serial = self.village.settings.get('device_serial', '')
        has_account_store = isinstance(getattr(self.village, 'account_capacities', None), dict)
        screen = self.adb.screencap()
        resolver = getattr(self.village, 'check_and_sync_account', None)
        if not callable(resolver):
            resolver = getattr(self.village, 'resolve_player_tag', None)
        acc_key = resolver(screen, log) if callable(resolver) else None
        if not acc_key:
            log('[Kho] Chưa xác minh được tài khoản; tạm hoãn đọc dung lượng.')
            return None

        if has_account_store and callable(getattr(self.village, 'get_account_capacities', None)):
            entry = self.village.account_capacities.get(acc_key, {})
            recently_measured = time.time() - float(entry.get('updated_at', 0)) < 300
            saved = self.village.get_account_capacities(resources, acc_key=acc_key) if recently_measured else None
            if isinstance(saved, dict) and all(isinstance(saved.get(r), int) and saved[r] > 0 for r in resources):
                now_mono = time.monotonic()
                for r in resources:
                    self.cache[(serial, acc_key, r)] = (saved[r], now_mono)
                log_id = (serial, acc_key or 'default', tuple(resources))
                if log_id not in self._logged_accounts:
                    self._logged_accounts.add(log_id)
                    summary = ', '.join(f"{r}: {saved[r]:,}" for r in resources)
                    log(f"[Kho] Đã lấy dung lượng kho của tài khoản {acc_key or 'hiện tại'} từ bộ nhớ ({summary}).")
                return {r: saved[r] for r in resources}

        values = {}
        scanned_new = False
        for resource in resources:
            key = (serial, acc_key, resource)
            cached = self.cache.get(key)
            if cached and time.monotonic()-cached[1] < 300:
                values[resource] = cached[0]
                continue
            screen = self.adb.screencap()
            if screen is None or screen.shape[:2] != (900, 1600) or not self.village.is_in_main_village(screen):
                return None
            if has_account_store and not acc_key and callable(getattr(self.village, 'check_and_sync_account', None)):
                synced = self.village.check_and_sync_account(screen, log)
                if isinstance(synced, str) and synced:
                    acc_key = synced
                    saved = self.village.get_account_capacities(resources, acc_key=acc_key)
                    if isinstance(saved, dict) and all(isinstance(saved.get(r), int) and saved[r] > 0 for r in resources):
                        now_mono = time.monotonic()
                        for r in resources:
                            self.cache[(serial, acc_key, r)] = (saved[r], now_mono)
                        return {r: saved[r] for r in resources}
            log(f'[Kho] Đang kiểm tra dung lượng tối đa {resource} của tài khoản {acc_key}.')
            # Close an existing tooltip before opening the requested resource.
            if self.vision.read_resource_capacity_popup(screen) is not None:
                self.adb.tap(1000, 250, jitter=0, delay_after=.15)
            try:
                self.adb.tap(*self.BAR_POINTS[resource], jitter=0, delay_after=.2)
                previous = None
                capacity = None
                for _ in range(4):
                    fresh = self.adb.screencap()
                    if fresh is None or not self.village.is_in_main_village(fresh):
                        break
                    current = self.vision.read_resource_capacity_popup(fresh)
                    if current is not None and current == previous:
                        capacity = current
                        break
                    previous = current
                    time.sleep(.12)
                if capacity is None:
                    log(f'[Kho] Chưa đọc chắc Max của {resource}; sẽ kiểm tra lại, chưa kết thúc farm.')
                    return None
                self.cache[key] = (capacity, time.monotonic())
                values[resource] = capacity
                scanned_new = True
                log(f'[Kho] Dung lượng {resource}: {capacity:,}.')
            finally:
                # A neutral point outside the HUD dismisses only the tooltip.
                self.adb.tap(1000, 250, jitter=0, delay_after=.15)
        if scanned_new and has_account_store and callable(getattr(self.village, 'save_account_capacities', None)):
            self.village.save_account_capacities(values, acc_key=acc_key, source='popup_ocr')
        return values


class FarmFinishController:
    def __init__(self, village):
        self.village = village
        self.capacity_reader = ResourceCapacityReader(village)
        self.ready_reads = 0
        self.last_signature = None
        self.missing = set()
        self.status = 'Chưa bật hoàn tất farm.'
        self.completed = False
        self.next_capacity_retry = 0.0
        self.extra_resources = set()
        self.builder = None
        self.next_builder_retry = 0.0
        self.capacities = {}
        self.last_readings = {}
        self.force_full = False

    @staticmethod
    def enabled(settings):
        # Opting in explicitly confirms that all walls have been completed.
        return settings.get('farm_finish_enabled', False) is True

    def observe_home(self, settings, screen, log):
        if not self.enabled(settings):
            self.ready_reads = 0
            self.last_signature = None
            self.missing.clear()
            self.completed = False
            self.force_full = False
            self.extra_resources.clear()
            return 'off'
        if self.completed:
            return 'complete'
        mode = settings.get('farm_finish_resources', 'gold_elixir')
        resources = ('gold', 'elixir') if mode == 'gold_elixir' else (mode,)
        if any(name not in ('gold', 'elixir') for name in resources):
            self.status = 'Cấu hình tài nguyên hoàn tất farm không hợp lệ.'
            return 'wait'
        resources = tuple(dict.fromkeys(resources + tuple(sorted(self.extra_resources))))
        percent = 100 if self.force_full else max(90, min(100, int(settings.get('farm_finish_target_percent', 100))))
        signature = (settings.get('device_serial'), resources, percent)
        if signature != self.last_signature:
            self.ready_reads = 0
            self.last_signature = signature
            self.next_capacity_retry = 0
        if time.monotonic() < self.next_capacity_retry:
            return 'wait'
        capacities = self.capacity_reader.read(resources, log)
        if capacities is None:
            self.ready_reads = 0
            self.next_capacity_retry = 0
            self.status = 'Chưa đọc được Max kho; thử lại ở lượt kế tiếp.'
            return 'wait'
        self.capacities = capacities
        # Read a fresh home frame after the tooltip has been dismissed.
        fresh = self.village.adb.screencap()
        if fresh is None or not self.village.is_in_main_village(fresh):
            self.ready_reads = 0
            return 'wait'
        readings = self.village.vision.read_village_resources(fresh)
        state = getattr(self.village, 'village_state', None)
        if state is not None:
            state.update_resources('main', readings)
        self.last_readings = readings
        if not all(readings.get('valid', {}).get(name) is True for name in resources):
            self.ready_reads = 0
            self.status = 'Chưa đọc đủ số dư kho; chưa tìm trận hoặc đóng tool.'
            return 'wait'
        self.missing = {name for name in resources if readings[name]*100 < capacities[name]*percent}
        self.status = ' | '.join(f'{name}: {readings[name]:,}/{capacities[name]:,}' for name in resources)
        if self.missing:
            self.ready_reads = 0
            return 'farm'
        self.ready_reads += 1
        if self.ready_reads < 2:
            return 'wait'
        self.completed = True
        self.status = f'Đã đạt {percent}% kho qua hai lần đọc hợp lệ; hoàn tất farm.'
        log('[Hoàn tất] '+self.status)
        return 'complete'

    def suggested_upgrader(self):
        if self.builder is None:
            from bot.suggested_upgrades import SuggestedUpgrader
            self.builder = SuggestedUpgrader(self.village)
        return self.builder

    def assign_builders(self, log):
        builder = self.suggested_upgrader()
        if time.monotonic() < self.next_builder_retry:
            return 'wait'
        result = builder.step(log)
        self.status = result.get('reason', '')
        if result['status'] == 'progress':
            self.completed = False
            self.ready_reads = 0
            self.force_full = True
            return 'wait'
        if result['status'] == 'needs_resources':
            costs = result.get('costs', {})
            if costs and all(name in self.capacities and cost > self.capacities[name]
                             for name, cost in costs.items()):
                self.status = 'Giá các gợi ý vượt dung lượng kho; còn thợ thường rảnh, giữ tool mở để xử lý.'
                return 'blocked'
            self.extra_resources.update(result.get('resources', ()))
            self.force_full = True
            self.completed = False
            self.ready_reads = 0
            self.missing.clear()
            return 'farm'
        if result['status'] in ('done', 'idle'):
            from core.stats_store import StatsStore
            folder = Path(__file__).resolve().parents[1]/'supervision'/'runtime'
            try:
                folder.mkdir(parents=True, exist_ok=True)
                StatsStore._atomic_write(folder/'farm-finish-result.json', json.dumps(dict(
                    status='done', reason=self.status, capacities=self.capacities,
                    last_readings=self.last_readings, assignments=self.builder.assignments,
                    regular_builders_free=0, timestamp=time.time()), ensure_ascii=False, indent=2, default=StatsStore.json_default).encode('utf-8'))
            except (OSError, TypeError, ValueError):
                self.status = 'Đã hết thợ thường nhưng chưa lưu được báo cáo; dừng và giữ tool mở.'
                return 'blocked'
            return 'done'
        # No auto-close on an ambiguous transaction or an unrecognized UI.
        return 'blocked'
