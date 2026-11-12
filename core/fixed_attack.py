"""Per-device calibration and deterministic fixed deployment blocks."""
from core.ultra_attack import UltraAttackPlan


class FixedAttackPlan:
    KEY = 'fixed_deployment'
    DIRECTIONS = ('Trên', 'Phải', 'Dưới', 'Trái')
    SPELL_COUNTS = (2, 1, 2, 1)
    ROLES = ('troops', 'heroes', 'spells', 'siege')
    SETTINGS_KEY = 'fixed_deployment_profiles'

    @staticmethod
    def battlefield_point(point):
        if not isinstance(point, (tuple, list)) or len(point) != 2 or any(type(n) is not int for n in point):
            return False
        x, y = point
        return (150 <= x <= 1440 and 120 <= y <= 695
                and not (x >= 1200 and y < 240)
                and not (x < 300 and y < 250)
                and not (x >= 1290 and y >= 550)
                and not (x < 780 and y >= 610))

    @classmethod
    def valid(cls, profile):
        if not isinstance(profile, dict) or type(profile.get('zoom')) is not int or not 1 <= profile['zoom'] <= 8:
            return False
        points = set()
        for key, sizes in (('edges', (3, 3, 3, 3)), ('spells', cls.SPELL_COUNTS)):
            groups = profile.get(key)
            if not isinstance(groups, (tuple, list)) or len(groups) != 4:
                return False
            for group, size in zip(groups, sizes):
                if not isinstance(group, (tuple, list)) or len(group) != size:
                    return False
                for point in group:
                    if not cls.battlefield_point(point) or tuple(point) in points:
                        return False
                    points.add(tuple(point))
        slots = profile.get('slots')
        if not isinstance(slots, list) or not 1 <= len(slots) <= 15:
            return False
        for slot in slots:
            if not isinstance(slot, dict) or slot.get('role') not in cls.ROLES:
                return False
            x, count = slot.get('x'), slot.get('count')
            maximum = 300 if slot['role'] == 'troops' else 6 if slot['role'] == 'spells' else 1
            if type(x) is not int or not 60 <= x <= 1540 or type(count) is not int or not 1 <= count <= maximum:
                return False
        centers = sorted(slot['x'] for slot in slots)
        return (all(b-a >= 25 for a, b in zip(centers, centers[1:]))
                and any(slot['role'] == 'troops' for slot in slots)
                and sum(slot['count'] for slot in slots if slot['role'] == 'spells') == 6
                and sum(slot['role'] == 'heroes' for slot in slots) <= 4
                and sum(slot['role'] == 'siege' for slot in slots) <= 1)

    @classmethod
    def load(cls, settings, serial):
        profiles = settings.get(cls.SETTINGS_KEY, {})
        profile = profiles.get(serial) if isinstance(profiles, dict) else None
        if not profile and serial and any(f":{p}" in serial for p in range(16384, 16450)) and isinstance(profiles, dict):
            for k, v in profiles.items():
                if any(f":{p}" in k for p in range(16384, 16450)):
                    profile = v
                    break
        return profile if cls.valid(profile) else None

    @classmethod
    def cards(cls, settings, profile, serial):
        recent = UltraAttackPlan.remembered_cards(settings, serial)
        expected_heroes = sum(slot['role'] == 'heroes' for slot in profile['slots'])
        if (recent and len(recent['heroes']) >= expected_heroes
                and sum(card['count'] for card in recent['spells']) == 6):
            centers = sorted(card['x'] for role in cls.ROLES for card in recent[role])
            if len(centers) <= 15 and all(b-a >= 25 for a, b in zip(centers, centers[1:])):
                cards = {role: sorted(recent[role], key=lambda card: card['x']) for role in cls.ROLES}
                cards['heroes'] = [card for card in cards['heroes']
                                   if settings.get('use_' + card['name'], True)][:4]
                return cards, 'lần quét gần nhất'
        cards = {role: [] for role in cls.ROLES}
        for slot in sorted(profile['slots'], key=lambda slot: slot['x']):
            role = slot['role']
            cards[role].append(dict(name=f'ô {slot["x"]}', x=slot['x'], y=810, count=slot['count']))
        return cards, 'mẫu hiệu chuẩn'

    @staticmethod
    def blocks(profile, cards, *, retry=False, hero_targets=None, sent=None):
        edges = [tuple(group[i]) for i in (1, 0, 2) for group in profile['edges']]
        if retry:
            edges = edges[4:] + edges[:4]
        spells = [tuple(group[i]) for i in range(2) for group in profile['spells'] if i < len(group)]
        if retry:
            spells.reverse()

        def block(card, points):
            return dict(card=card, points=points)

        troops = cards['troops']
        opening = min(4, troops[0]['count']) if troops and not retry else 0
        blocks = [block(troops[0], edges[:opening])] if opening else []
        blocks.extend(block(card, [edges[i % 4]]) for i, card in enumerate(cards['siege']))
        for i, card in enumerate(cards['heroes']):
            point = (hero_targets or {}).get(card['x'], tuple(profile['edges'][i % 4][1]))
            blocks.append(block(card, [point]))
        used = 0
        for card in cards['spells']:
            targets = spells[used:used+card['count']]
            if targets:
                blocks.append(block(card, targets))
            used += len(targets)
        cursor = opening
        for i, card in enumerate(troops):
            count = card['count'] - (opening if i == 0 else 0)
            if count:
                targets = edges
                if retry:
                    used_points = (sent or {}).get(card['x'], set())
                    directions = [sorted(edges[direction::4], key=lambda point: point in used_points)
                                  for direction in range(4)]
                    targets = [directions[direction][i] for i in range(3) for direction in range(4)]
                blocks.append(block(card, [targets[(cursor+j) % len(targets)] for j in range(count)]))
                cursor += count
        return blocks
