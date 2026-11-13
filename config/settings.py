import os
import json
from typing import Dict, Any, Optional, List, Tuple

BUILDING_CATEGORY_LABELS: Tuple[Tuple[str, str], ...] = (
    ("traps", "Bẫy"),
    ("resource", "Mỏ & Kho"),
    ("defense", "Phòng thủ"),
    ("army", "Binh lính"),
    ("heroes", "Tướng"),
)

BUILDING_CATALOG: Dict[str, Tuple[Tuple[str, str, str], ...]] = {
    "traps": (
        ("bomb", "Bomb", "Bom"),
        ("springtrap", "Spring Trap", "Bẫy lò xo"),
        ("airbomb", "Air Bomb", "Bom phòng không"),
        ("giantbomb", "Giant Bomb", "Bom khổng lồ"),
        ("seekingairmine", "Seeking Air Mine", "Mìn tầm nhiệt"),
        ("skeletontrap", "Skeleton Trap", "Bẫy bộ xương"),
        ("tornadotrap", "Tornado Trap", "Bẫy lốc xoáy"),
        ("gigabomb", "Giga Bomb", "Bom Giga"),
    ),
    "resource": (
        ("goldmine", "Gold Mine", "Mỏ vàng"),
        ("elixircollector", "Elixir Collector", "Mỏ dầu hồng"),
        ("darkelixirdrill", "Dark Elixir Drill", "Mũi khoan dầu đen"),
        ("goldstorage", "Gold Storage", "Kho vàng"),
        ("elixirstorage", "Elixir Storage", "Kho dầu hồng"),
        ("darkelixirstorage", "Dark Elixir Storage", "Kho dầu đen"),
    ),
    "defense": (
        ("townhall", "Town Hall", "Nhà chính"),
        ("cannon", "Cannon", "Pháo"),
        ("archertower", "Archer Tower", "Tháp cung"),
        ("mortar", "Mortar", "Súng cối"),
        ("airdefense", "Air Defense", "Phòng không"),
        ("wizardtower", "Wizard Tower", "Tháp phù thủy"),
        ("airsweeper", "Air Sweeper", "Máy thổi khí"),
        ("hiddentesla", "Hidden Tesla", "Tháp Tesla ẩn"),
        ("bombtower", "Bomb Tower", "Tháp bom"),
        ("xbow", "X-Bow", "Nỏ X-Bow"),
        ("infernotower", "Inferno Tower", "Tháp địa ngục"),
        ("eagleartillery", "Eagle Artillery", "Pháo đại bàng"),
        ("scattershot", "Scattershot", "Máy bắn đá"),
        ("buildershut", "Builder's Hut", "Nhà thợ xây"),
        ("spelltower", "Spell Tower", "Tháp thần chú"),
        ("monolith", "Monolith", "Đá nguyên khối"),
        ("multiarchertower", "Multi-Archer Tower", "Tháp đa cung"),
        ("ricochetcannon", "Ricochet Cannon", "Pháo nảy"),
        ("multigeartower", "Multi-Gear Tower", "Tháp đa năng"),
        ("firespitter", "Firespitter", "Pháo phun lửa"),
        ("revengetower", "Revenge Tower", "Tháp báo thù"),
        ("superwizardtower", "Super Wizard Tower", "Tháp siêu pháp sư"),
        ("infernoartillery", "Inferno Artillery", "Pháo địa ngục"),
        ("longshot", "Longshot", "Longshot"),
        ("smasher", "Smasher", "Smasher"),
        ("logger", "Logger", "Logger"),
        ("craftingstation", "Crafting Station", "Trạm chế tạo"),
    ),
    "army": (
        ("armycamp", "Army Camp", "Trại lính"),
        ("barracks", "Barracks", "Nhà lính"),
        ("darkbarracks", "Dark Barracks", "Nhà lính tối"),
        ("laboratory", "Laboratory", "Phòng thí nghiệm"),
        ("spellfactory", "Spell Factory", "Nhà máy phép"),
        ("darkspellfactory", "Dark Spell Factory", "Nhà phép tối"),
        ("workshop", "Workshop", "Xưởng công thành"),
        ("clancastle", "Clan Castle", "Lâu đài bang hội"),
        ("pethouse", "Pet House", "Nhà thú nuôi"),
        ("blacksmith", "Blacksmith", "Lò rèn"),
        ("herohall", "Hero Hall", "Sảnh tướng"),
        ("helperhut", "Helper Hut", "Lều trợ lý"),
    ),
    "heroes": (
        ("barbarianking", "Barbarian King", "Vua Barbarian"),
        ("archerqueen", "Archer Queen", "Nữ hoàng Archer"),
        ("minionprince", "Minion Prince", "Hoàng tử Minion"),
        ("grandwarden", "Grand Warden", "Đại quản giáo"),
        ("royalchampion", "Royal Champion", "Nữ tướng Hoàng gia"),
        ("dragonduke", "Dragon Duke", "Bá tước Dragon"),
    ),
}

BUILDING_KEY_ALIASES: Dict[str, str] = {
    "king": "barbarianking",
    "queen": "archerqueen",
    "prince": "minionprince",
    "warden": "grandwarden",
    "champion": "royalchampion",
    "duke": "dragonduke",
    "builderhut": "buildershut",
}

BUILDING_KEY_TO_CATEGORY: Dict[str, str] = {
    key: category
    for category, items in BUILDING_CATALOG.items()
    for key, _, _ in items
}
for _alias, _canonical in BUILDING_KEY_ALIASES.items():
    if _canonical in BUILDING_KEY_TO_CATEGORY:
        BUILDING_KEY_TO_CATEGORY[_alias] = BUILDING_KEY_TO_CATEGORY[_canonical]

BUILDING_KEY_TO_RESOURCES: Dict[str, Tuple[str, ...]] = {
    # Traps: only Gold
    "bomb": ("gold",),
    "springtrap": ("gold",),
    "airbomb": ("gold",),
    "giantbomb": ("gold",),
    "seekingairmine": ("gold",),
    "skeletontrap": ("gold",),
    "tornadotrap": ("gold",),
    "gigabomb": ("gold",),
    # Resource: Elixir collector/storage use Gold; Gold mine/storage and Dark drill/storage use Elixir
    "goldmine": ("elixir",),
    "goldstorage": ("elixir",),
    "elixircollector": ("gold",),
    "elixirstorage": ("gold",),
    "darkelixirdrill": ("elixir",),
    "darkelixirstorage": ("elixir",),
    # Defense: Monolith uses Dark Elixir; all others use Gold
    "monolith": ("dark_elixir",),
    "townhall": ("gold",),
    "cannon": ("gold",),
    "archertower": ("gold",),
    "mortar": ("gold",),
    "airdefense": ("gold",),
    "wizardtower": ("gold",),
    "airsweeper": ("gold",),
    "hiddentesla": ("gold",),
    "bombtower": ("gold",),
    "xbow": ("gold",),
    "infernotower": ("gold",),
    "eagleartillery": ("gold",),
    "scattershot": ("gold",),
    "buildershut": ("gold",),
    "spelltower": ("gold",),
    "multiarchertower": ("gold",),
    "ricochetcannon": ("gold",),
    "multigeartower": ("gold",),
    "firespitter": ("gold",),
    "revengetower": ("gold",),
    "superwizardtower": ("gold",),
    "infernoartillery": ("gold",),
    "longshot": ("gold",),
    "smasher": ("gold",),
    "logger": ("gold",),
    "craftingstation": ("elixir", "gold"),
    # Army: Clan Castle and Blacksmith use Gold; others use Elixir
    "clancastle": ("gold",),
    "blacksmith": ("gold",),
    "armycamp": ("elixir",),
    "barracks": ("elixir",),
    "darkbarracks": ("elixir",),
    "laboratory": ("elixir",),
    "spellfactory": ("elixir",),
    "darkspellfactory": ("elixir",),
    "workshop": ("elixir",),
    "pethouse": ("elixir",),
    "herohall": ("elixir",),
    "helperhut": ("elixir",),
    # Heroes: Grand Warden uses Elixir; others use Dark Elixir
    "grandwarden": ("elixir",),
    "barbarianking": ("dark_elixir",),
    "archerqueen": ("dark_elixir",),
    "minionprince": ("dark_elixir",),
    "royalchampion": ("dark_elixir",),
    "dragonduke": ("dark_elixir",),
}
for _alias, _canonical in BUILDING_KEY_ALIASES.items():
    if _canonical in BUILDING_KEY_TO_RESOURCES:
        BUILDING_KEY_TO_RESOURCES[_alias] = BUILDING_KEY_TO_RESOURCES[_canonical]

CATEGORY_TO_RESOURCES: Dict[str, Tuple[str, ...]] = {
    "traps": ("gold",),
    "resource": ("gold", "elixir"),
    "defense": ("gold", "elixir", "dark_elixir"),
    "army": ("gold", "elixir"),
    "heroes": ("elixir", "dark_elixir"),
}

DEFAULT_BUILDING_WHITELIST: Dict[str, bool] = {
    key: True
    for items in BUILDING_CATALOG.values()
    for key, _, _ in items
}

DEFAULT_CONFIG: Dict[str, Any] = {
    # Device connection
    "device_serial": "127.0.0.1:5555",
    "auto_reconnect": True,
    "auto_restart_game": True,
    "periodic_restart_enabled": True,
    "periodic_restart_interval_mins": 60,

    # Farm loot thresholds
    "min_gold": 600000,
    "min_elixir": 600000,
    "min_dark_elixir": 4000,
    "loot_filter_mode": "legacy",  # legacy, all, any
    "loot_filter_gold": True,
    "loot_filter_elixir": True,
    "loot_filter_dark_elixir": True,
    "max_search_skips": 50,
    "trophy_drop_mode": False,
    "max_trophies": 3200,

    # Hero usage
    "use_king": True,
    "use_queen": True,
    "use_warden": True,
    "use_champion": True,
    "use_prince": True,
    "use_luke": True,
    "trigger_hero_abilities": True,
    "hero_ability_delay_sec": 5,

    # Siege machine usage
    "use_siege": True,
    "siege_deploy_mode": "always",  # "always", "require_cc"

    # Troop attack strategy
    "attack_strategy": "perimeter",  # perimeter, four_corners, smart_wave, hybrid, resource_dragons, ultra_fast, random_mix
    "attack_spread_points": 16,
    "fixed_deployment_profiles": {},
    "ultra_fixed_positions": False,
    "ultra_main_taps": 17,
    "ultra_support_taps": 3,
    "ultra_drop_gap_ms": 20,
    "ultra_card_select_ms": 80,
    "ultra_spell_inward_pct": 20,
    "ultra_spell_max_lead_px": 40,
    "troop_drop_delay_ms": 80,
    "spell_first_delay_sec": 1,
    "spell_wave_gap_sec": 2,
    "star_target": 1,
    "end_star_source": "all",
    "battle_timeout_sec": 120,
    "auto_surrender": True,
    "end_on_stars": True,
    "end_on_time": True,
    "end_on_loot": False,
    "loot_target_percent": 70,
    "loot_target_resource": "gold_elixir",
    "end_on_destruction": False,
    "destruction_target_percent": 50,
    "end_condition_mode": "any",
    "resource_attack_fallback": "perimeter",
    "auto_collect_treasury": False,
    "treasury_star_bonus_enabled": True,
    "treasury_war_rewards_enabled": True,
    "treasury_check_interval_min": 30,
    "surrender_at_stars": 1,

    # Village automation
    "main_village_enabled": True,
    "auto_collect": True,
    "collect_interval_min": 60,
    "auto_train_army": True,
    "quick_train_slot": 1,  # 1, 2, or 3
    "auto_request_troops": False,
    "auto_assign_apprentice": False,
    "auto_upgrade_walls": True,
    "wall_hold_enabled": True,
    "farm_finish_enabled": False,
    "farm_finish_target_percent": 100,
    "farm_finish_resources": "gold_elixir",
    "farm_finish_close_app": True,
    "farm_finish_wait_builders": True,
    "wall_resource": "both",  # 'gold', 'elixir', 'both'
    "min_gold_to_upgrade_wall": 3000000,
    "min_elixir_to_upgrade_wall": 3000000,
    "reserve_builders": 0,
    "auto_upgrade_buildings": False,
    "auto_buy_new_buildings": False,
    "auto_upgrade_army": False,
    "army_upgrade_resources": {"elixir": True, "dark_elixir": True},
    "army_upgrade_types": {"troops": True, "spells": True, "siege": True},
    "army_upgrade_priority": "suggested",
    "lock_townhall_upgrade": True,
    "building_upgrade_specific_target": "",
    "building_upgrade_wait_mode": "target_cost",  # 'target_cost', 'full_storage'
    "building_upgrade_use_menu_filter": True,
    "building_upgrade_priority": "time_min",  # 'time_min', 'cost_min', 'cost_max', 'time_max', 'suggested', 'specific'
    "building_upgrade_resources": {
        "gold": True,
        "elixir": True,
        "dark_elixir": True,
    },
    "building_upgrade_categories": {
        "traps": True,
        "resource": True,
        "defense": True,
        "army": True,
        "heroes": True,
    },
    "building_upgrade_whitelist": dict(DEFAULT_BUILDING_WHITELIST),
    "wall_max_upgrades_per_cycle": 5,
    "wall_upgrade_max_by_money": False,
    "wall_quantity_button_mode": "ocr",  # ocr (existing recognition), fixed
    "wall_quantity_fixed_gap_ms": 120,
    "maintenance_interval_sec": 45,

    "live_preview_enabled": True,

    # Builder base (Night village)
    "builder_base_enabled": False,
    "builder_base_interval_mins": 30,
    "builder_base_combo": "current",
    "builder_base_reinforcement_1": "current",
    "builder_base_reinforcement_2": "current",
    "builder_base_obstacle_attack": False,
    "builder_base_collect_gem": False,
    "builder_base_collect_cart": False,
    "builder_base_collect_mine": False,
    "builder_base_upgrade_walls": False,
    "builder_base_wall_lowest_first": False,
    "builder_base_wall_trigger": "either",
    "builder_base_wall_min_gold": 0,
    "builder_base_wall_min_elixir": 0,
    "builder_base_pause_on_no_upgrades": False,
    "builder_base_upgrade_buildings": False,
    "builder_base_reserve_builders": 0,
    "builder_base_building_priority": "suggested",
    "builder_base_building_resources": {"gold": True, "elixir": True},

    # Anti-ban & Humanizer
    "coordinate_jitter": 4,
    "human_pause_min_sec": 0.5,
    "human_pause_max_sec": 1.2
}

class BotSettings:
    """Manages application settings with JSON persistence."""

    def __init__(self, filepath: Optional[str] = None):
        if filepath is None:
            self.filepath = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
            )
        else:
            self.filepath = filepath
        self.data: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    if "main_village_enabled" not in saved:
                        saved["main_village_enabled"] = not (
                            saved.get("builder_base_enabled", False) and saved.get("builder_base_only", False)
                        )
                    saved.pop("builder_base_only", None)
                    saved.pop("builder_base_battle", None)
                    saved.pop("wall_verify_layout_editor", None)
                    self.data.update(saved)
            except Exception as e:
                print(f"[Settings] Warning loading settings: {e}")

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                def _json_default(o):
                    if hasattr(o, 'tolist'): return o.tolist()
                    if hasattr(o, 'item'): return o.item()
                    return str(o)
                json.dump(self.data, f, indent=4, ensure_ascii=False, default=_json_default)
        except Exception as e:
            print(f"[Settings] Error saving settings: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any):
        self.data[key] = value
        self.save()
