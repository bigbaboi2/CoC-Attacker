"""Bounded Clan Castle Treasury collection; never opens gem purchase flows."""
import re
import time
import unicodedata
import cv2
import json
from pathlib import Path
from core.adb_client import BotStoppedError
from core.resource_targets import ResourceTargetDetector


class TreasuryCollector:
    def __init__(self, manager):
        self.manager=manager
        self.adb=manager.adb
        self.vision=manager.vision
        self.detector=ResourceTargetDetector(self.vision.asset_dir)
        config_path=getattr(manager.settings,'filepath',None)
        self.memory_path=Path(config_path).resolve().with_name('treasury_state.json') if isinstance(config_path,(str,Path)) else None
        self.memory={}
        if self.memory_path:
            try:
                saved=json.loads(self.memory_path.read_text(encoding='utf-8'))
                if saved.get('device_serial')==manager.settings.get('device_serial'):
                    values=saved.get('state',{})
                    for key,value in values.items():
                        if key.endswith('_at') or key.endswith('_after'):
                            if type(value) in (int,float) and 0<=value<time.time()+31*86400:
                                self.memory[key]=value
                        elif key in ('star_reward_pending','war_reward_pending','bonus_full_seen') and type(value) is bool:
                            self.memory[key]=value
                        elif key=='bonus_stars' and type(value) is int and 0<=value<=5:
                            self.memory[key]=value
                        elif key=='last_result' and type(value) is str:
                            self.memory[key]=value
            except (OSError,ValueError,TypeError,AttributeError):
                pass
        retry=self.memory.get('next_check_at',0)
        if type(retry) in (int,float) and time.time()<retry<time.time()+31*86400:
            manager._treasury_next_check=retry

    def save_memory(self):
        if self.memory_path:
            from core.stats_store import StatsStore
            payload=dict(device_serial=self.manager.settings.get('device_serial'),state=self.memory)
            try:
                StatsStore._atomic_write(self.memory_path,json.dumps(payload,ensure_ascii=False,indent=2,default=StatsStore.json_default).encode('utf-8'))
            except (OSError, TypeError, ValueError):
                pass # A storage failure must not stop farming or trigger input.

    @staticmethod
    def duration(text):
        text=text.lower().replace(' ','')
        parts=re.findall(r'(\d{1,3})([dhms])',text)
        seconds=sum(int(number)*dict(d=86400,h=3600,m=60,s=1)[unit] for number,unit in parts)
        return seconds if parts and 0<seconds<=31*86400 else None

    def observe_home(self,screen,log=None):
        if not self.manager.settings.get('treasury_war_rewards_enabled',True):
            return
        now=time.time()
        if now<self.memory.get('war_read_after',0):
            return
        self.memory['war_read_after']=now+300
        words=self.words(screen,(15,585,175,55))
        seconds=self.duration(' '.join(text for text,x,y in words))
        if seconds:
            due=now+seconds+60 # Minute OCR rounds down; wait beyond that minute.
            old=self.memory.get('war_due_at',0)
            if old and old<=now:
                self.memory['war_reward_pending']=True
            self.memory['war_due_at']=due
            # A visible war countdown is a check time, not proof of a reward.
            if self.memory.get('last_result') in ('collected','collect_unavailable') and not self.memory.get('war_reward_pending'):
                self.manager._treasury_next_check=due
                self.memory['next_check_at']=due
            if log and (not old or abs(old-due)>180):
                log(f'[Treasury] Hội chiến còn {seconds//3600}h {(seconds%3600)//60}m; đã lưu mốc kiểm tra thưởng.')
        self.save_memory()

    @staticmethod
    def normalized(text):
        text=unicodedata.normalize('NFD',text.lower().replace('đ','d'))
        return ''.join(char for char in text if unicodedata.category(char)!='Mn')

    def words(self, screen, roi):
        if screen is None:
            return []
        x,y,w,h=roi
        ocr=self.vision._get_ocr()
        if ocr is None:
            return []
        result=ocr(screen[y:y+h,x:x+w],use_det=True)
        texts=getattr(result,'txts',None);boxes=getattr(result,'boxes',None)
        if texts is None or boxes is None:
            return []
        return [(self.normalized(text),x+round(box[:,0].mean()),y+round(box[:,1].mean()))
                for text,box in zip(texts,boxes)]

    def treasury_button(self, screen):
        words=self.words(screen,(150,500,1250,370))
        title=' '.join(text for text,x,y in words)
        # The game's outlined font makes the lower-case l look like i to OCR.
        # Accept this specific title variant only beside an exact Treasury label.
        if not (re.search(r'\b(?:clan|cian)\s+castle\b',title) or 'lau dai bang hoi' in title):
            return None
        return next(((x,y) for text,x,y in words if text.strip() in ('treasury','kho bac')),None)

    def open_castle(self, screen):
        button=self.treasury_button(screen)
        if button:
            return screen,button
        self.adb.zoom_out_map(times=2)
        time.sleep(.3)
        screen=self.adb.screencap()
        if not self.vision.is_in_main_village(screen):
            return None,None
        candidates=self.detector.detect(screen,kinds=('clan_castle',),threshold=.8)
        positions=[(item['x'],item['y']) for item in candidates[:2]]
        if not positions:
            positions=[(x,min(690,y+55)) for text,x,y in self.words(screen,(200,140,1150,460))
                       if text.strip() in ('request','xin quan')][:2]
        for point in positions:
            self.adb.tap(*point,jitter=0,delay_after=.5)
            screen=self.adb.screencap()
            button=self.treasury_button(screen)
            if button:
                return screen,button
        return None,None

    def collect_button(self, screen):
        words=self.words(screen,(250,35,1100,820))
        if not any(text.strip() in ('treasury','kho bac') and y<250 for text,x,y in words):
            return None
        for text,x,y in words:
            if text.strip() not in ('collect','thu thap','nhan'):
                continue
            region=screen[max(0,y-28):min(900,y+28),max(0,x-95):min(1600,x+95)]
            green=cv2.inRange(cv2.cvtColor(region,cv2.COLOR_BGR2HSV),(25,90,90),(90,255,255))
            if (green>0).mean()>.2:
                return (x,y)
        return None

    def close(self):
        screen=self.adb.screencap()
        close=self.vision.find_close_button(screen)
        if close:
            self.adb.tap(*close[:2],jitter=0,delay_after=.4)

    def run(self, log=None):
        result=self._run(log)
        reason=result.get('reason')
        if reason not in ('disabled','cooldown','not_home'):
            now=time.time()
            self.memory.update(last_checked_at=now,last_result='collected' if result.get('verified') else reason)
            if result.get('verified'):
                self.memory['last_collected_at']=now
            if result.get('verified') or reason=='collect_unavailable':
                self.memory['star_reward_pending']=False
                war=self.memory.get('war_due_at',0) if self.manager.settings.get('treasury_war_rewards_enabled',True) else 0
                if war>now:
                    self.manager._treasury_next_check=war
            self.memory['next_check_at']=self.manager._treasury_next_check
            self.save_memory()
        return result

    def _run(self, log=None):
        log=log or (lambda message:None)
        now=time.time()
        if not self.manager.settings.get('auto_collect_treasury',False):
            return dict(verified=False,reason='disabled')
        star_enabled=self.manager.settings.get('treasury_star_bonus_enabled',True)
        war_enabled=self.manager.settings.get('treasury_war_rewards_enabled',True)
        if not star_enabled and not war_enabled:
            return dict(verified=False,reason='disabled')
        pending=(star_enabled and self.memory.get('star_reward_pending',False)
                 or war_enabled and self.memory.get('war_reward_pending',False))
        if self.memory.get('last_result') in ('collected','collect_unavailable') and not pending and not war_enabled:
            return dict(verified=False,reason='cooldown')
        if now<getattr(self.manager,'_treasury_next_check',0) and not pending:
            return dict(verified=False,reason='cooldown')
        minutes=max(1,int(self.manager.settings.get('treasury_check_interval_min',30)))
        self.manager._treasury_next_check=now+minutes*60
        self.memory['next_check_at']=self.manager._treasury_next_check
        # Consume a scheduled check before sending input. Uncertain outcomes
        # use the persisted retry time, including across a process restart.
        self.memory['star_reward_pending']=False
        self.memory['war_reward_pending']=False
        self.save_memory()
        screen=self.adb.screencap()
        if not self.vision.is_in_main_village(screen):
            return dict(verified=False,reason='not_home')
        before=self.vision.read_village_resources(screen)
        if not all(before.get('valid',{}).get(key,False) for key in ('gold','elixir','dark_elixir')):
            log('[Treasury] Chưa đọc đủ số dư trước thu; kiểm tra lại lượt sau.')
            return dict(verified=False,reason='unreadable_balance')
        try:
            _,button=self.open_castle(screen)
            if not button:
                log('[Treasury] Chưa xác minh được Clan Castle/nút Treasury; bỏ lượt thu.')
                return dict(verified=False,reason='castle_not_found')
            self.adb.tap(*button,jitter=0,delay_after=.6)
            collect=None
            for _ in range(6):
                screen=self.adb.screencap()
                collect=self.collect_button(screen)
                if collect:
                    break
                time.sleep(.25)
            if not collect:
                log('[Treasury] Không có nút Collect đang bật; kho bạc trống, kho đầy hoặc giao diện chưa rõ.')
                return dict(verified=False,reason='collect_unavailable')
            self.adb.tap(*collect,jitter=0,delay_after=.6)
            confirmed=False
            gains={};stable=0;previous=None
            for sample in range(10):
                screen=self.adb.screencap()
                if self.vision.is_in_main_village(screen):
                    after=self.vision.read_village_resources(screen)
                    if all(after.get('valid',{}).get(key,False) for key in ('gold','elixir','dark_elixir')):
                        gains={key:after[key]-before[key] for key in ('gold','elixir','dark_elixir')}
                        stable=stable+1 if gains==previous and any(value>0 for value in gains.values()) and all(value>=0 for value in gains.values()) else 0
                        previous=dict(gains)
                        if stable>=1:
                            log('[Treasury] Đã xác minh thu kho bạc: '+', '.join(f'{key} +{value:,}' for key,value in gains.items() if value>0))
                            return dict(verified=True,gains=gains)
                elif not confirmed:
                    words=self.words(screen,(350,170,900,530))
                    body=' '.join(text for text,x,y in words)
                    okay=None
                    if re.search(r'\b(gems?|buy|purchase|mua|ngoc)\b',body):
                        log('[Treasury] Giao diện có nội dung mua/ngọc; không xác nhận, bỏ lượt thu.')
                        break
                    if re.search(r'collect|treasury|kho bac|thu thap',body):
                        okay=next(((x,y) for text,x,y in words if text.strip() in ('okay','ok','confirm','xac nhan')),None)
                        if okay:
                            self.adb.tap(*okay,jitter=0,delay_after=.6)
                            confirmed=True
                    if sample>=2 and ('treasury' in body or 'kho bac' in body) and not okay:
                        self.close()
                elif confirmed:
                    self.close()
                time.sleep(.3)
            log('[Treasury] Chưa xác minh được số dư tăng; không bấm Collect lặp lại.')
            return dict(verified=False,reason='unverified_result')
        except BotStoppedError:
            raise
        except Exception as exc:
            log(f'[Treasury] Không hoàn tất thu ({type(exc).__name__}); bot tiếp tục.')
            return dict(verified=False,reason='error')
        finally:
            try:
                self.close()
            except BotStoppedError:
                raise
            except Exception:
                pass
