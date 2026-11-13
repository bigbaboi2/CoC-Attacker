import os
import cv2
import numpy as np
import threading
import hashlib
import re
from collections import OrderedDict
from typing import Optional, Tuple, List, Dict, Union, Any

class SafeOCR:
    """Wraps RapidOCR to prevent internal state mutation where use_det=False sticks forever."""
    def __init__(self, raw_ocr):
        self._raw = raw_ocr
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def __call__(self, img, **kwargs):
        if "use_det" not in kwargs or kwargs["use_det"] is None:
            kwargs["use_det"] = True
        # Exact pixels only: never reuse a quantity/price from a different image.
        key = (img.shape, img.dtype.str, tuple(sorted(kwargs.items())),
               hashlib.blake2b(img.tobytes(), digest_size=16).digest())
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            result = self._raw(img, **kwargs)
            self._cache[key] = result
            while len(self._cache) > 24:
                self._cache.popitem(last=False)
            return result

    def __getattr__(self, name):
        return getattr(self._raw, name)


class VisionEngine:
    """
    High-performance OpenCV Template Matching and OCR Engine.
    Features:
    - In-memory template caching
    - Region of Interest (ROI) slicing for sub-millisecond matching
    - Multi-object detection with Non-Maximum Suppression (NMS)
    - Hybrid OCR: RapidOCR + Fast Template Digit Matching
    """

    def __init__(self, asset_dir: Optional[str] = None, ocr_threads: int = 1):
        cv2.setNumThreads(1)
        self._ocr_threads = max(1, min(2, int(ocr_threads)))
        self.asset_dir = asset_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets"
        )
        self._template_cache: Dict[str, Optional[np.ndarray]] = {}
        self._digit_templates: Dict[int, List[np.ndarray]] = {}
        self._close_button_cache = None
        self._ocr = None
        self._init_digit_templates()

    def _get_template_path(self, name_or_relpath: str) -> str:
        """Resolve full path to template in asset_dir."""
        if os.path.isabs(name_or_relpath):
            return name_or_relpath
        # Check direct path (must be a FILE: on Windows 'okay' also matches folder 'Okay')
        p1 = os.path.join(self.asset_dir, name_or_relpath)
        if os.path.isfile(p1):
            return p1
        # Check with .png
        if not name_or_relpath.endswith(".png"):
            p2 = os.path.join(self.asset_dir, f"{name_or_relpath}.png")
            if os.path.isfile(p2):
                return p2
        # Check subdirectories
        for root, _, files in os.walk(self.asset_dir):
            target = name_or_relpath if name_or_relpath.endswith(".png") else f"{name_or_relpath}.png"
            if target in files:
                return os.path.join(root, target)
        return p1

    def load_template(self, name_or_relpath: str) -> Optional[np.ndarray]:
        """Loads template image and caches in memory."""
        if name_or_relpath in self._template_cache:
            return self._template_cache[name_or_relpath]

        full_path = self._get_template_path(name_or_relpath)
        if not os.path.isfile(full_path):
            self._template_cache[name_or_relpath] = None
            return None

        tmpl = cv2.imread(full_path)
        self._template_cache[name_or_relpath] = tmpl
        return tmpl

    def _init_digit_templates(self):
        """Preload digit templates for fast loot reading."""
        loot_digit_dir = os.path.join(self.asset_dir, "digit_templates_loot")
        if not os.path.exists(loot_digit_dir):
            return

        for digit in range(10):
            d_dir = os.path.join(loot_digit_dir, str(digit))
            self._digit_templates[digit] = []
            if os.path.exists(d_dir):
                for f in os.listdir(d_dir):
                    if f.endswith(".png"):
                        fp = os.path.join(d_dir, f)
                        t = cv2.imread(fp, cv2.IMREAD_GRAYSCALE)
                        if t is not None:
                            self._digit_templates[digit].append(t)

    def _get_ocr(self):
        """Lazy-load RapidOCR with bounded threads and disabled rotation classifier."""
        if self._ocr is None:
            try:
                from rapidocr import RapidOCR
                params = {
                    'Global.use_cls': False,
                    'Global.log_level': 'error',
                    # Cropped HUD lines must not be enlarged until their SHORT
                    # edge reaches 736px (a 58px line becomes thousands wide).
                    'Det.limit_type': 'max',
                    'EngineConfig.onnxruntime.intra_op_num_threads': self._ocr_threads,
                    'EngineConfig.onnxruntime.inter_op_num_threads': 1
                }
                raw = RapidOCR(params=params)
                self._ocr = SafeOCR(raw)
            except Exception as e:
                print(f"[Vision] Warning: RapidOCR not available: {e}")
                self._ocr = False
        return self._ocr if self._ocr is not False else None

    def match_template(
        self,
        screen: np.ndarray,
        template_name: str,
        threshold: float = 0.8,
        roi: Optional[Tuple[int, int, int, int]] = None
    ) -> Optional[Tuple[int, int, int, int, float]]:
        """
        Match a template on screen or within an ROI.
        ROI format: (x, y, width, height)
        Returns: (center_x, center_y, width, height, confidence) or None
        """
        tmpl = self.load_template(template_name)
        if tmpl is None or screen is None:
            return None

        offset_x, offset_y = 0, 0
        search_img = screen

        if roi:
            rx, ry, rw, rh = roi
            # Clamp bounds
            sh, sw = screen.shape[:2]
            rx = max(0, min(rx, sw - 1))
            ry = max(0, min(ry, sh - 1))
            rw = max(1, min(rw, sw - rx))
            rh = max(1, min(rh, sh - ry))
            search_img = screen[ry : ry + rh, rx : rx + rw]
            offset_x, offset_y = rx, ry

        th, tw = tmpl.shape[:2]
        sh, sw = search_img.shape[:2]

        if th > sh or tw > sw:
            return None

        res = cv2.matchTemplate(search_img, tmpl, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

        if max_val >= threshold:
            top_left_x = max_loc[0] + offset_x
            top_left_y = max_loc[1] + offset_y
            center_x = top_left_x + tw // 2
            center_y = top_left_y + th // 2
            return (center_x, center_y, tw, th, float(max_val))

        return None

    def match_all(
        self,
        screen: np.ndarray,
        template_name: str,
        threshold: float = 0.8,
        roi: Optional[Tuple[int, int, int, int]] = None,
        min_distance: int = 20,
        max_matches: int = 30
    ) -> List[Tuple[int, int, int, int, float]]:
        """
        Find multiple occurrences of template using Non-Maximum Suppression.
        Returns list of (center_x, center_y, width, height, confidence)
        """
        tmpl = self.load_template(template_name)
        if tmpl is None or screen is None:
            return []

        offset_x, offset_y = 0, 0
        search_img = screen

        if roi:
            rx, ry, rw, rh = roi
            sh, sw = screen.shape[:2]
            search_img = screen[ry : ry + rh, rx : rx + rw]
            offset_x, offset_y = rx, ry

        th, tw = tmpl.shape[:2]
        sh, sw = search_img.shape[:2]

        if th > sh or tw > sw:
            return []

        res = cv2.matchTemplate(search_img, tmpl, cv2.TM_CCOEFF_NORMED)
        locs = np.where(res >= threshold)

        candidates = []
        for pt in zip(*locs[::-1]):
            val = float(res[pt[1], pt[0]])
            cx = pt[0] + offset_x + tw // 2
            cy = pt[1] + offset_y + th // 2
            candidates.append((cx, cy, tw, th, val))

        # Sort candidates descending by confidence
        candidates.sort(key=lambda c: c[4], reverse=True)

        # Distance-based Non-Maximum Suppression
        selected = []
        for cand in candidates:
            if len(selected) >= max_matches:
                break
            cx, cy, _, _, _ = cand
            too_close = False
            for scx, scy, _, _, _ in selected:
                dist = ((cx - scx) ** 2 + (cy - scy) ** 2) ** 0.5
                if dist < min_distance:
                    too_close = True
                    break
            if not too_close:
                selected.append(cand)

        return selected

    def read_digits_ocr(self, img_region: np.ndarray) -> str:
        """
        Read digits from an image region using RapidOCR.
        Filters out non-numeric characters.
        """
        ocr = self._get_ocr()
        if ocr is None or img_region is None or img_region.size == 0:
            return ""

        try:
            res = ocr(img_region, use_det=False)
            if res and hasattr(res, "txts") and res.txts:
                digits = "".join([c for c in "".join(res.txts) if c.isdigit()])
                if digits:
                    return digits
            res = ocr(img_region)
            if not res or not hasattr(res, "txts") or not res.txts:
                return ""
            full_txt = "".join(res.txts)
            digits = "".join([c for c in full_txt if c.isdigit()])
            return digits
        except Exception:
            return ""

    @staticmethod
    def has_system_dialog(screen):
        """Find flat neutral Android dialog rows without OCR on normal game frames."""
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        rows = screen[300:600:12, 420:1180:12]
        flat = (np.ptp(rows, axis=1) <= 10).all(axis=1)
        neutral = np.ptp(rows[:, 0], axis=1) <= 25
        return np.count_nonzero(flat & neutral) >= 2

    def game_fault(self, screen):
        """Classify blocking messages from the central game dialog."""
        self.last_game_fault_message = None
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return None
        ocr = self._get_ocr()
        if ocr is None:
            return None
        try:
            result = ocr(screen[180:760, 300:1300], use_det=True)
        except Exception:
            return None
        message = ' '.join(getattr(result, 'txts', None) or ()).lower()
        message = ' '.join(message.split())
        if re.search(r'\b(?:captcha|robot check|(?:not a|are you a|i am a) robot|verify (?:that )?you are human|human verification)\b', message):
            self.last_game_fault_message = message[:180]
            return 'robot'
        if re.search(r'\b(?:connection (?:lost|error|failed|interrupted)|(?:lost|no|check your) (?:internet |network )?connection|(?:unable|failed|could not) to connect|disconnected|network error|reload game|server (?:error|unavailable)|out of sync)\b', message):
            self.last_game_fault_message = message[:180]
            return 'network'
        return None

    def find_close_button(self,screen):
        if screen is None:return None
        if screen.shape[:2] != (900, 1600):
            return self._detect_close_button(screen)
        # Both searches are confined to this strip at the game's native size.
        # Compare exact pixels so a newly opened or moved dialog is checked again.
        pixels = screen[:180, 1000:1600].tobytes()
        key = (screen.shape, screen.dtype, pixels)
        cached = self._close_button_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        result = self._detect_close_button(screen)
        self._close_button_cache = (key, result)
        return result

    def _detect_close_button(self, screen):
        from pathlib import Path
        panel_path=str(Path(__file__).resolve().parents[1]/'local_assets'/'close_panel.png')
        panel=self.match_template(screen,panel_path,.90,roi=(1450,0,150,130))
        if panel:return panel
        template=self.load_template('x')
        if template is None:return None
        area=screen[0:180,1000:1600]
        best=None
        for scale in (.75,.85,1.0,1.15,1.3,1.5):
            candidate=cv2.resize(template,None,fx=scale,fy=scale)
            h,w=candidate.shape[:2]
            if h>area.shape[0] or w>area.shape[1]:continue
            _,score,_,location=cv2.minMaxLoc(cv2.matchTemplate(area,candidate,cv2.TM_CCOEFF_NORMED))
            if score>=.80 and (best is None or score>best[4]):
                best=(location[0]+1000+w//2,location[1]+h//2,w,h,float(score))
        return best

    def wall_confirmation(self,screen):
        from pathlib import Path
        path=str(Path(__file__).resolve().parents[1]/'local_assets'/'wall_confirm_title.png')
        return self.match_template(screen,path,.90,roi=(350,150,900,250))

    def find_okay_button(self, screen):
        """Nhận diện nút xanh 'Okay' trên các bảng thông báo giữa màn hình (VD: 'Welcome Back Chief!', thưởng sao...)."""
        if not isinstance(screen, np.ndarray):
            return None
        if screen.shape[:2] == (900, 1600):
            pixels = screen[450:850, 580:1020].tobytes()
            key = (screen.shape, screen.dtype, pixels)
            cached = getattr(self, '_okay_button_cache', None)
            if cached is not None and cached[0] == key:
                return cached[1]
            result = self._detect_okay_button(screen)
            self._okay_button_cache = (key, result)
            return result
        return self._detect_okay_button(screen)

    def _detect_okay_button(self, screen):
        if not isinstance(screen, np.ndarray):
            return None
        match = self.match_template(screen, "okay", threshold=0.75, roi=(500, 450, 600, 400))
        if match:
            return match
        if screen.shape[:2] != (900, 1600):
            return None
        rx, ry, rw, rh = 580, 460, 440, 390
        roi = screen[ry:ry+rh, rx:rx+rw]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (33, 85, 110), (88, 255, 255))
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if not (110 <= w <= 270 and 42 <= h <= 110):
                continue
            fill = float((mask[y:y+h, x:x+w] > 0).mean())
            if fill < 0.70:
                continue
            cx, cy = rx + x + w // 2, ry + y + h // 2
            if not (700 <= cx <= 900):
                continue
            # Kiểm tra vùng nền dialog phẳng màu kem/trắng ngay phía trên nút Okay
            bg_top = screen[max(0, ry+y-18):max(0, ry+y-4), max(0, cx-50):min(1600, cx+50)]
            if bg_top.size == 0:
                continue
            bg_hsv = cv2.cvtColor(bg_top, cv2.COLOR_BGR2HSV)
            if bg_hsv[:, :, 1].mean() > 50 or bg_hsv[:, :, 2].mean() < 130:
                continue
            ocr = self._get_ocr()
            if ocr is not None:
                try:
                    btn_crop = screen[max(0, ry+y-4):min(900, ry+y+h+4), max(0, rx+x-4):min(1600, rx+x+w+4)]
                    res = ocr(btn_crop)
                    txts = ' '.join(getattr(res, 'txts', None) or ()).lower()
                    if any(k in txts for k in ('okay', 'ok', '0kay')):
                        return (cx, cy, w, h, fill)
                except Exception:
                    pass
            elif self.find_close_button(screen) is None:
                return (cx, cy, w, h, fill)
        return None

    def has_open_dialog(self, screen):
        return (
            self.find_close_button(screen) is not None
            or self.wall_confirmation(screen) is not None
            or self.find_okay_button(screen) is not None
        )

    def is_in_main_village(self, screen: np.ndarray) -> bool:
        if screen is None or screen.shape[:2] != (900, 1600) or self.has_open_dialog(screen):
            return False
        if self.match_template(screen, 'battle', threshold=0.78, roi=(30, 800, 160, 95)):
            return True
        ocr = self._get_ocr()
        if ocr:
            try:
                result = ocr(screen[820:890, 40:180])
                texts = getattr(result, 'txts', None)
                return any('battle' in text.lower() for text in (texts if texts is not None else []))
            except Exception:
                pass
        return False

    def battle_phase(self, screen):
        # ADB returns a new immutable BGR frame per capture. Reuse recognition
        # when that same frame is checked by matchmaking, filtering and loot OCR.
        if screen is not None and getattr(self, '_phase_frame', None) is screen:
            return self._phase_result
        result = self._detect_battle_phase(screen)
        self._phase_frame, self._phase_result = screen, result
        return result

    def _detect_battle_phase(self, screen):
        """Only a Next button/start countdown identifies scouting; ends identifies combat."""
        if screen is None or screen.shape[:2] != (900, 1600):
            return None
        if self.match_template(screen, 'find', threshold=0.72, roi=(1350, 550, 240, 320)):
            return 'scouting'
        ocr = self._get_ocr()
        if ocr:
            try:
                result = ocr(screen[10:100, 650:950])
                texts = getattr(result, 'txts', None)
                header = ''.join(text.lower().replace(' ', '') for text in (texts if texts is not None else []))
                if 'battle' in header and 'starts' in header:
                    return 'scouting'
                if 'battle' in header and 'ends' in header:
                    return 'combat'
                result = ocr(screen[550:710, 1350:1590])
                texts = getattr(result, 'txts', None)
                if any('next' in text.lower() for text in (texts if texts is not None else [])):
                    return 'scouting'
            except Exception:
                pass
        if self.match_template(screen, 'surrender', threshold=0.72, roi=(25, 600, 220, 120)):
            return 'combat'
        if self.match_template(screen, '1star', threshold=0.75, roi=(15, 600, 230, 120)):
            return 'combat'
        return None

    def is_enemy_base(self, screen: np.ndarray) -> bool:
        return self.battle_phase(screen) == 'scouting'

    def is_in_combat(self, screen: np.ndarray) -> bool:
        return self.battle_phase(screen) == 'combat'

    def is_attack_menu_open(self, screen: np.ndarray) -> Tuple[bool, Optional[Tuple[int, int]]]:
        """
        Xác minh xem Menu Tấn công (Attack / Multiplayer) đã mở ra hay chưa.
        Tương thích cả giao diện mới (CoC Town Hall update), bảng xác nhận quân MY ARMY, và giao diện cũ:
        - Bảng MY ARMY: Nút Attack! tại (1440, 800)
        - Giao diện mới: Tabs Multiplayer / Single Player ở trên cùng, nút Find a Match tại (275, 650)
        - Giao diện cũ: Nút Tìm trận (findmatch.png) ở góc dưới phải (1401, 806)
        Trả về: (is_open: bool, (btn_x, btn_y): Tuple[int, int])
        """
        if screen is None:
            return False, None

        # 1. Kiểm tra template cũ findmatch / backupfindmatch
        match = self.match_template(screen, "findmatch", threshold=0.72, roi=(100,500,1480,360))
        if match:
            return True, (match[0], match[1])

        match_bak = self.match_template(screen, "backupfindmatch", threshold=0.72, roi=(100,500,1480,360))
        if match_bak:
            return True, (match_bak[0], match_bak[1])

        # 2. Kiểm tra bằng OCR toàn diện
        ocr = self._get_ocr()
        if ocr:
            try:
                # A. Kiểm tra nhanh bảng xác nhận quân MY ARMY (nút Attack! tại 1440, 800)
                crop_atk = screen[750:840, 1330:1550]
                res_atk = ocr(crop_atk)
                if res_atk and hasattr(res_atk, "txts") and res_atk.txts:
                    for t in res_atk.txts:
                        if "attack" in t.lower():
                            return True, (1440, 800)

                # B. Quét nhanh thanh tab trên (y: 40-110)
                tabs_crop = screen[40:110, 80:1000]
                tab_res = ocr(tabs_crop)
                has_tabs = False
                if tab_res and hasattr(tab_res, "txts") and tab_res.txts:
                    for t in tab_res.txts:
                        clean_t = t.replace(" ", "").lower()
                        if "multiplayer" in clean_t or "singleplayer" in clean_t or "practice" in clean_t or "army" in clean_t:
                            has_tabs = True
                            break

                # C. Quét tìm nút "Find a Match" (y: 550-750)
                mid_crop = screen[550:750, 100:1000]
                mid_res = ocr(mid_crop)
                btn_pos = None

                if mid_res and hasattr(mid_res, "txts") and mid_res.txts:
                    for txt, box in zip(mid_res.txts, mid_res.boxes):
                        clean_txt = txt.replace(" ", "").lower()
                        if "findamatch" in clean_txt or ("find" in clean_txt and "match" in clean_txt):
                            cx = int(np.mean(box[:, 0])) + 100
                            cy = int(np.mean(box[:, 1])) + 550
                            if btn_pos is None or cx < 500:
                                btn_pos = (cx, cy)

                if has_tabs or btn_pos is not None:
                    chosen_pos = btn_pos if btn_pos else (275, 650)
                    return True, chosen_pos
            except Exception as e:
                pass

        return False, None

    def read_loot(self, screen):
        loot={'gold':0,'elixir':0,'dark_elixir':0,'valid':False,'valid_dark':False}
        if screen is None or self.battle_phase(screen) not in ('scouting','combat'):return loot
        values={}
        for key,(x,y,w,h) in {'gold':(80,105,225,40),'elixir':(80,150,225,42),'dark_elixir':(80,200,180,42)}.items():
            value=self.read_enemy_resource_number(screen[y:y+h,x:x+w])
            values[key]=value
            loot[key]=value if value is not None else 0
        loot['known']={key:value is not None for key,value in values.items()}
        loot['valid']=any(value is not None for value in values.values())
        loot['valid_dark']=values['dark_elixir'] is not None
        return loot

    def read_enemy_resource_number(self,crop):
        """Loot numerals start at the left edge; exclude bright village objects."""
        if crop is None or not crop.size:
            return None
        hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
        mask=cv2.inRange(hsv,(0,0,191),(179,89,255))
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        boxes=sorted((cv2.boundingRect(contour) for contour in contours),key=lambda box:box[0])
        boxes=[box for box in boxes if 14<=box[3]<=36 and 3<=box[2]<=30]
        anchor=next((box for box in boxes if box[0]<=18),None)
        if anchor is None:
            return None
        baseline=anchor[1]+anchor[3]
        group=[];right=anchor[0]
        for box in boxes:
            x,y,w,h=box
            if x<anchor[0] or not .7*anchor[3]<=h<=1.35*anchor[3] or abs(y+h-baseline)>max(3,anchor[3]*.2):
                continue
            if x-right>20:
                break
            group.append(box);right=x+w
        clean=np.zeros_like(crop)
        for x,y,w,h in group:
            clean[y:y+h,x:x+w]=crop[y:y+h,x:x+w]
        return self.read_white_number(clean)

    def read_destruction_percent(self, screen):
        """Read only the battle HUD percentage, never infer it from stars."""
        if screen is None or screen.shape[:2] != (900,1600):
            return None
        ocr=self._get_ocr()
        if ocr is None:
            return None
        import re
        crop=screen[650:710,1430:1595]
        try:
            for scale in (1,2):
                image=crop if scale==1 else cv2.resize(crop,None,fx=scale,fy=scale)
                result=ocr(image,use_det=False)
                text=' '.join(getattr(result,'txts',()) or ())
                match=re.fullmatch(r'\s*(\d{1,3})\s*[%％]\s*',text)
                if match and 0<=int(match.group(1))<=100:
                    return int(match.group(1))
        except Exception:
            pass
        return None

    def read_white_number(self, crop):
        """Read white HUD numerals; reject OCR that omits visible digit components."""
        if crop is None or not crop.size:return None
        hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
        _,saturation,value=cv2.split(hsv)
        mask=((saturation<90)&(value>190)).astype(np.uint8)*255
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        components=[]
        clean=np.zeros_like(mask)
        for contour in contours:
            x,y,w,h=cv2.boundingRect(contour)
            if 10<=h<=36 and 3<=w<=30:
                components.append((x,y,w,h))
                cv2.drawContours(clean,[contour],-1,255,cv2.FILLED)
        if not components:return None
        # White reflections in the resource bar can look like a leading "1".
        # HUD digits share the height/baseline of the rightmost numeral.
        anchor = max(components, key=lambda box: box[0] + box[2])
        baseline = anchor[1] + anchor[3]
        components = [box for box in components
                      if .7*anchor[3] <= box[3] <= 1.35*anchor[3]
                      and abs(box[1]+box[3]-baseline) <= max(3, anchor[3]*.2)]
        clean[:] = 0
        for x,y,w,h in components:
            clean[y:y+h,x:x+w] = mask[y:y+h,x:x+w]
        clean=cv2.bitwise_and(clean,mask)
        canvas=cv2.cvtColor(255-clean,cv2.COLOR_GRAY2BGR)
        canvas=cv2.copyMakeBorder(canvas,6,6,6,6,cv2.BORDER_CONSTANT,value=(255,255,255))
        ocr=self._get_ocr()
        if not ocr:return None
        try:
            # 1. Ultra-fast direct recognition (~20ms vs ~3000ms with detection network)
            res = ocr(canvas, use_det=False)
            if res and res.txts:
                digits = ''.join(c for c in res.txts[0] if c.isdigit())
                if len(digits) == len(components) and digits:
                    return int(digits)

            res_crop = ocr(crop, use_det=False)
            if res_crop and res_crop.txts:
                digits = ''.join(c for c in res_crop.txts[0] if c.isdigit())
                if len(digits) == len(components) and digits:
                    return int(digits)

            # 2. Reliable fallback to detection mode
            result=ocr(cv2.resize(canvas,None,fx=2,fy=2,interpolation=cv2.INTER_LINEAR))
            texts=getattr(result,'txts',None)
            digits=''.join(c for text in (texts if texts is not None else []) for c in text if c in '0123456789')
            if len(digits)==len(components) and digits:return int(digits)
            original=ocr(cv2.resize(crop,None,fx=2,fy=2,interpolation=cv2.INTER_LINEAR))
            original_texts=getattr(original,'txts',None)
            original_digits=''.join(c for text in (original_texts if original_texts is not None else []) for c in text if c in '0123456789')
            if digits and original_digits==digits:return int(digits)
            if len(components)==1 and components[0][2]/components[0][3]<.48:
                return 1
        except Exception:pass
        return None

    def read_village_resources(self, screen, *, builder=False):
        rows = {'gold': (1320, 25, 205, 42), 'elixir': (1320, 85, 205, 45)}
        if builder:
            rows['gems'] = (1375, 151, 150, 40)
        else:
            rows.update(dark_elixir=(1320, 145, 205, 45), gems=(1400, 212, 125, 40))
        resources = dict.fromkeys(rows, 0)
        resources['valid'] = {}
        if screen is None:
            return resources
        for key, (x, y, w, h) in rows.items():
            amount=self.read_white_number(screen[y:y+h,x:x+w])
            resources['valid'][key]=amount is not None
            resources[key]=amount if amount is not None else 0
        return resources

    def read_player_profile_header(self, screen: np.ndarray) -> Optional[dict]:
        """Read top-left player profile banner: (level, player_name, account_key)."""
        if screen is None or not screen.size:
            return None
        height, width = screen.shape[:2]
        crop = screen[0:min(120, height), 0:min(320, width)]
        ocr = self._get_ocr()
        if ocr is None:
            return None
        try:
            res = ocr(crop, use_det=True)
            txts = getattr(res, 'txts', [])
            name = None
            level = None
            for t in txts:
                clean = t.strip()
                if clean.isdigit() and level is None and int(clean) <= 600:
                    level = int(clean)
                elif name is None and not clean.isdigit() and len(clean) >= 2:
                    name = clean
            if name or level:
                acc_key = f"{name or 'Player'}_{level or 0}"
                return {'name': name or 'Player', 'level': level or 0, 'account_key': acc_key}
        except Exception:
            pass
        return None

    def read_resource_capacity_popup(self, screen):
        """Read only the Max line, never the hourly production or treasury."""
        if screen is None or not screen.size:
            return None
        height, width = screen.shape[:2]
        crop = screen[:min(height, 390), max(0, width-650):width]
        ocr = self._get_ocr()
        if ocr is None:
            return None
        try:
            result = ocr(cv2.resize(crop, None, fx=2, fy=2), use_det=True)
            texts = getattr(result, 'txts', None)
            boxes = getattr(result, 'boxes', None)
            if texts is None:
                return None
            # Use OCR to locate the labelled line, then read only its digits.
            # A full-line recognition can hallucinate a "1" in a thousands gap.
            if boxes is not None and len(boxes) == len(texts):
                located = set()
                saw_max = False
                for text, box in zip(texts, boxes):
                    if not re.search(r'\bmax(?:imum)?\b', text, re.I):
                        continue
                    saw_max = True
                    left = max(0, int(box[:, 0].min()/2)-3)
                    top = max(0, int(box[:, 1].min()/2)-3)
                    bottom = min(crop.shape[0], int(box[:, 1].max()/2)+4)
                    # Include a separately detected number to the right.
                    row = crop[top:bottom, left:]
                    mask = cv2.inRange(cv2.cvtColor(row, cv2.COLOR_BGR2HSV),
                                       (0, 0, 180), (180, 100, 255))
                    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    dots = [cv2.boundingRect(contour) for contour in contours
                            if 1 <= cv2.boundingRect(contour)[2] <= 6
                            and 1 <= cv2.boundingRect(contour)[3] <= 6]
                    colons = [max(a[0]+a[2], b[0]+b[2]) for a in dots for b in dots
                              if abs(a[0]-b[0]) <= 2 and 4 <= b[1]-a[1] <= 12
                              and a[0] < min(110, row.shape[1]*.5)]
                    if colons:
                        value = self.read_white_number(row[:, max(colons)+2:])
                        if value is not None and 1000 <= value <= 2000000000:
                            located.add(value)
                if saw_max:
                    return located.pop() if len(located) == 1 else None
            lines = list(texts)
            if boxes is not None and len(boxes) == len(texts):
                # OCR sometimes separates "Max:" and the number into boxes.
                words = sorted(((float(box[:, 1].mean()), float(box[:, 0].mean()), text)
                                for text, box in zip(texts, boxes)), key=lambda item: item[0])
                grouped = []
                for y, x, text in words:
                    if grouped and abs(y-grouped[-1][0]) <= 14:
                        grouped[-1][1].append((x, text))
                    else:
                        grouped.append((y, [(x, text)]))
                lines.extend(' '.join(text for _, text in sorted(row)) for _, row in grouped)
            capacities = set()
            for line in lines:
                match = re.search(r'\bmax(?:imum)?\s*[:：.]?\s*([0-9OoIiIl|][0-9OoIiIl| ,.\u00a0]*)\s*$', line, re.I)
                if not match:
                    continue
                token = match.group(1).translate(str.maketrans({'O':'0', 'o':'0', 'I':'1',
                                                              'i':'1', 'l':'1', '|':'1'}))
                groups = re.split(r'[ ,.\u00a0]+', token.strip())
                if len(groups) > 1 and (not 1 <= len(groups[0]) <= 3
                                        or any(len(group) != 3 for group in groups[1:])):
                    continue
                digits = re.sub(r'[ ,.\u00a0]', '', token)
                if digits.isascii() and digits.isdigit() and 1000 <= int(digits) <= 2000000000:
                    capacities.add(int(digits))
            return capacities.pop() if len(capacities) == 1 else None
        except Exception:
            return None
