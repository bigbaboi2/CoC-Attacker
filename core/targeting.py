"""Spell targets require observed friendly health bars and avoid active effect zones."""
import math
import cv2
import numpy as np

class SpellTargeter:
    @staticmethod
    def kind(spell):
        return 'rage' if spell in ('no','angry_spell') else ('haste' if spell=='haste_spell' else spell)

    @classmethod
    def effect_group(cls,spell):
        kind=cls.kind(spell)
        return 'speed_support' if kind in ('rage','haste') else kind

    def __init__(self):
        self.max_lead=38
        self.inward_bias=12
        self.previous=[]
        self.previous_at=0
        self.active=[]
        self.trusted=[]

    def friendly_positions(self,screen):
        if screen is None or not isinstance(getattr(screen,'shape',None),tuple):return []
        hsv=cv2.cvtColor(screen,cv2.COLOR_BGR2HSV)
        mask=cv2.inRange(hsv,(30,160,145),(82,255,255)) | cv2.inRange(hsv,(125,180,130),(165,255,255))
        mask[:110]=0;mask[700:]=0;mask[:,:215]=0;mask[:,1410:]=0
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        points=[]
        self.stationary_friendly=[]
        gray=cv2.cvtColor(screen,cv2.COLOR_BGR2GRAY)
        for contour in contours:
            x,y,w,h=cv2.boundingRect(contour)
            if not (7<=w<=85 and 1<=h<=7 and w/max(1,h)>=3):continue
            # Health bars have a dark outline; thin green scenery without it is ignored.
            rim=gray[max(0,y-2):y+h+2,max(0,x-2):x+w+2]
            above=gray[max(0,y-1):y,max(0,x):x+w]
            below=gray[y+h:y+h+1,max(0,x):x+w]
            if rim.size==0 or not above.size or not below.size:continue
            if (above<95).mean()<.55 or (below<95).mean()<.55:continue
            body=hsv[y+7:min(700,y+60),max(215,x-25):min(1410,x+w+25)]
            if body.size==0:continue
            blue=((body[:,:,0]>=88)&(body[:,:,0]<=124)&(body[:,:,1]>100)&(body[:,:,2]>110)).sum()
            if blue<25:continue
            point=(x+10,y+22)
            bar=hsv[y:y+h,x:x+w]
            green_fraction=((bar[:,:,0]>=30)&(bar[:,:,0]<=82)).mean()
            if green_fraction>.65:self.stationary_friendly.append(point)
            if all(math.dist(point,other)>18 for other in points):points.append(point)
        return points

    def target(self,screen,spell,now):
        positions=self.friendly_positions(screen)
        self.active=[zone for zone in self.active if zone['until']>now]
        if not positions:return None
        previous=self.previous;dt=now-self.previous_at
        tracked=[]
        trusted=[]
        for x,y in positions:
            vx=vy=0
            moving=False
            if previous and .15<=dt<=8:
                old=min(previous,key=lambda point:math.dist((x,y),point))
                distance=math.dist((x,y),old)
                if 4<=distance<=50:
                    moving=True
                    vx=(x-old[0])/dt;vy=(y-old[1])/dt
                    length=max(1,math.hypot(vx,vy)/45)
                    vx/=length;vy/=length
            inherited=any(math.dist((x,y),point)<22 and until>now for point,until in self.trusted)
            friend_here=any(math.dist((x,y),point)<3 for point in getattr(self,'stationary_friendly',[]))
            if moving or inherited or friend_here:
                length=max(1,math.hypot(800-x,430-y))
                # Predict a short travel segment while keeping the current
                # army inside the effect radius when the spell lands.
                dx=vx*.55+(800-x)/length*self.inward_bias
                dy=vy*.55+(430-y)/length*self.inward_bias
                scale=max(1,math.hypot(dx,dy)/self.max_lead)
                tracked.append(((x,y),(x+dx/scale,y+dy/scale)))
                trusted.append(((x,y),now+3 if moving else now+1))
        self.previous=positions;self.previous_at=now;self.trusted=trusted
        if not tracked:return None
        kind=self.kind(spell)
        scored=[]
        for current,future in tracked:
            members=[pair for pair in tracked if math.dist(current,pair[0])<=75]
            cx=sum(pair[1][0] for pair in members)/len(members)
            cy=sum(pair[1][1] for pair in members)/len(members)
            if not (220<=cx<=1390 and 120<=cy<=690):continue
            covered_now=sum(math.dist((cx,cy),pair[0])<=70 for pair in members)
            covered_next=sum(math.dist((cx,cy),pair[1])<=70 for pair in members)
            if not covered_now or not covered_next:continue
            # Different effects may benefit the same army. Avoid repeating the
            # same effect on an area that is already covered.
            if any(self.effect_group(zone['spell'])==self.effect_group(spell) and math.dist((cx,cy),zone['point'])<90 for zone in self.active):continue
            scored.append((covered_now+covered_next,-math.dist((cx,cy),(800,430)),(round(cx),round(cy))))
        return max(scored)[2] if scored else None

    def mark(self,point,spell,now):
        duration=20
        self.active.append(dict(point=point,spell=spell,until=now+duration))
