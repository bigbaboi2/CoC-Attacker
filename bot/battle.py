import time
import random
import numpy as np
from typing import Optional, Dict, Tuple, Any
from core.adb_client import ADBSocketClient
from core.vision import VisionEngine
from config.settings import BotSettings

class BattleSearcher:
    """
    Quản lý quy trình tìm trận, soi nhà, đọc tài nguyên và lọc đối thủ.
    Mỗi hành động đều có bước xác minh (Verification) nghiêm ngặt trước khi sang bước tiếp theo.
    """

    # Tọa độ nút bấm chuẩn màn hình 1600x900
    BATTLE_BTN_POS = (106, 856)          # Nút Tấn công ở Làng chính (góc dưới trái)
    FIND_MATCH_POS = (275, 650)          # Nút Tìm trận (Find a Match) chuẩn CoC mới (chuẩn cũ: 1401, 806)
    CONFIRM_ATTACK_POS = (1440, 800)     # Nút xác nhận Attack! trên bảng MY ARMY
    NEXT_BTN_POS = (1463, 617)           # Nút Next tìm nhà khác (góc phải khi soi nhà: 1463, 617)
    SURRENDER_BTN_POS = (116, 670)       # Nút End Battle / Surrender (góc dưới trái)
    CONFIRM_OKAY_POS = (970, 580)        # Nút xác nhận Okay trên hộp thoại bỏ trận
    RETURN_HOME_POS = (800, 750)         # Nút Return Home kết thúc trận

    def __init__(self, adb: ADBSocketClient, vision: VisionEngine, settings: BotSettings):
        self.adb = adb
        self.vision = vision
        self.settings = settings
        self.total_searches = 0
        self.total_skips = 0
        self.bonus_handler = None
        self.fault_check = None

    def open_attack_menu(self, log_callback=None) -> bool:
        """
        Bấm nút Tấn công tại Làng chính và XÁC MINH menu Tấn công đã mở ra.
        Có retry nếu mạng lag hoặc animation chậm.
        """
        # 1. Kiểm tra xem menu đã mở sẵn từ trước chưa
        screen = self.adb.screencap()
        is_open, btn_pos = self.vision.is_attack_menu_open(screen)
        if is_open:
            if log_callback:
                log_callback("[Battle] Menu Tấn công đã mở sẵn.")
            return True

        if log_callback:
            log_callback("[Battle] Bấm nút Tấn công tại Làng chính (106, 856)...")

        # Bấm nút Tấn công
        self.adb.tap(self.BATTLE_BTN_POS[0], self.BATTLE_BTN_POS[1], jitter=3, delay_after=0.4)

        # 2. Vòng lặp XÁC MINH menu đã mở (tối đa 4.5 giây)
        start_t = time.time()
        while time.time() - start_t < 4.5:
            time.sleep(0.35)
            screen = self.adb.screencap()
            is_open, btn_pos = self.vision.is_attack_menu_open(screen)
            if is_open:
                if log_callback:
                    log_callback(f"[Battle] Menu Tấn công mở thành công ({int((time.time() - start_t)*1000)}ms)!")
                return True

        if log_callback:
            log_callback("[Battle] CẢNH BÁO: Không mở được menu Tấn công sau 4.5s!")
        return False

    def start_find_match(self, log_callback=None) -> bool:
        """
        Bấm nút Tìm trận (Find a Match) và XÁC MINH game đã vào trạng thái tìm kiếm (mây trôi).
        Tự động xử lý cả bước xác nhận quân 'MY ARMY' (bấm nút Attack! tại 1440, 800) nếu có.
        """
        screen = self.adb.screencap()
        is_open, btn_pos = self.vision.is_attack_menu_open(screen)
        if is_open and self.bonus_handler is not None:
            screen=self.bonus_handler(screen,log_callback)
            if screen is None:
                return False
            is_open,btn_pos=self.vision.is_attack_menu_open(screen)
            if not is_open:
                return False

        if not is_open or not btn_pos:
            return False
        tap_x, tap_y = btn_pos

        if log_callback:
            log_callback(f"[Battle] Đã tap tại ({tap_x}, {tap_y}) để Tìm trận...")

        self.adb.tap(tap_x, tap_y, jitter=4, delay_after=0.8)

        # Poll for the army confirmation, which can appear after an animation.
        confirmed_army = (tap_x, tap_y) == self.CONFIRM_ATTACK_POS
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(.35)
            screen = self.adb.screencap()
            if screen is None:
                continue
            is_open, position = self.vision.is_attack_menu_open(screen)
            if not is_open:
                if log_callback:
                    log_callback('[Battle] Menu Tấn công đã đóng. Đang chờ đối thủ.')
                return True
            if position == self.CONFIRM_ATTACK_POS and not confirmed_army:
                self.adb.tap(*position, jitter=4, delay_after=.8)
                confirmed_army = True

        if log_callback:
            log_callback("[Battle] CẢNH BÁO: Nút Tìm trận chưa phản hồi!")
        return False

    def wait_for_enemy_base(self, timeout_sec: float = 40.0, log_callback=None) -> Optional[np.ndarray]:
        """
        Chờ mây trôi tìm đối thủ với XÁC MINH NGHIÊM NGẶT.
        Chỉ trả về màn hình khi đã 100% load xong căn cứ đối thủ (Next button hoặc End Battle xuất hiện).
        TUYỆT ĐỐI KHÔNG đoán mò bằng độ lệch chuẩn màu (np.std).
        """
        start_time = time.time()
        poll_count = 0
        next_fault_check = 0

        while time.time() - start_time < timeout_sec:
            time.sleep(0.6)
            poll_count += 1
            screen = self.adb.screencap()
            if screen is None:
                continue

            if self.fault_check is not None and self.vision.has_system_dialog(screen):
                if self.fault_check(screen):
                    return None

            # Xác minh nghiêm ngặt xem có phải là màn hình đối thủ
            if self.vision.is_enemy_base(screen):
                # Đợi thêm 0.8s để bộ đếm tài nguyên (Gold/Elixir animation) chạy xong
                time.sleep(0.8)
                stable_screen = self.adb.screencap()
                if log_callback:
                    elapsed = int(time.time() - start_time)
                    log_callback(f"[Matchmaking] Đã tìm thấy căn cứ đối thủ ({elapsed}s)!")
                return stable_screen if stable_screen is not None else screen

            now = time.monotonic()
            if self.fault_check is not None and now >= next_fault_check:
                next_fault_check = now + 2
                if self.fault_check(screen):
                    return None

            if poll_count % 5 == 0 and log_callback:
                elapsed = int(time.time() - start_time)
                log_callback(f"[Matchmaking] Đang tìm kiếm đối thủ trong mây... ({elapsed}s)")

        if log_callback:
            log_callback(f"[Matchmaking] Hết thời gian chờ đối thủ ({int(timeout_sec)}s)!")
        return None

    def evaluate_target(self, screen: np.ndarray) -> Tuple[bool, Dict[str, Any]]:
        """
        Đọc chính xác tài nguyên Vàng, Dầu, Hắc Dầu của căn cứ đối thủ.
        So sánh với ngưỡng cài đặt để quyết định Đánh hay Bỏ qua (Next).
        """
        loot = self.vision.read_loot(screen)
        # Retry only uncertain/very small readings. Two agreeing observations
        # distinguish genuine 0/1 from a transient or missing OCR result.
        keys=('gold','elixir','dark_elixir')
        known=loot.get('known',{})
        suspect=[key for key in keys if key in known and (not known[key] or loot.get(key,0)<=1)]
        if suspect:
            history={key:[loot[key]] if known.get(key,False) else [] for key in suspect}
            unresolved=set(suspect)
            for _ in range(2):
                time.sleep(.15)
                fresh=self.adb.screencap()
                if fresh is None or self.vision.battle_phase(fresh)!='scouting':
                    break
                observed=self.vision.read_loot(fresh)
                for key in list(unresolved):
                    if observed.get('known',{}).get(key,False):
                        history[key].append(observed[key])
                        if len(history[key])>=2 and history[key][-1]==history[key][-2]:
                            loot[key]=history[key][-1]
                            known[key]=True
                            unresolved.remove(key)
                if not unresolved:
                    break
            for key in unresolved:
                known[key]=False
            loot['known']=known
            loot['valid']=any(known.values())
            loot['valid_dark']=known.get('dark_elixir',False)
        min_gold = self.settings.get("min_gold", 600000)
        min_elixir = self.settings.get("min_elixir", 600000)
        min_dark = self.settings.get("min_dark_elixir", 4000)

        # Nếu không đọc được tài nguyên hợp lệ (hoặc không phải màn hình soi nhà)
        if not loot.get("valid", False):
            # Nếu thật sự là enemy base mà không có tài nguyên -> Bỏ qua
            return False, loot

        thresholds=dict(gold=min_gold,elixir=min_elixir,dark_elixir=min_dark)
        needed = getattr(self, 'farm_needed_resources', None)
        if (self.settings.get('farm_finish_enabled', False) or self.settings.get('auto_upgrade_army', False)
                or self.settings.get('auto_buy_new_buildings', False)) and isinstance(needed, set) and needed:
            # A full gold store must not qualify a base when only elixir is missing.
            wanted = [key for key in keys if key in needed]
            return any(loot.get('known', {}).get(key, False)
                       and loot.get(key, 0) >= max(1, thresholds[key]) for key in wanted), loot
        selected=[key for key in keys if self.settings.get('loot_filter_'+key,True)]
        meets={key:loot.get('known',{}).get(key,loot.get('valid_dark',True) if key=='dark_elixir' else loot.get('valid',False))
                    and loot.get(key,0)>=thresholds[key] for key in selected}
        mode=self.settings.get('loot_filter_mode','legacy')
        if not selected:
            should_attack=False
        elif mode=='all':
            should_attack=all(meets.values())
        elif mode=='any':
            should_attack=any(meets.values())
        else:
            main=[meets[key] for key in ('gold','elixir') if key in meets]
            should_attack=(any(main) if main else True) and (meets.get('dark_elixir',True) or 'dark_elixir' in meets and min_dark==0)

        return should_attack, loot

    def click_next(self, log_callback=None) -> bool:
        """
        Bấm nút Next (1463, 617) để tìm nhà tiếp theo.
        Xác minh nút Next đã biến mất (mây bắt đầu trôi) trước khi chuyển trạng thái.
        """
        screen = self.adb.screencap()
        next_pos = self.NEXT_BTN_POS

        # Lấy tọa độ template find.png hoặc OCR chữ Next
        if screen is not None:
            match = self.vision.match_template(screen, "find", threshold=0.72, roi=(1350, 550, 240, 320))
            if match:
                next_pos = (match[0], match[1])

        if log_callback:
            log_callback(f"[Search] Bấm Next tại ({next_pos[0]}, {next_pos[1]})...")

        self.adb.tap(next_pos[0], next_pos[1], jitter=4, delay_after=0.4)
        self.total_skips += 1

        # Xác minh màn hình chuyển sang mây trôi (nút Next biến mất)
        start_t = time.time()
        while time.time() - start_t < 3.0:
            time.sleep(0.3)
            scr = self.adb.screencap()
            if scr is None:
                break
            m = self.vision.match_template(scr, "find", threshold=0.72, roi=(1350, 550, 240, 320))
            if not m:
                # Đã bắt đầu chuyển cảnh sang nhà kế tiếp
                return True

        return False

    def surrender_and_return_home(self, log_callback=None):
        from bot.combat import CombatExecutor
        return CombatExecutor(self.adb,self.vision,self.settings)._surrender_and_exit(log_callback)
