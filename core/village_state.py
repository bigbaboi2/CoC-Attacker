import json
import time
from pathlib import Path
from typing import Dict, Any, Optional

STRATEGY_NAMES = {
    'fixed_deployment': 'Rải quân cố định',
    'perimeter': 'Vòng quanh mép đỏ',
    'four_corners': 'Tấn công 4 góc',
    'smart_wave': 'Từng đợt thông minh',
    'hybrid': 'Chiến thuật hỗn hợp',
    'resource_dragons': 'Tập trung mỏ tài nguyên',
    'ultra_fast': 'Siêu tốc (Ultra-Attack)',
    'random_mix': 'Ngẫu nhiên xoay vòng'
}

BUILDER_COMBO_NAMES = {
    'pekka_bomber': 'P.E.K.K.A + Bomber',
    'baby_dragon': 'Baby Dragon Rỉa Nhà',
    'witch_spam': 'Phù Thủy Đêm (Night Witch)',
    'giant_cannon': 'Boxer Giant + Cannon Cart',
    'barbarian_archer': 'Barbarian + Sneaky Archer',
    'custom': 'Tùy chỉnh'
}

class VillageStateManager:
    """
    Central real-time state repository for active village operations.
    Maintains detected resources, wall distribution, active attack strategies,
    and tool settings. Automatically persists wall inventory across restarts.
    """

    def __init__(self, storage_path: Optional[str] = None):
        self.storage_path = Path(storage_path) if storage_path else Path("village_live_state.json")
        self.account_key = None
        self.account_caches = {}
        self.active_village: str = "main"  # 'main' or 'builder'

        self.home_resources: Dict[str, Any] = {
            'gold': 0,
            'elixir': 0,
            'dark_elixir': 0,
            'gems': 0,
            'updated_at': 0.0,
            'valid': {}
        }

        self.builder_resources: Dict[str, Any] = {
            'gold': 0,
            'elixir': 0,
            'gems': 0,
            'updated_at': 0.0,
            'valid': {}
        }

        # Wall distribution: {level: count}
        self.wall_inventory: Dict[str, Dict[int, int]] = {
            'main': {},
            'builder': {}
        }

        self.walls_confirmed_maxed: bool = False
        self.th_level: Optional[int] = None
        self.last_wall_status: str = "Chưa kiểm tra nâng tường."

        self.current_attack: Dict[str, Dict[str, Any]] = {
            'main': {
                'strategy': 'perimeter',
                'strategy_label': 'Vòng quanh mép đỏ',
                'target_stars': 1,
                'spread_points': 17,
                'updated_at': 0.0
            },
            'builder': {
                'combo': 'pekka_bomber',
                'combo_label': 'P.E.K.K.A + Bomber',
                'stage2_skip': False,
                'reinforcements': [],
                'updated_at': 0.0
            }
        }

        self.live_battle = {'main': {}, 'builder': {}}
        self.free_builders = None
        self.total_builders: int = 6
        self.builder_plan_summary: str = ""
        self.ongoing_upgrades: list = []
        self._load_cache()
        # Both villages display the same wallet. Older caches saved two copies.
        known_wallets = [entry for entry in (self.home_resources, self.builder_resources)
                         if entry['valid'].get('gems')]
        if known_wallets:
            latest = max(known_wallets, key=lambda entry: entry['updated_at'])
            for entry in (self.home_resources, self.builder_resources):
                entry['gems'] = latest['gems']
                entry['valid']['gems'] = True

    def set_active_village(self, village_type: str):
        if village_type in ('main', 'builder'):
            self.active_village = village_type

    def _account_snapshot(self):
        return dict(wall_inventory=self.wall_inventory,
                    home_resources=self.home_resources,
                    builder_resources=self.builder_resources,
                    walls_confirmed_maxed=self.walls_confirmed_maxed,
                    th_level=self.th_level)

    def set_account(self, key):
        if not key or key == self.account_key:
            return
        if self.account_key:
            self.account_caches[self.account_key] = json.loads(json.dumps(self._account_snapshot()))
        self.account_key = key
        saved = self.account_caches.get(key, {})
        self.wall_inventory = {'main': {}, 'builder': {}}
        for village in ('main', 'builder'):
            for level, count in saved.get('wall_inventory', {}).get(village, {}).items():
                try:
                    self.wall_inventory[village][int(level)] = int(count)
                except (TypeError, ValueError):
                    pass
        self.home_resources = dict(gold=0, elixir=0, dark_elixir=0, gems=0, updated_at=0.0, valid={})
        self.builder_resources = dict(gold=0, elixir=0, gems=0, updated_at=0.0, valid={})
        for name in ('home_resources', 'builder_resources'):
            target = getattr(self, name)
            source = saved.get(name, {})
            for resource in ('gold', 'elixir', 'dark_elixir', 'gems'):
                if type(source.get(resource)) is int and source.get('valid', {}).get(resource):
                    target[resource] = source[resource]
                    target['valid'][resource] = True
            target['updated_at'] = source.get('updated_at', 0.0)
        wallets = [entry for entry in (self.home_resources, self.builder_resources)
                   if entry['valid'].get('gems')]
        if wallets:
            latest = max(wallets, key=lambda entry: entry['updated_at'])
            for entry in (self.home_resources, self.builder_resources):
                entry['gems'] = latest['gems']
                entry['valid']['gems'] = True
        self.walls_confirmed_maxed = bool(saved.get('walls_confirmed_maxed', False))
        self.th_level = saved.get('th_level')
        self.ongoing_upgrades = []
        self.free_builders = None

    def merge_account_cache(self, legacy, tag):
        if self.account_key == legacy:
            self.account_caches[legacy] = json.loads(json.dumps(self._account_snapshot()))
        old = self.account_caches.get(legacy)
        new = self.account_caches.get(tag)
        if not isinstance(old, dict):
            return True
        def comparable(snapshot):
            values = json.loads(json.dumps(snapshot))
            for name in ('home_resources', 'builder_resources'):
                values.get(name, {}).pop('updated_at', None)
            return values
        if isinstance(new, dict) and comparable(old) != comparable(new):
            return False
        self.account_caches.setdefault(tag, old)
        del self.account_caches[legacy]
        if self.account_key == legacy:
            self.account_key = tag
        return True

    def update_resources(self, village_type: str, resources: Dict[str, Any]):
        if village_type not in ('main', 'builder') or not isinstance(resources, dict):
            return False
        target = self.home_resources if village_type == 'main' else self.builder_resources
        valid = resources.get('valid', {})
        if not isinstance(valid, dict):
            return False
        updated = False
        for key in ('gold', 'elixir', 'dark_elixir', 'gems'):
            value = resources.get(key)
            if key not in target or not valid.get(key, True) or type(value) is not int or value < 0:
                continue
            target[key] = value
            target['valid'][key] = True
            if key == 'gems':
                other = self.builder_resources if village_type == 'main' else self.home_resources
                other['gems'] = value
                other['valid']['gems'] = True
            updated = True
        if updated:
            target['updated_at'] = time.time()
            self._save_cache()
        return updated

    def start_battle(self, village_type, started_at, *, stage=None, recovered=False):
        self.live_battle[village_type] = {
            'started_at': started_at, 'stars': 0, 'stage': stage, 'recovered': recovered
        }

    def update_battle(self, village_type, stars, *, stage=None):
        battle = self.live_battle[village_type]
        if battle:
            self.live_battle[village_type] = dict(battle, stars=max(stars, battle['stars']), stage=stage)

    def clear_battle(self, village_type):
        self.live_battle[village_type] = {}

    def record_wall_group(self, village_type: str, level: int, count: int):
        """Naturally updates wall count for a given level when observed in-game."""
        if village_type not in ('main', 'builder') or not isinstance(level, int) or level < 1:
            return
        if not isinstance(count, int) or count < 0:
            return
        self.wall_inventory[village_type][level] = count
        self._save_cache()

    def record_walls_batch(self, village_type: str, items: Dict[int, int]):
        """Batch update detected wall levels and their counts."""
        if village_type not in ('main', 'builder') or not isinstance(items, dict):
            return
        updated = False
        for lvl, cnt in items.items():
            if isinstance(lvl, int) and lvl >= 1 and isinstance(cnt, int) and cnt >= 0:
                self.wall_inventory[village_type][lvl] = cnt
                updated = True
        if updated:
            self._save_cache()

    def replace_wall_inventory(self, village_type: str, items: Dict[int, int]):
        """Replace complete wall inventory with verified authoritative counts (e.g. from Layout Editor)."""
        if village_type not in ('main', 'builder') or not isinstance(items, dict):
            return
        cleaned = {}
        for lvl, cnt in items.items():
            if isinstance(lvl, int) and lvl >= 1 and isinstance(cnt, int) and cnt >= 0:
                cleaned[lvl] = cnt
        self.wall_inventory[village_type] = cleaned
        self._save_cache()

    def record_walls_maxed(self, maxed: bool = True, th_level: Optional[int] = None):
        self.walls_confirmed_maxed = bool(maxed)
        if th_level is not None:
            self.th_level = th_level
        self._save_cache()

    def set_wall_status(self, status_msg: str):
        if isinstance(status_msg, str):
            self.last_wall_status = status_msg

    def set_builders(self, free: int, total: int = 6):
        self.free_builders = free
        if isinstance(total, int) and total >= 1:
            self.total_builders = total

    def set_ongoing_upgrades(self, jobs: list):
        self.ongoing_upgrades = list(jobs) if isinstance(jobs, list) else []

    def set_builder_plan(self, summary: str):
        self.builder_plan_summary = str(summary or "")

    def update_attack_strategy(self, village_type: str, data: Dict[str, Any]):
        if village_type not in ('main', 'builder') or not isinstance(data, dict):
            return
        entry = self.current_attack[village_type]
        entry.update(data)
        if village_type == 'main' and 'strategy' in data:
            entry['strategy_label'] = STRATEGY_NAMES.get(data['strategy'], data['strategy'])
        elif village_type == 'builder' and 'combo' in data:
            entry['combo_label'] = BUILDER_COMBO_NAMES.get(data['combo'], data['combo'])
        entry['updated_at'] = time.time()

    def get_snapshot(self) -> Dict[str, Any]:
        return {
            'active_village': self.active_village,
            'home_resources': dict(self.home_resources, valid=dict(self.home_resources['valid'])),
            'builder_resources': dict(self.builder_resources, valid=dict(self.builder_resources['valid'])),
            'wall_inventory': {
                'main': dict(self.wall_inventory['main']),
                'builder': dict(self.wall_inventory['builder'])
            },
            'walls_confirmed_maxed': getattr(self, 'walls_confirmed_maxed', False),
            'th_level': getattr(self, 'th_level', None),
            'live_battle': dict(self.live_battle),
            'free_builders': self.free_builders,
            'total_builders': getattr(self, 'total_builders', 6),
            'builder_plan_summary': self.builder_plan_summary,
            'ongoing_upgrades': list(getattr(self, 'ongoing_upgrades', [])),
            'last_wall_status': self.last_wall_status,
            'current_attack': {
                'main': dict(self.current_attack['main']),
                'builder': dict(self.current_attack['builder'])
            }
        }

    def _save_cache(self):
        try:
            if not self.account_key:
                return
            self.account_caches[self.account_key] = self._account_snapshot()
            data = {'accounts': self.account_caches}
            from core.stats_store import StatsStore
            self.storage_path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=StatsStore.json_default), encoding='utf-8')
        except Exception:
            pass

    def _load_cache(self):
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding='utf-8'))
            if isinstance(data.get('accounts'), dict):
                self.account_caches = data['accounts']
                return
            # Old live-state files had no account key. Do not display their
            # wall or resource values before a player identity is established.
            return
        except Exception:
            pass
