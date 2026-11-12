import time
import cv2
import json
import random
from pathlib import Path
from core.deck import DeckRecognizer
from core.deployment import DeploymentPlanner
from core.targeting import SpellTargeter
from core.combat_policy import BattleEndPolicy
from core.attack_plan import strategy_points
from core.resource_targets import ResourceTargetDetector
from core.ultra_attack import UltraAttackPlan
from core.fixed_attack import FixedAttackPlan

class CombatExecutor:
    """Deploy observed cards, coordinate timed spells/heroes, and verify battle exit."""
    def __init__(self, adb, vision, settings, screen_callback=None, village_state=None):
        self.village_state = village_state
        self.adb,self.vision,self.settings=adb,vision,settings
        self.screen_callback=screen_callback
        self.recognizer=DeckRecognizer(vision)
        self.planner=DeploymentPlanner()
        self.battle_start_time=0
        self.combo_rotation_index=0
        self.hero_actions=[]
        self._log=lambda message:None
        self._loot_initial=None
        self._loot_gained={}
        self.pending_spells=[]
        self._verified_drop_points=[]
        self.targeter=SpellTargeter()
        self.last_spell_at=0
        self._end_policy=None
        self._active_fight=False
        self._last_destruction=None
        self._last_loot_percent=None
        self._troop_points_by_name={}
        self.resource_detector=None
        self._ultra_active=False
        self._timing_valid=False
        self._fifty_streak=0
        self._time_to_50_ms=0
        self._ultra_skill_slots=[]
        self._ultra_skill_repeats=0
        self._ultra_last_skill=0
        self._ultra_trace=None
        self._record_seen_below_50=False
        self._last_random_strategy=None
        self._opening_support_sent=False
        self._prepared_spell_targets={}
        self._deployment_pending=False
        self._deployment_recoveries=0
        self._fixed_audit_pending = False

    def save_ultra_trace(self):
        if self._ultra_trace is not None:
            try:
                path=Path(__file__).resolve().parents[1]/'supervision'/'runtime'/'ultra-last.json'
                path.parent.mkdir(exist_ok=True,parents=True)
                from core.stats_store import StatsStore
                path.write_text(json.dumps(self._ultra_trace,ensure_ascii=False,indent=2,default=StatsStore.json_default),encoding='utf-8')
            except (OSError, TypeError, ValueError):
                pass

    def scan_deck_cards(self,screen,log_callback=None):
        return self.recognizer.scan(screen,log_callback)

    def zoom_out_map(self,times=2,log_callback=None):
        self.adb.zoom_out_map(times=times)
        time.sleep(.3)

    def interpolate_line_points(self,p1,p2,count):
        if count<=0:return []
        return [(round(p1[0]+(p2[0]-p1[0])*i/max(1,count-1)),round(p1[1]+(p2[1]-p1[1])*i/max(1,count-1))) for i in range(count)]

    def select_slot_pos(self,x,y,log_callback=None):
        self.adb.tap(x,y,jitter=0,delay_after=.03)
        self._selected_slot = (x, y)

    def _control(self):
        self.adb._guard_input()

    def _abilities(self):
        self._control()
        if self._ultra_active:
            now=time.monotonic()
            if self._ultra_skill_repeats<3 and now-self._ultra_last_skill>=.7 and self._ultra_skill_slots:
                self.adb.tap_batch(self._ultra_skill_slots,jitter=0,interval_ms=0,delay_after=0)
                self._ultra_skill_repeats+=1;self._ultra_last_skill=now
            return
        if not self.settings.get('trigger_hero_abilities',True):return
        now=time.monotonic()
        delay=max(0,float(self.settings.get('hero_ability_delay_sec',5)))
        for action in self.hero_actions:
            if not action['used'] and now-action['at']>=delay:
                card=action['card']
                self.select_slot_pos(card['x'],card['y'])
                action['used']=True
                self._log(f"[Hero] Dùng kỹ năng {card['name']} sau {delay:g}s từ lần thả.")

    def _wait(self,seconds):
        until=time.monotonic()+max(0,seconds)
        while time.monotonic()<until:
            self._abilities()
            time.sleep(min(.15,max(0,until-time.monotonic())))

    def hero_deployed(self,screen,card):
        if screen is None:return False
        x=int(card['x'])
        area=screen[713:737,max(60,x-50):min(1540,x+50)]
        hsv=cv2.cvtColor(area,cv2.COLOR_BGR2HSV)
        mask=cv2.inRange(hsv,(8 if card.get('name')=='luke' else 32,100,100),(85,255,255))
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            bx,by,w,h=cv2.boundingRect(contour)
            if w>=25 and 4<=h<=12 and by>=(0 if card.get('name')=='luke' else 4) and by+h<=21 and cv2.contourArea(contour)/(w*h)>.65:
                return True
        return False

    def _drops(self,points,spell=False):
        self._control()
        slot = getattr(self, '_selected_slot', None)
        def before_chunk():
            # Service due skills between bounded batches, then restore the troop
            # or spell card before any deployment tap can hit the battlefield.
            self._abilities()
            if slot is not None and getattr(self, '_selected_slot', None) != slot:
                self.select_slot_pos(*slot)
        self.adb.tap_batch(points,jitter=0,interval_ms=self.settings.get('troop_drop_delay_ms',80),
                           delay_after=.03,before_chunk=before_chunk)
        self._abilities()

    def _home_button(self,screen):
        for name in ('return_home','okhome'):
            match=self.vision.match_template(screen,name,.75,roi=(450,600,700,280))
            if match:return match
        return None

    def _stars(self,screen):
        # 1star.png and 3star.png in the source assets are buttons, not star counters.
        template=self.vision.load_template('2star')
        if template is None or screen is None:return 0
        single=template[:,:template.shape[1]//2]
        # Only the earned-star row beside Overall Damage, excluding battle
        # objects, popups, and the damage numerals at the right.
        roi=screen[650:710,1280:1495]
        if roi.shape[0]<single.shape[0]:return 0
        response=cv2.matchTemplate(roi,single,cv2.TM_CCOEFF_NORMED)
        centers=[]
        for _ in range(3):
            _,score,_,location=cv2.minMaxLoc(response)
            if score<.83:break
            x,y=location
            centers.append((x,y))
            response[max(0,y-20):y+21,max(0,x-25):x+26]=-1
        return len(centers)

    def _update_loot(self,screen):
        if not self._loot_initial:return
        loot=self.vision.read_loot(screen)
        if isinstance(loot,dict):
            for key in ('gold','elixir','dark_elixir'):
                if not self._loot_initial.get('known',{}).get(key,True) or not loot.get('known',{}).get(key,True):continue
                before=self._loot_initial.get(key)
                after=loot.get(key)
                if isinstance(before,int) and isinstance(after,int) and 0<=after<=before:
                    self._loot_gained[key]=before-after
        return loot

    def refresh_initial_loot(self,screen):
        """Recheck the scouting read after zoom, before any deployment taps."""
        if not self._loot_initial:return
        first=self.vision.read_loot(screen)
        second=self.vision.read_loot(self.adb.screencap())
        for key in ('gold','elixir','dark_elixir'):
            a=first.get(key);b=second.get(key)
            known=(first.get('known',{}).get(key,False) and second.get('known',{}).get(key,False)
                   and type(a) is int and type(b) is int and a==b)
            self._loot_initial.setdefault('known',{})[key]=bool(known)
            if known:
                old=self._loot_initial.get(key)
                self._loot_initial[key]=b
                if old!=b:self._log(f'[Loot] Đối chiếu trước thả: {key} {old} → {b} qua hai ảnh; dùng mốc đã kiểm tra để tính loot/RPH.')
            else:
                self._log(f'[Loot] Chưa xác minh mốc {key} trước thả; không cộng loot/RPH từ số OCR chưa rõ.')

    def _result(self,stars,completed,surrendered=False,reason=''):
        if self._ultra_active and self._ultra_trace is not None:
            self._ultra_trace.update(completed=completed,reason=reason,time_to_50_ms=self._time_to_50_ms)
            self.save_ultra_trace()
        if completed or reason in ('not_scouting','unrecognized_deck_or_boundary'):
            self._active_fight=False
            if self.village_state is not None:
                self.village_state.clear_battle('main')
        return dict(stars=stars,duration=max(0,time.monotonic()-self.battle_start_time),completed=completed,
                    surrendered=surrendered,reason=reason,loot_gained=dict(self._loot_gained),
                    destruction_percent=self._last_destruction,loot_percent=self._last_loot_percent,
                    time_to_50_ms=self._time_to_50_ms if self._timing_valid else 0)

    def _surrender_and_exit(self,log_callback=None):
        log=log_callback or self._log
        screen=self.adb.screencap()
        home=self._home_button(screen)
        if home:
            self.adb.tap(home[0],home[1],jitter=0,delay_after=1.5)
            return True
        if self.vision.battle_phase(screen) not in ('scouting','combat'):
            log('[Exit] Chưa xác nhận màn hình trận; không bấm tọa độ đoán.')
            return False
        # The locally named 1star image is the observed End Battle button.
        end=self.vision.match_template(screen,'1star',.80,roi=(15,600,230,120))
        if not end:end=self.vision.match_template(screen,'surrender',.75,roi=(15,600,230,120))
        if not end:
            log('[Exit] Không tìm thấy nút kết thúc trận.')
            return False
        self.adb.tap(end[0],end[1],jitter=0,delay_after=.5)
        deadline=time.monotonic()+12
        confirmed=False
        while time.monotonic()<deadline:
            self._control()
            screen=self.adb.screencap()
            home=self._home_button(screen)
            if home:
                self.adb.tap(home[0],home[1],jitter=0,delay_after=1.5)
                log('[Exit] Đã thấy kết quả và bấm về làng.')
                return True
            if not confirmed:
                match=self.vision.match_template(screen,'okay',.75,roi=(750,450,450,250))
                if match:
                    self.adb.tap(match[0],match[1],jitter=0,delay_after=.6)
                    confirmed=True
            time.sleep(.25)
        log('[Exit] Chưa xác minh được kết quả trận; giữ trạng thái để kiểm tra lại.')
        return False

    def _cast_pending_spells(self, screen=None):
        now=time.monotonic()
        first_delay=.3 if self._ultra_active else float(self.settings.get('spell_first_delay_sec',1))
        if now-self.battle_start_time<first_delay:return
        gap=.4 if self._ultra_active else max(0,float(self.settings.get('spell_wave_gap_sec',2)))
        if now-self.last_spell_at<gap or not any(card['count']>0 for card in self.pending_spells):return
        if screen is None:
            screen=self.adb.screencap()
        if screen is None or self._home_button(screen):return
        if self.vision.battle_phase(screen)!='combat':return
        if not self.targeter.previous:
            self.targeter.target(screen,'no',now)
        for card in self.pending_spells:
            if card['count']<=0:continue
            if card['name'] not in ('no','angry_spell','haste_spell','healing_spell','bang','ice_block_spell','poison_spell'):
                continue
            now=time.monotonic()
            target=self.targeter.target(screen,card['name'],now)
            if target is None:continue
            self._abilities()
            if self._ultra_active:
                self.send_ultra_blocks([dict(card=card,points=[target])])
            else:
                self.select_slot_pos(card['x'],card['y'])
                self._drops([target],spell=True)
            card['count']-=1
            self.targeter.mark(target,card['name'],now)
            self.last_spell_at=now
            self._log(f"[Spell] {card['name']} tại {target}, theo cụm quân đã nhận diện; còn {card['count']}.")

    def cast_opening_support(self,entries):
        if self._opening_support_sent or not entries:return
        now=time.monotonic()
        sent=0
        for card in self.pending_spells:
            if sent>=2:break
            if card['count']<=0 or card['name'] not in ('no','angry_spell','haste_spell'):continue
            for entry in entries:
                target=self._prepared_spell_targets.get(entry)
                if target is None:continue
                group=SpellTargeter.effect_group(card['name'])
                if any(SpellTargeter.effect_group(zone['spell'])==group and
                       (target[0]-zone['point'][0])**2+(target[1]-zone['point'][1])**2<90**2
                       for zone in self.targeter.active if zone['until']>now):continue
                self.select_slot_pos(card['x'],card['y']);self._drops([target],spell=True)
                card['count']-=1;sent+=1
                self.targeter.mark(target,card['name'],now);self.last_spell_at=now
                self._log(f'[Spell sớm] {card["name"]} tại {target}, điểm đã tính từ vùng thả {entry}.')
                break
        if sent:self._opening_support_sent=True

    def _start_live_battle(self, *, recovered=False):
        if self.village_state is not None:
            self.village_state.start_battle('main', self.battle_start_time, recovered=recovered)

    def _monitor(self,log_callback=None,screen_callback=None):
        log=log_callback or self._log
        policy=self._end_policy or BattleEndPolicy.from_settings(self.settings)
        self._end_policy=policy
        deadline=self.battle_start_time+policy.time_limit
        if not (policy.enabled and policy.time_enabled):
            deadline=max(self.battle_start_time+240,time.monotonic()+30)
        stars=0;last_report=-1;last_loot=-5;last_destruction=-5
        if self.village_state is not None:
            if not self.village_state.live_battle['main']:
                self._start_live_battle(recovered=True)
            stars = self.village_state.live_battle['main']['stars']
        while time.monotonic()<deadline:
            self._abilities()
            screen=self.adb.screencap()
            if screen is None:
                self._wait(.5);continue
            if screen_callback:screen_callback(screen)
            home=self._home_button(screen)
            if home:
                self.adb.tap(home[0],home[1],jitter=0,delay_after=1.5)
                log(f'[Combat] Đã thấy kết quả trận; sao xác nhận: {stars}.')
                return self._result(stars,True)
            stars=max(stars,self._stars(screen))
            if self.village_state is not None:
                self.village_state.update_battle('main', stars)
            now=time.monotonic()
            if (not self._ultra_active or policy.enabled and policy.loot_enabled) and now-last_loot >= (1.5 if policy.enabled and policy.loot_enabled else 5):
                remaining=self._update_loot(screen);last_loot=now
                if policy.loot_enabled:
                    self._last_loot_percent=policy.loot_percent(self._loot_initial,remaining)
                    policy.observe_loot(self._last_loot_percent)
            if (self._timing_valid or policy.enabled and (policy.destruction_enabled or policy.stars_enabled)) and now-last_destruction>=(.25 if self._ultra_active else .6):
                percent=self.vision.read_destruction_percent(screen)
                self._last_destruction=percent if type(percent) is int and 0<=percent<=100 else None
                policy.observe_destruction(self._last_destruction)
                policy.observe_stars(stars,self._last_destruction)
                self._fifty_streak=self._fifty_streak+1 if self._last_destruction is not None and self._last_destruction>=50 else 0
                if self._last_destruction is not None and self._last_destruction<50:
                    self._record_seen_below_50=True
                if self._timing_valid and not self._time_to_50_ms and self._fifty_streak>=2:
                    measured=max(1,round((time.monotonic()-self.battle_start_time)*1000))
                    if self._record_seen_below_50 and measured>=5000:
                        self._time_to_50_ms=measured
                        log(f'[Combat] Đã xác minh 50% sau {measured/1000:.2f}s.')
                    else:
                        log('[Kỷ lục] Mốc 50% quá sớm hoặc thiếu diễn biến trước 50%; không ghi kỷ lục lượt này.')
                last_destruction=now
            target=policy.stars_target
            reason=policy.reason(stars,now-self.battle_start_time)
            if reason:
                labels={'star_target':f'đạt {policy.eligible_stars(stars)}/{target} sao theo nguồn đã chọn (tổng {stars}; nhà chính: {"đã xác minh" if policy.town_hall_destroyed else "chưa xác minh"})',
                        'loot_target':f'đạt {policy.loot_target}% tài nguyên đã chọn',
                        'destruction_target':f'đạt {policy.destruction_target}% phá hủy',
                        'combined_targets':'đạt tất cả mục tiêu đã tích',
                        'timeout':f'đạt giới hạn {policy.time_limit} giây'}
                log(f'[Combat] Kết thúc: {labels[reason]}.')
                done=self._surrender_and_exit(log)
                self._update_loot(screen)
                return self._result(stars,done,done,reason)
            self._cast_pending_spells(screen=screen)
            elapsed=int(time.monotonic()-self.battle_start_time)
            if elapsed//10!=last_report:
                last_report=elapsed//10
                details=f'[Combat] {elapsed}s | {stars}/{target} sao'
                if policy.loot_enabled:
                    details += ' | Farm '+(f'{self._last_loot_percent:.1f}/{policy.loot_target}%' if self._last_loot_percent is not None else 'chưa đọc đủ dữ liệu')
                if policy.destruction_enabled:
                    details += f' | Phá hủy {self._last_destruction if self._last_destruction is not None else "—"}/{policy.destruction_target}%'
                log(details)
            self._wait(.35 if self._ultra_active else .6)
        if policy.enabled and policy.time_enabled:
            done=self._surrender_and_exit(log)
            return self._result(stars,done,done,'timeout')
        log('[Combat] Hết khoảng theo dõi; tiếp tục chờ kết quả ở vòng sau theo cài đặt không tự rút quân.')
        return self._result(stars,False,reason='monitor_timeout')

    def monitor_existing_battle(self,log_callback=None,screen_callback=None):
        self._log=log_callback or (lambda message:None)
        if self.settings.get('attack_strategy') == FixedAttackPlan.KEY:
            if not self._active_fight:
                self.battle_start_time = time.monotonic()
                self._active_fight = True
                self._deployment_pending = False
                self._ultra_active = True
                self._timing_valid = False
                self._ultra_skill_slots = []
                self._ultra_skill_repeats = 0
                self._ultra_last_skill = self.battle_start_time
                self._ultra_trace = None
                self._end_policy = BattleEndPolicy.from_settings(self.settings)
                self._loot_initial = None
                self._loot_gained = {}
                self.pending_spells = []
                self._last_destruction = self._last_loot_percent = None
                self._start_live_battle(recovered=True)
                self._fixed_audit_pending = True
            if self._fixed_audit_pending:
                self._fixed_audit_pending = False
                profile = FixedAttackPlan.load(self.settings, self._fixed_serial())
                if profile is not None:
                    self._audit_fixed(profile)
            return self._monitor(self._log, screen_callback or self.screen_callback)
        if not self._active_fight or self._deployment_pending:
            screen=self.adb.screencap()
            percent=self.vision.read_destruction_percent(screen)
            if type(percent) is int and percent==0:
                fresh=self.adb.screencap()
                second=self.vision.read_destruction_percent(fresh)
                if type(second) is int and second==0 and self.vision.battle_phase(fresh)=='combat':
                    cards=self.scan_deck_cards(fresh)
                    if cards.get('troops') and not any(self.hero_deployed(fresh,c) for c in cards.get('heroes',[])):
                        if self._deployment_recoveries<2:
                            self._deployment_recoveries+=1
                            self._log('[Recovery] Trận 0%, còn quân chưa thả: quét lại và triển khai, không đứng chờ kết quả.')
                            self.targeter=SpellTargeter();self.last_spell_at=0;self.hero_actions=[]
                            self.battle_start_time=time.monotonic()
                            return self.execute_ultra(fresh,screen_callback or self.screen_callback,recovered=True)
                        self._log('[Recovery] Hai lần phục hồi triển khai chưa thành công; kết thúc trận để tìm nhà khác.')
                        self.battle_start_time=time.monotonic()
                        done=self._surrender_and_exit(self._log)
                        self._deployment_pending=False
                        return self._result(0,done,done,'deployment_failed')
        if not self._active_fight:
            self._ultra_active=False;self._timing_valid=False;self._time_to_50_ms=0;self._fifty_streak=0
            self.battle_start_time=time.monotonic()
            self.hero_actions=[];self._loot_initial=None;self._loot_gained={}
            self._end_policy=BattleEndPolicy.from_settings(self.settings)
            if self.settings.get('attack_strategy','perimeter')=='ultra_fast':
                self._ultra_active=True;self._ultra_skill_slots=[];self._ultra_skill_repeats=3
                self._end_policy=self.ultra_policy()
            self._last_destruction=None;self._last_loot_percent=None
            self._active_fight=True
        return self._monitor(self._log,screen_callback or self.screen_callback)

    def ultra_policy(self):
        return BattleEndPolicy.from_settings(self.settings)

    def send_ultra_blocks(self,blocks,retry=False):
        gap=max(5,min(100,int(self.settings.get('ultra_drop_gap_ms',20))))
        select=max(40,min(200,int(self.settings.get('ultra_card_select_ms',80))))/1000
        if retry:gap=max(35,gap);select=max(.08,select)
        for block in blocks:
            self._control()
            card=block['card']
            self.adb.tap(card['x'],card['y'],jitter=0,delay_after=select)
            self._selected_slot=(card['x'],card['y'])
            self.adb.tap_batch(block['points'],jitter=0,interval_ms=gap,delay_after=.015)
            if block.get('planned') and block.get('kind')=='spells':
                for pending in self.pending_spells:
                    if pending['name']==card['name'] and pending['x']==card['x']:
                        pending['count']=max(0,pending['count']-len(block['points']));break
                for target in block['points']:
                    self.targeter.mark(target,card['name'],time.monotonic())
                self.last_spell_at=time.monotonic()
                if self._ultra_trace is not None:
                    self._ultra_trace.setdefault('early_spells_sent',[]).append(dict(name=card['name'],points=block['points'],
                                                                                  seconds=round(self.last_spell_at-self.battle_start_time,3)))
                self._log(f'[Spell sớm] {card["name"]} tại {block["points"]}, xen sau nhóm lính mở đầu/nhóm lính kế tiếp.')

    def verify_ultra_heroes(self,screen,heroes):
        changed=False
        for card in heroes:
            if not self.settings.get('use_'+card['name'],True):continue
            if self.hero_deployed(screen,card) and self.settings.get('trigger_hero_abilities',True):
                slot=(card['x'],card['y'])
                if slot not in self._ultra_skill_slots:
                    self._ultra_skill_slots.append(slot);changed=True
        if changed:self._ultra_skill_repeats=0
        self._abilities()

    def cleanup_ultra(self,original,points):
        """Audit whole bursts, then retry only observed leftovers, at most twice."""
        last={};confirmed_points=[]
        for check in range(3):
            frame=self.adb.screencap()
            if frame is None or self._home_button(frame) or self.vision.battle_phase(frame) not in ('scouting','combat'):
                break
            percent=self.vision.read_destruction_percent(frame)
            if type(percent) is int and 0<=percent<50:
                self._record_seen_below_50=True
            self.verify_ultra_heroes(frame,original['heroes'])
            remaining=self.scan_deck_cards(frame)
            self.pending_spells=[dict(card) for card in remaining['spells']]
            heroes=[card for card in remaining['heroes'] if self.settings.get('use_'+card['name'],True)
                    and not self.hero_deployed(frame,card)]
            last=dict(troops=[(card['name'],card['count']) for card in remaining['troops']],
                      spells=[(card['name'],card['count']) for card in remaining['spells']],
                      heroes=[card['name'] for card in heroes],checks=check+1)
            if not remaining['troops'] and not remaining['spells'] and not heroes:
                self._log('[Dự phòng nhanh] Không đọc thấy quân/phép còn số lượng hoặc tướng chưa xuất trận.')
                break
            if check==2:
                self._log(f'[Dự phòng nhanh] Sau 2 lượt còn {last}; tiếp tục theo dõi trận, không lặp vô hạn.')
                break
            self._log(f'[Dự phòng nhanh] Lượt {check+1}: quân {last["troops"]}, phép {last["spells"]}, tướng {last["heroes"]}.')
            shifted=points[(check+1)%len(points):]+points[:(check+1)%len(points)]
            for card in remaining['troops']:
                legal,left=self._verify_drop_points(card,shifted,confirmed_points[:1])
                for point in legal:
                    if point not in confirmed_points:confirmed_points.append(point)
                if legal and left:
                    self.send_ultra_blocks([dict(card=card,points=[legal[i%len(legal)] for i in range(left)])],retry=True)
            hero_points=confirmed_points or shifted
            for i,card in enumerate(heroes):
                for attempt in range(2):
                    self.send_ultra_blocks([dict(card=card,points=[hero_points[(i+attempt)%len(hero_points)]])],retry=True)
                    time.sleep(.15)
                    fresh=self.adb.screencap()
                    if not self.hero_deployed(fresh,card):
                        time.sleep(.15);fresh=self.adb.screencap()
                    if self.hero_deployed(fresh,card):
                        self.verify_ultra_heroes(fresh,[card])
                        self._log(f'[Dự phòng nhanh] Đã xác minh {card["name"]} xuất trận.')
                        break
            self._cast_pending_spells(screen=frame)
        if self._ultra_trace is not None:
            self._ultra_trace['cleanup']=last
            self.save_ultra_trace()

    def execute_ultra(self,screen,callback=None,recovered=False):
        preparation_started=time.monotonic()
        self._ultra_trace=None
        self._ultra_active=True
        self._ultra_skill_slots=[];self._ultra_skill_repeats=0
        self._timing_valid=False
        self.targeter.max_lead=max(15,min(60,int(self.settings.get('ultra_spell_max_lead_px',40))))
        self.targeter.inward_bias=self.targeter.max_lead*max(5,min(50,int(self.settings.get('ultra_spell_inward_pct',20))))/100
        self._fifty_streak=0;self._time_to_50_ms=0
        self._record_seen_below_50=False
        self._end_policy=self.ultra_policy()
        self._log('[Siêu nhanh] Chuẩn bị: zoom, quét đội hình và ranh đỏ trước khi thả quân.')
        self.zoom_out_map(2)
        screen=self.adb.screencap()
        cards=self.scan_deck_cards(screen,self._log)
        if not cards.get('troops'):
            self._log('[Siêu nhanh] Chưa đọc rõ đội hình trong thời gian chuẩn bị; không thả đoán.')
            return self._result(0,False,reason='unrecognized_deck_or_boundary')
        if not recovered:self.refresh_initial_loot(screen)
        # Selecting the troop card reveals the current forbidden boundary but
        # does not start combat. Finish all scanning before the burst begins.
        main=cards['troops'][0]
        self.select_slot_pos(main['x'],main['y'])
        previous=[];stable=[];observed=[]
        for _ in range(4):
            fresh=self.adb.screencap()
            if fresh is None or self.vision.battle_phase(fresh) not in ('scouting','combat'):
                break
            candidates=self.planner.points(fresh,16)
            observed=candidates
            stable=[p for p in candidates if any((p[0]-q[0])**2+(p[1]-q[1])**2<=12**2 for q in previous)]
            if len(stable)>=4:
                screen=fresh
                break
            previous=candidates
            time.sleep(.12)
        points=UltraAttackPlan.scanned_points(stable,self.settings.get('ultra_fixed_positions',False))
        if len(points)<4:
            # Animated scenery can move Hough endpoints between frames. Probe
            # only latest observed candidates, then trust count decreases.
            legal=[]
            if observed:
                self._log('[Siêu nhanh] Ranh dao động; thử từng điểm vừa quét và xác minh số quân giảm trước khi thả tiếp.')
                self.battle_start_time=time.monotonic()
                legal,left=self._verify_drop_points(main,observed)
                main['count']=left
            if not legal:
                self._deployment_pending=True
                self._log('[Siêu nhanh] Chưa xác minh được vùng thả; sẽ phục hồi triển khai nếu trận bắt đầu, không chỉ đứng theo dõi.')
                return self._result(0,False,reason='unrecognized_deck_or_boundary')
            points=legal
            recovered=True  # Probe taps already started combat; no record.
            self._log(f'[Siêu nhanh] Đã xác minh {len(points)} điểm giảm số quân; dùng các điểm này cho phần còn lại.')
        self._deployment_pending=False
        points=UltraAttackPlan.diverse_points(points,self.combo_rotation_index)
        self.combo_rotation_index+=1
        UltraAttackPlan.remember(self.settings,cards,serial=self._fixed_serial())
        self._log(f'[Siêu nhanh] Đã quét {len(points)} điểm ngoài ranh đỏ ổn định; bắt đầu thả liên tục.')
        # Calculate the whole troop/spell timeline while still scouting. Early
        # support sits beside the intended entry, with short inward lead.
        blocks=UltraAttackPlan.blocks(cards,points,self.settings)
        self.pending_spells=[dict(card) for card in cards['spells']]
        commands=[point for block in blocks for point in [(block['card']['x'],block['card']['y'])]+block['points']]
        if isinstance(getattr(screen,'shape',None),tuple) and screen.shape[:2]==(900,1600):
            self._ultra_trace=dict(source='fresh_prebattle_scan',points=points,commands=len(commands),
                                   preparation_seconds=round(time.monotonic()-preparation_started,3),
                                   planned_spells=[dict(name=b['card']['name'],points=b['points']) for b in blocks if b.get('planned')])
            marked=screen.copy()
            for i,point in enumerate(points):
                cv2.circle(marked,point,9,(0,255,0),2)
                cv2.putText(marked,str(i),point,cv2.FONT_HERSHEY_SIMPLEX,.5,(0,255,0),1)
            try:
                path=Path(__file__).resolve().parents[1]/'supervision'/'runtime'/'ultra-prepared.png'
                path.parent.mkdir(exist_ok=True,parents=True);cv2.imwrite(str(path),marked)
            except (OSError,cv2.error):
                pass
            self.save_ultra_trace()
        self._log(f'[Siêu nhanh] {len(commands)} thao tác; chờ chọn thẻ và nhịp ngắn để game nhận đủ, không OCR giữa các lần thả.')
        self._ultra_skill_slots=[];self._ultra_skill_repeats=0
        phase=self.vision.battle_phase(screen)
        baseline=self.vision.read_destruction_percent(screen) if phase=='combat' else 0
        if self._ultra_trace is not None:
            self._ultra_trace.update(initial_phase=phase,initial_destruction=baseline)
        self.battle_start_time=time.monotonic();self._ultra_last_skill=self.battle_start_time
        self._active_fight=True;self._timing_valid=not recovered and (phase=='scouting' or baseline==0)
        self._start_live_battle(recovered=recovered)
        self.send_ultra_blocks(blocks)
        deployment_seconds=time.monotonic()-self.battle_start_time
        if self._ultra_trace is not None:
            self._ultra_trace['deployment_seconds']=round(deployment_seconds,3)
            self.save_ultra_trace()
        self._log(f'[Siêu nhanh] Đã gửi chuỗi triển khai trong {deployment_seconds:.2f}s; theo dõi mốc 50%.')
        cleanup_started=time.monotonic()
        self.cleanup_ultra(cards,points)
        if self._ultra_trace is not None:
            self._ultra_trace['cleanup_seconds']=round(time.monotonic()-cleanup_started,3)
            self.save_ultra_trace()
        return self._monitor(self._log,callback)

    def _verify_drop_points(self,card,points,preferred=None):
        """A count decrease proves that a point accepts this troop type."""
        remaining=card['count'];valid=[]
        priorities=list(preferred or [])+[points[i*len(points)//4] for i in range(4)]
        unique=[]
        for point in priorities:
            if point not in unique:unique.append(point)
        for point in unique[:4]:
            if remaining<=0:break
            self.select_slot_pos(card['x'],card['y'])
            self._drops([point])
            frame = self.adb.screencap()
            if frame is not None and getattr(self, '_live_callback', None):
                self._live_callback(frame)
            after=self.recognizer.read_count(frame,card['x'])
            if after is None or (after is not None and after<remaining-1):
                self._wait(.35)
                after=self.recognizer.read_count(self.adb.screencap(),card['x'],scale=3)
            if after is not None and after==remaining-1:
                valid.append(point)
                remaining=after
                if card['count']<=6:break
        self._log(f"[Deploy] {card['name']}: {len(valid)} điểm đã giảm số quân; còn {remaining}.")
        return valid,remaining

    def _spread_verified_card(self, card, points, legal, remaining):
        """Cover the requested perimeter in batches, checking actual count deltas."""
        candidates=[point for point in dict.fromkeys(points) if point not in legal]
        for offset in range(0,len(candidates),4):
            if remaining<=0:
                break
            batch=candidates[offset:offset+min(4,remaining)]
            self._abilities();self.select_slot_pos(card['x'],card['y'])
            self._drops(batch)
            frame=self.adb.screencap()
            after=self.recognizer.read_count(frame,card['x'])
            if after is None or not 0 <= after <= remaining:
                self._wait(.25)
                frame=self.adb.screencap()
                after=self.recognizer.read_count(frame,card['x'],scale=3)
            if after is None or not 0 <= remaining-after <= len(batch):
                self._log(f"[Deploy] Chưa xác minh số quân {card['name']} sau đợt rải; để lượt quét quân còn lại xử lý.")
                return legal,None
            if remaining-after==len(batch):
                legal.extend(batch)
            remaining=after
            self._cast_pending_spells(screen=frame)
        return legal,remaining

    def _deploy_resource_dragons(self, screen, card, legal_points):
        if self.resource_detector is None:
            self.resource_detector=ResourceTargetDetector(self.vision.asset_dir)
        buildings=self.resource_detector.detect(screen)
        targets=self.resource_detector.deployment_targets(buildings,legal_points,card['count'])
        self._log(f'[Resource] Nhận diện {len(buildings)} mỏ/kho; phân {len(targets)} mục tiêu, một rồng mỗi mục tiêu.')
        remaining=card['count']
        verified=[]
        for item in targets:
            if remaining<=0:
                break
            point=item['point']
            self._abilities();self.select_slot_pos(card['x'],card['y'])
            self._drops([point])
            frame=self.adb.screencap()
            after=self.recognizer.read_count(frame,card['x'])
            if after is None or after!=remaining-1:
                self._wait(.25)
                frame=self.adb.screencap()
                after=self.recognizer.read_count(frame,card['x'],scale=3)
            if after is None or not remaining-1<=after<=remaining:
                self._log('[Resource] Số rồng chưa rõ; dừng thả theo mục tiêu, để lượt quét quân còn lại kiểm tra.')
                return None,verified
            if after==remaining-1:
                remaining=after
                verified.append(point)
                building=item['building']
                self._log(f"[Resource] Một rồng gần {building['kind']} ({building['x']}, {building['y']}), điểm hợp lệ {point}; còn {remaining}.")
                self.cast_opening_support([point])
                self._cast_pending_spells(screen=frame)
        return remaining,verified

    def _fixed_serial(self):
        serial = getattr(self.adb, 'device_serial', None)
        return serial if isinstance(serial, str) else self.settings.get('device_serial')

    def _send_fixed_blocks(self, blocks):
        select = max(40, min(200, int(self.settings.get('ultra_card_select_ms', 80))))
        gap = max(5, min(100, int(self.settings.get('ultra_drop_gap_ms', 20))))
        first = blocks[0]['card']
        self.adb.tap(first['x'], first['y'], jitter=0, delay_after=select / 1000)
        points = list(blocks[0]['points'])
        delays = [gap] * len(points)
        for block in blocks[1:]:
            card = block['card']
            points.append((card['x'], card['y']))
            delays.append(select)
            points.extend(block['points'])
            delays.extend([gap] * len(block['points']))
        delays[-1] = 0
        started = time.monotonic()
        self.adb.tap_batch(points, jitter=0, step_delays_ms=delays, delay_after=0)
        return time.monotonic() - started

    def _audit_fixed(self, profile, original=None, opening=()):
        targets = {card['x']: tuple(profile['edges'][i][1])
                   for i, card in enumerate((original or {}).get('heroes', [])[:4])}
        sent = {}
        for block in opening:
            sent.setdefault(block['card']['x'], set()).update(block['points'])
        for check in range(2):
            frame = self.adb.screencap()
            if frame is None or self._home_button(frame) or self.vision.battle_phase(frame) not in ('scouting', 'combat'):
                return
            percent = self.vision.read_destruction_percent(frame)
            if type(percent) is int and 0 <= percent < 50:
                self._record_seen_below_50 = True
            observed = self.scan_deck_cards(frame)
            deployed = [card for card in observed['heroes'] if self.hero_deployed(frame, card)]
            self.verify_ultra_heroes(frame, deployed)
            left = dict(observed, heroes=[card for card in observed['heroes'] if card not in deployed
                                         and self.settings.get('use_' + card['name'], True)][:4])
            # Siege recognition has no remaining-count check. Re-selecting an
            # already deployed siege can release its troops prematurely.
            left['siege'] = []
            blocks = FixedAttackPlan.blocks(profile, left, retry=True, hero_targets=targets, sent=sent)
            if not blocks:
                self._log('[Rải cố định] Quét sau thả: không đọc thấy thẻ cần vét.')
                return
            labels = ', '.join(f"{block['card']['name']} ×{len(block['points'])}" for block in blocks)
            if check:
                self._log(f'[Rải cố định] Sau lượt vét vẫn nhận diện: {labels}. Tiếp tục theo dõi trận.')
                return
            self._log(f'[Rải cố định] Vét một lượt: {labels}.')
            self._send_fixed_blocks(blocks)

    def _execute_fixed(self, profile, callback):
        serial = self._fixed_serial()
        cards, source = FixedAttackPlan.cards(self.settings, profile, serial)
        use_siege = self.settings.get('use_siege', True)
        siege_mode = self.settings.get('siege_deploy_mode', 'always')
        if not use_siege:
            cards['siege'] = []
        elif siege_mode == 'require_cc':
            cards['siege'] = [c for c in cards.get('siege', []) if c.get('has_cc', True)]
        blocks = FixedAttackPlan.blocks(profile, cards)
        self._log(f'[Rải cố định] Dùng {source}; thu nhỏ và gửi {sum(len(b["points"]) for b in blocks)} lệnh thả.')
        started = time.monotonic()
        self.zoom_out_map(profile['zoom'])
        zoom_seconds = time.monotonic() - started
        self._ultra_active = True
        self._ultra_skill_slots = []
        self._ultra_skill_repeats = 0
        self._ultra_trace = None
        self._deployment_pending = False
        self._timing_valid = True
        self._time_to_50_ms = self._fifty_streak = 0
        self._record_seen_below_50 = False
        self._active_fight = True
        self.battle_start_time = time.monotonic()
        self._ultra_last_skill = self.battle_start_time
        self._start_live_battle()
        self._fixed_audit_pending = True
        drop_seconds = self._send_fixed_blocks(blocks)
        self._fixed_audit_pending = False
        audit_started = time.monotonic()
        self._audit_fixed(profile, cards, blocks)
        audit_seconds = time.monotonic() - audit_started
        self._log(f'[Rải cố định] Zoom {zoom_seconds:.2f}s · thả {drop_seconds:.2f}s · kiểm tra/vét {audit_seconds:.2f}s.')
        return self._monitor(self._log, callback)

    def execute_full_attack(self,log_callback=None,screen_callback=None,initial_loot=None,approved_scout=None):
        self._log=log_callback or (lambda message:None)
        callback=screen_callback or self.screen_callback
        self._live_callback=callback
        self.hero_actions=[];self._loot_initial=initial_loot;self._loot_gained={}
        self.pending_spells=[];self.targeter=SpellTargeter();self.last_spell_at=0
        self._verified_drop_points=[]
        self._troop_points_by_name={}
        self._opening_support_sent=False;self._prepared_spell_targets={}
        self._end_policy=BattleEndPolicy.from_settings(self.settings)
        self._last_destruction=None;self._last_loot_percent=None
        if self.settings.get('attack_strategy') == FixedAttackPlan.KEY:
            profile = FixedAttackPlan.load(self.settings, self._fixed_serial())
            if profile is None or getattr(approved_scout, 'shape', ())[:2] != (900, 1600):
                self._log('[Rải cố định] Cần hồ sơ hiệu chuẩn và ảnh soi nhà đã đạt ngưỡng.')
                return self._result(0, False, reason='not_scouting')
            self.choose_attack_strategy()
            return self._execute_fixed(profile, callback)
        screen=self.adb.screencap()
        phase=self.vision.battle_phase(screen)
        if phase=='combat':return self.monitor_existing_battle(self._log,callback)
        self._ultra_active=False;self._timing_valid=False;self._time_to_50_ms=0;self._fifty_streak=0;self._record_seen_below_50=False
        self.battle_start_time=time.monotonic()
        if phase!='scouting':return self._result(0,False,reason='not_scouting')
        self._deployment_recoveries=0
        strategy=self.choose_attack_strategy()
        if strategy=='ultra_fast':
            return self.execute_ultra(screen,callback)
        self.zoom_out_map(2)
        screen=self.adb.screencap()
        if callback and screen is not None:callback(screen)
        self.refresh_initial_loot(screen)
        cards=self.scan_deck_cards(screen,self._log)
        UltraAttackPlan.remember(self.settings,cards,serial=self._fixed_serial())
        point_count=int(self.settings.get('attack_spread_points',16))
        points=self.planner.points(screen,max(32,point_count) if strategy=='resource_dragons' else point_count)
        if not cards['troops'] or not points:
            self._log('[Combat] Chưa xác nhận quân hoặc ranh thả; không triển khai.')
            return self._result(0,False,reason='unrecognized_deck_or_boundary')
        base_points=points
        rotation=self.combo_rotation_index
        effective=self.settings.get('resource_attack_fallback','perimeter') if strategy=='resource_dragons' else strategy
        if effective not in ('perimeter','four_corners','smart_wave','hybrid'):
            effective='perimeter'
        points=strategy_points(base_points,'four_corners' if effective=='hybrid' else effective,rotation)
        self.combo_rotation_index+=1
        self._log(f'[Combat] Chiến thuật {strategy}; {len(points)} điểm ngoài ranh đỏ.')
        self.battle_start_time=time.monotonic()
        self._timing_valid=True
        self._active_fight=True
        self._start_live_battle()
        self.pending_spells=[dict(card) for card in cards['spells']]
        self._prepared_spell_targets={point:UltraAttackPlan.spell_points([point],1,self.settings)[0] for point in base_points}
        if strategy=='resource_dragons':
            dragon=next((card for card in cards['troops'] if card['name']=='dragon'),None)
            if dragon is None:
                self._log('[Resource] Chưa nhận diện rồng thường; chuyển sang cách đánh bổ sung.')
            else:
                left,verified=self._deploy_resource_dragons(screen,dragon,base_points)
                self._verified_drop_points.extend(verified)
                cards['troops']=[dict(card,count=left) if card is dragon else card for card in cards['troops']
                                 if card is not dragon or left is not None and left>0]
        preferred=[]
        main=max(cards['troops'],key=lambda card:card['count']) if cards['troops'] else None
        troops=sorted(cards['troops'],key=lambda card:card is not main) if effective=='hybrid' else cards['troops']
        heroes_started=False
        for card in troops:
            card_points=(strategy_points(base_points,'perimeter',rotation)
                         if effective=='hybrid' and card is not main else points)
            legal,remaining=self._verify_drop_points(card,card_points,preferred if effective!='hybrid' else None)
            if not legal:
                self._log(f"[Deploy] Chưa đọc chắc số quân {card['name']}; chuyển thẻ này sang lượt quét dự phòng.")
                continue
            preferred=legal
            if not heroes_started:
                self.cast_opening_support(legal)
                self.deploy_siege_machines(cards['siege'], points, preferred=legal)
                self.deploy_heroes_early(cards['heroes'],points)
                heroes_started=True
            legal,remaining=self._spread_verified_card(card,card_points,legal,remaining)
            self._troop_points_by_name[card['name']]=list(legal)
            if remaining is not None and remaining>0:
                self._abilities();self.select_slot_pos(card['x'],card['y'])
                self._drops([legal[i%len(legal)] for i in range(remaining)])
            self._log(f"[Troop] {card['name']}: {len(legal)}/{len(card_points)} điểm đã xác minh.")
            # The scouting frame predates deployment and would suppress spells.
            self._cast_pending_spells()
        if not heroes_started:
            self.deploy_siege_machines(cards['siege'], points, preferred=preferred)
            self.deploy_heroes_early(cards['heroes'],points)
        self._cast_pending_spells()
        self.run_backup_sweep(self._log,callback,points)
        return self._monitor(self._log,callback)

    def choose_attack_strategy(self):
        strategy=self.settings.get('attack_strategy','perimeter')
        if strategy=='random_mix':
            choices=['perimeter','four_corners','smart_wave','hybrid','resource_dragons','ultra_fast']
            strategy=random.choice([name for name in choices if name!=self._last_random_strategy])
            self._last_random_strategy=strategy
            self._log(f'[Ngẫu nhiên] Chiến thuật trận này: {strategy}.')
        if getattr(self, 'village_state', None) is not None:
            self.village_state.update_attack_strategy('main', {
                'strategy': strategy,
                'target_stars': self.settings.get('star_target', 1),
                'spread_points': self.settings.get('attack_spread_points', 17)
            })
        return strategy

    def deploy_siege_machines(self, siege_cards, points, preferred=None):
        if not self.settings.get('use_siege', True):
            return
        mode = self.settings.get('siege_deploy_mode', 'always')
        drop_list = (preferred if preferred else points) or []
        if not drop_list:
            return
        target = drop_list[0]
        for siege in siege_cards:
            if mode == 'require_cc' and not siege.get('has_cc', True):
                self._log('[Siege] Bỏ qua cỗ máy do nhà Clan chưa có lính (chế độ: chỉ thả khi có lính Clan).')
                continue
            self._abilities()
            self.select_slot_pos(siege['x'], siege['y'])
            self._wait(0.12)
            self._drops([target])
            self._verified_drop_points.append(target)
            label = siege.get('label', siege.get('name', 'cỗ máy'))
            cc_status = "đầy đủ lính Clan" if siege.get('has_cc', True) else "không có lính Clan"
            self._log(f"[Siege] Đã xuất trận {label} ({cc_status}) tại {target}.")

    def deploy_heroes_early(self,heroes,points):
        for card in heroes:
            if not self.settings.get('use_'+card['name'],True):continue
            deployed=False
            start=len(self.hero_actions)*len(points)//4
            for retry in range(4):
                self._abilities();self.select_slot_pos(card['x'],card['y'])
                target=points[(start+retry*max(1,len(points)//4))%len(points)]
                attempted_at=time.monotonic()
                self._drops([target])
                fresh=self.adb.screencap()
                if not self.hero_deployed(fresh,card):
                    self._wait(.6)
                    fresh=self.adb.screencap()
                if self.hero_deployed(fresh,card):
                    self.hero_actions.append(dict(card=card,at=attempted_at,used=False))
                    self._abilities()
                    self._verified_drop_points.append(target)
                    self._log(f"[Hero] Đã xác minh {card['name']} xuất trận tại {target}.")
                    deployed=True
                    break
            if not deployed:self._log(f"[Hero] Chưa xác minh {card['name']} xuất trận; không tự kích hoạt kỹ năng.")

    def run_backup_sweep(self,log_callback=None,screen_callback=None,points=None):
        log=log_callback or self._log
        screen=self.adb.screencap()
        if screen is None or self._home_button(screen):return
        cards=self.scan_deck_cards(screen)
        remaining=cards['troops']
        if not remaining:
            log('[Deck] Không đọc được thẻ còn quân/phép sau triển khai.');return
        log('[Deck] Còn: '+', '.join(f"{c['name']} x{c['count']}" for c in remaining))
        points=getattr(self,'_verified_drop_points',[]) or points or self.planner.points(screen)
        if not points:return
        for card in remaining:
            self._abilities();self.select_slot_pos(card['x'],card['y'])
            card_points=self._troop_points_by_name.get(card['name']) or points
            self._drops([card_points[i%len(card_points)] for i in range(card['count'])])
        after=self.scan_deck_cards(self.adb.screencap())
        leftovers=after['troops']+after['spells']
        log('[Deck] Sau kiểm tra: '+(', '.join(f"{c['name']} x{c['count']}" for c in leftovers) if leftovers else 'không đọc thấy thẻ còn số lượng.'))
