"""Recognize deck portraits and read count badges without inventing missing cards."""
import os
import re
import cv2

class DeckRecognizer:
    HEROES = ('king', 'queen', 'warden', 'champion', 'prince', 'luke')
    SIEGE = {'battle_bimp', 'battle_drill', 'flame_flinger', 'log_launcher',
             'siege_barracks', 'sky_wagon', 'stone_slammer', 'wall_wrecker', 'troop_launcher'}

    def __init__(self, vision):
        self.vision = vision
        self.templates = None

    def _templates(self):
        if self.templates is not None:
            return self.templates
        self.templates = []
        for hero in self.HEROES:
            path = 'royal.png' if hero == 'champion' else hero + '.png'
            template = self.vision.load_template(path)
            if template is not None:
                tmpl_g = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) if len(template.shape) == 3 else template
                self.templates.append(('heroes', hero, template, tmpl_g))
        folder = os.path.join(self.vision.asset_dir, 'deploy-troop')
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder)):
                if not name.endswith('.png'):
                    continue
                identity = name.removeprefix('use_main_use_').removesuffix('.png')
                kind = 'siege' if identity in self.SIEGE else ('spells' if 'spell' in identity or identity == 'no' else 'troops')
                template = self.vision.load_template('deploy-troop/' + name)
                if template is not None:
                    tmpl_g = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) if len(template.shape) == 3 else template
                    self.templates.append((kind, identity, template, tmpl_g))
        return self.templates

    def _is_empty_siege(self, screen, x):
        """Check if the siege card at x displays the empty CC warning exclamation badge."""
        if screen is None:
            return False
        badge_tmpl = self.vision.load_template('siege_empty_exclamation.png')
        if badge_tmpl is None:
            return False
        h, w = badge_tmpl.shape[:2]
        x1 = max(0, x - 55)
        x2 = min(screen.shape[1], x + 55)
        card_roi = screen[740:850, x1:x2]
        if card_roi.shape[0] < h or card_roi.shape[1] < w:
            return False
        res = cv2.matchTemplate(card_roi, badge_tmpl, cv2.TM_CCOEFF_NORMED)
        _, max_v, _, _ = cv2.minMaxLoc(res)
        return max_v >= 0.70

    def _find_siege_by_switch_or_badge(self, screen, existing_cards):
        """Find siege machine if clean portrait match failed due to empty CC badge or overlay."""
        if screen is None or screen.shape[:2] != (900, 1600):
            return None
        badge_tmpl = self.vision.load_template('siege_empty_exclamation.png')
        if badge_tmpl is not None:
            roi = screen[740:850, 60:1540]
            if roi.shape[0] >= badge_tmpl.shape[0] and roi.shape[1] >= badge_tmpl.shape[1]:
                res = cv2.matchTemplate(roi, badge_tmpl, cv2.TM_CCOEFF_NORMED)
                _, max_v, _, loc = cv2.minMaxLoc(res)
                if max_v >= 0.70:
                    card_x = loc[0] + 60 + badge_tmpl.shape[1] // 2
                    if not any(abs(card_x - c['x']) < 50 for c in existing_cards):
                        return dict(name='siege_machine', label='siege_machine',
                                    x=card_x, y=810, confidence=float(max_v), count=1, has_cc=False)

        btn_tmpl = self.vision.load_template('siege_switch_btn.png')
        if btn_tmpl is not None:
            roi_btn = screen[820:890, 60:1540]
            if roi_btn.shape[0] >= btn_tmpl.shape[0] and roi_btn.shape[1] >= btn_tmpl.shape[1]:
                res_btn = cv2.matchTemplate(roi_btn, btn_tmpl, cv2.TM_CCOEFF_NORMED)
                _, max_btn, _, loc_btn = cv2.minMaxLoc(res_btn)
                if max_btn >= 0.72:
                    card_x = loc_btn[0] + 60 + btn_tmpl.shape[1] // 2
                    if not any(abs(card_x - c['x']) < 50 for c in existing_cards):
                        has_cc = not self._is_empty_siege(screen, card_x)
                        return dict(name='siege_machine', label='siege_machine',
                                    x=card_x, y=810, confidence=float(max_btn), count=1, has_cc=has_cc)
        return None

    def read_count(self, screen, x, scale=2):
        if screen is None:
            return None
        ocr = self.vision._get_ocr()
        if ocr is None:
            return None
        crop = screen[740:780, max(60, x - 56):min(1540, x + 56)]

        def _parse(texts):
            if not texts:
                return None
            joined = ''.join(str(text).replace(' ', '') for text in texts)
            match = re.fullmatch(r'[xX×]\s*(\d{1,3})', joined)
            if match and 0 <= int(match.group(1)) <= 300:
                return int(match.group(1))
            return None

        # 1. Try fast without det (with RapidOCR)
        try:
            res_fast = ocr(crop, use_det=False)
            cnt = _parse(getattr(res_fast, 'txts', None))
            if cnt is not None:
                return cnt
        except TypeError:
            pass
        except Exception:
            pass

        # 2. Scaled fallback (compatible with custom/mock ocr without keyword arguments)
        try:
            scaled = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            res = ocr(scaled)
            cnt = _parse(getattr(res, 'txts', None))
            if cnt is not None:
                return cnt
        except Exception:
            pass
        return None

    def scan(self, screen, log_callback=None):
        cards = {key: [] for key in ('troops', 'siege', 'heroes', 'spells')}
        if screen is None or screen.shape[:2] != (900, 1600):
            return cards
        # Portraits, excluding counts and levels. Templates from local assets.
        roi = screen[740:850, 60:1540]
        roi_g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        candidates = []
        for kind, identity, template, tmpl_g in self._templates():
            h, w = tmpl_g.shape[:2]
            if h > roi_g.shape[0] or w > roi_g.shape[1]:
                continue
            # Grayscale matchTemplate is 4-5x faster than 3-channel BGR
            result = cv2.matchTemplate(roi_g, tmpl_g, cv2.TM_CCOEFF_NORMED)
            for _ in range(12):
                _, score, _, location = cv2.minMaxLoc(result)
                if score < .84:
                    break
                x = location[0] + 60 + w // 2
                candidates.append(dict(kind=kind, name=identity, x=x, y=810, confidence=float(score)))
                left, right = max(0, location[0] - 60), min(result.shape[1], location[0] + 61)
                result[:, left:right] = -1
        selected = []
        for card in sorted(candidates, key=lambda c: c['confidence'], reverse=True):
            if not any(abs(card['x'] - old['x']) < 70 for old in selected):
                selected.append(card)
        ocr = self.vision._get_ocr()
        for card in sorted(selected, key=lambda c: c['x']):
            kind = card.pop('kind')
            card['label'] = card['name']
            if kind in ('troops', 'spells'):
                if ocr is None:
                    continue
                # Count sits above the portrait, within the same card bounds.
                x = card['x']
                count = self.read_count(screen, x)
                if count is None or not 1 <= count <= 300:
                    continue
                card['count'] = count
            elif kind == 'siege':
                card['count'] = 1
                card['has_cc'] = not self._is_empty_siege(screen, card['x'])
            cards[kind].append(card)
        if not cards['siege']:
            fallback_siege = self._find_siege_by_switch_or_badge(screen, selected)
            if fallback_siege is not None:
                cards['siege'].append(fallback_siege)
        if log_callback:
            log_callback('[Deck] ' + ', '.join(f'{len(value)} {key}' for key, value in cards.items()))
        return cards
