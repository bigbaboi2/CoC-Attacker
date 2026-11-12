"""End-of-battle targets, independent of OCR and ADB input."""
from dataclasses import dataclass


@dataclass
class BattleEndPolicy:
    enabled: bool = True
    stars_enabled: bool = True
    stars_target: int = 1
    star_source: str = 'all'
    town_hall_destroyed: bool = False
    town_hall_streak: int = 0
    damage_percent: object = None
    damage_star_streak: int = 0
    damage_star_tier: int = 0
    time_enabled: bool = True
    time_limit: int = 120
    loot_enabled: bool = False
    loot_target: int = 70
    loot_resource: str = 'gold_elixir'
    destruction_enabled: bool = False
    destruction_target: int = 50
    mode: str = 'any'
    loot_streak: int = 0
    destruction_streak: int = 0

    @classmethod
    def from_settings(cls, settings):
        return cls(enabled=bool(settings.get('auto_surrender',True)),
                   stars_enabled=bool(settings.get('end_on_stars',True)),
                   stars_target=max(1,min(3,int(settings.get('star_target',1)))),
                   star_source=settings.get('end_star_source','all'),
                   time_enabled=bool(settings.get('end_on_time',True)),
                   time_limit=max(10,int(settings.get('battle_timeout_sec',120))),
                   loot_enabled=bool(settings.get('end_on_loot',False)),
                   loot_target=max(1,min(100,int(settings.get('loot_target_percent',70)))),
                   loot_resource=settings.get('loot_target_resource','gold_elixir'),
                   destruction_enabled=bool(settings.get('end_on_destruction',False)),
                   destruction_target=max(1,min(100,int(settings.get('destruction_target_percent',50)))),
                   mode=settings.get('end_condition_mode','any'))

    def loot_percent(self, initial, remaining):
        if not initial or not remaining:
            return None
        choices = {'gold_elixir':('gold','elixir'),'gold':('gold',),'elixir':('elixir',),
                   'dark_elixir':('dark_elixir',),'all':('gold','elixir','dark_elixir')}
        keys = choices.get(self.loot_resource, choices['gold_elixir'])
        before = after = 0
        for key in keys:
            if not initial.get('known',{}).get(key,True) or not remaining.get('known',{}).get(key,False):
                return None
            start, left = initial.get(key), remaining.get(key)
            if type(start) is not int or type(left) is not int or not 0 <= left <= start:
                return None
            before += start
            after += left
        return (before-after)*100/before if before > 0 else None

    def observe_loot(self, percent):
        self.loot_streak = self.loot_streak+1 if percent is not None and percent >= self.loot_target else 0

    def observe_destruction(self, percent):
        self.destruction_streak = (self.destruction_streak+1
                                   if percent is not None and percent >= self.destruction_target else 0)

    def observe_stars(self, stars, percent):
        self.damage_percent = percent if type(percent) is int and 0 <= percent <= 100 else None
        tier = (int(percent >= 50) + int(percent == 100)) if self.damage_percent is not None else 0
        self.damage_star_streak = self.damage_star_streak + 1 if tier == self.damage_star_tier and tier else 0
        if tier and tier != self.damage_star_tier:
            self.damage_star_streak = 1
        self.damage_star_tier = tier
        # Below 50%, any earned star is the Town Hall. At 50..99%, two
        # stars prove both milestones. Require two consistent HUD samples.
        certain = (self.damage_percent is not None and stars >= 1 and
                   (self.damage_percent < 50 or stars >= 2))
        self.town_hall_streak = self.town_hall_streak + 1 if certain else 0
        if self.town_hall_streak >= 2:
            self.town_hall_destroyed = True

    def eligible_stars(self, stars):
        if self.star_source == 'town_hall':
            return stars if self.town_hall_destroyed else 0
        if self.star_source == 'destruction':
            # These milestones do not require guessing which star came first.
            percent = self.damage_percent
            return min(stars,self.damage_star_tier) if percent is not None and self.damage_star_streak >= 2 else 0
        return stars

    def reason(self, stars, elapsed):
        if not self.enabled:
            return None
        if self.time_enabled and elapsed >= self.time_limit:
            return 'timeout'
        goals = {}
        if self.stars_enabled:
            goals['star_target'] = self.eligible_stars(stars) >= self.stars_target
        if self.loot_enabled:
            goals['loot_target'] = self.loot_streak >= 2
        if self.destruction_enabled:
            goals['destruction_target'] = self.destruction_streak >= 2
        if not goals:
            return None
        if self.mode == 'all':
            return 'combined_targets' if all(goals.values()) else None
        return next((name for name, met in goals.items() if met),None)
