"""Observe the battle menu's Star Bonus row; no ranked-attack selection."""
import re
import time
import cv2
from core.adb_client import BotStoppedError


class StarBonusWatcher:
    def __init__(self,treasury):
        self.treasury=treasury
        self.adb=treasury.adb
        self.vision=treasury.vision
        self.memory=treasury.memory

    def read(self,screen):
        words=self.treasury.words(screen,(330,800,650,95))
        text=' '.join(word for word,x,y in words)
        if 'star bonus' not in text:
            return None
        timer=re.search(r'next\s*in\s*:?\s*(.*)',text)
        if timer:
            seconds=self.treasury.duration(timer.group(1))
            if seconds:
                return dict(wait_seconds=seconds,stars=None,claim=None)
            return None # Broken countdown is not a ready reward.
        progress=re.search(r'\b([0-5])\s*/\s*5\b',text)
        stars=int(progress.group(1)) if progress else None
        if stars is None:
            # At the observed canonical layout the five star interiors occupy
            # these small patches. Only accept all five strongly gold patches.
            gold=[]
            for x in (723,768,813,858,903):
                hsv=cv2.cvtColor(screen[845:866,x-8:x+9],cv2.COLOR_BGR2HSV)
                gold.append((cv2.inRange(hsv,(15,110,130),(40,255,255))>0).mean()>.6)
            stars=sum(gold)
        claim=None
        if stars==5:
            for word,x,y in words:
                if word.strip() in ('collect','claim','thu thap','nhan'):
                    patch=screen[max(0,y-22):min(900,y+22),max(0,x-70):min(1600,x+70)]
                    if (cv2.inRange(cv2.cvtColor(patch,cv2.COLOR_BGR2HSV),(25,90,90),(90,255,255))>0).mean()>.2:
                        claim=(x,y)
        return dict(wait_seconds=0,stars=stars,claim=claim)

    def handle(self,screen,log=None):
        log=log or (lambda message:None)
        settings=self.treasury.manager.settings
        if not settings.get('auto_collect_treasury',False) or not settings.get('treasury_star_bonus_enabled',True):
            return screen
        now=time.time()
        if now<self.memory.get('bonus_read_after',0):
            return screen
        try:
            status=self.read(screen)
            self.memory['bonus_read_after']=now+30
            if status is None:
                return screen
            previous=self.memory.get('bonus_stars')
            if status['wait_seconds']:
                # A newly appearing cooldown after observed progress is evidence
                # that the game completed/auto-awarded that bonus cycle.
                if previous is not None and previous>0 and self.memory.get('bonus_available_at',0)<=now:
                    self.memory['star_reward_pending']=True
                    log('[Star Bonus] Game chuyển sang chu kỳ chờ mới; sẽ kiểm tra Treasury một lần.')
                due=now+status['wait_seconds']+60
                self.memory.update(bonus_available_at=due,bonus_read_after=due,bonus_stars=0,
                                   bonus_full_seen=False,bonus_claim_retry_at=0)
                log(f"[Star Bonus] Còn {status['wait_seconds']//3600}h {(status['wait_seconds']%3600)//60}m; đã lưu giờ kiểm tra.")
                return screen
            self.memory['bonus_stars']=status['stars']
            if status['stars'] is not None and status['stars']!=previous:
                log(f"[Star Bonus] Tiến độ đang hiển thị: {status['stars']}/5 sao.")
            if status['stars']!=5:
                return screen
            fresh=self.adb.screencap()
            verified=self.read(fresh)
            if not verified or verified['stars']!=5 or verified['wait_seconds'] or verified['claim']!=status['claim']:
                return screen
            # The game may auto-award the fifth star. Even without a Collect
            # control, schedule only one Treasury check for this observed cycle.
            if not self.memory.get('bonus_full_seen',False):
                self.memory['star_reward_pending']=True
                self.memory['bonus_full_seen']=True
            point=status['claim']
            if point is None or now<self.memory.get('bonus_claim_retry_at',0):
                return screen
            # Persist before input so a restart cannot repeatedly claim the same
            # uncertain popup. New cycles clear this gate when a cooldown appears.
            self.memory['bonus_claim_retry_at']=now+1800
            self.treasury.save_memory()
            self.adb.tap(*point,jitter=0,delay_after=.6)
            for _ in range(5):
                fresh=self.adb.screencap()
                if self.vision.is_attack_menu_open(fresh)[0]:
                    observed=self.read(fresh)
                    if observed and observed['wait_seconds']:
                        self.memory['bonus_available_at']=time.time()+observed['wait_seconds']+60
                        self.memory['bonus_read_after']=self.memory['bonus_available_at']
                        log('[Star Bonus] Đã thấy chu kỳ chờ mới sau nhận thưởng; chờ kiểm tra tài nguyên Treasury.')
                        return fresh
                words=self.treasury.words(fresh,(350,170,900,530))
                body=' '.join(word for word,x,y in words)
                if re.search(r'\b(gems?|buy|purchase|mua|ngoc)\b',body):
                    return None
                if 'star bonus' in body:
                    okay=next(((x,y) for word,x,y in words if word.strip() in ('okay','ok')),None)
                    if okay:
                        self.adb.tap(*okay,jitter=0,delay_after=.5)
                        return None
                time.sleep(.25)
            return None
        except BotStoppedError:
            raise
        except Exception as exc:
            log(f'[Star Bonus] Chưa đọc rõ ({type(exc).__name__}); bỏ thao tác thưởng lượt này.')
            return screen
        finally:
            self.treasury.save_memory()
