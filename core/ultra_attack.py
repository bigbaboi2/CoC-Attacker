"""Build fast card blocks without OCR or screenshots between deployment taps."""
import json
import math
from pathlib import Path


class UltraAttackPlan:
    FIXED_POINTS=((240,380),(800,130),(1380,430),(1050,595),(500,595))

    @classmethod
    def scanned_points(cls,candidates,prefer_fixed=False):
        candidates=list(dict.fromkeys(candidates))
        if not prefer_fixed or not candidates:
            return candidates
        # Fixed directions may prioritize an observed legal point, never supply
        # a blind world coordinate that lies inside the current base boundary.
        selected=[]
        for x,y in cls.FIXED_POINTS:
            point=min(candidates,key=lambda p:(p[0]-x)**2+(p[1]-y)**2)
            if point not in selected:
                selected.append(point)
        return selected if len(selected)>=4 else candidates

    @staticmethod
    def memory_path(settings):
        path=getattr(settings,'filepath',None)
        return Path(path).resolve().with_name('attack_deck_state.json') if isinstance(path,(str,Path)) else None

    @staticmethod
    def valid(cards):
        if not isinstance(cards,dict) or not cards.get('troops'):
            return False
        for kind in ('troops','siege','heroes','spells'):
            group=cards.get(kind)
            if not isinstance(group,list) or len(group)>20:
                return False
            for card in group:
                if not isinstance(card,dict) or not isinstance(card.get('name'),str):
                    return False
                if type(card.get('x')) is not int or not 60<=card['x']<=1540 or card.get('y')!=810:
                    return False
                if kind in ('troops','spells') and (type(card.get('count')) is not int or not 1<=card['count']<=300):
                    return False
        return True

    @classmethod
    def remember(cls,settings,cards,*,serial=None):
        path=cls.memory_path(settings)
        if path is None or not cls.valid(cards):
            return
        from core.stats_store import StatsStore
        try:
            StatsStore._atomic_write(path,json.dumps(dict(device_serial=serial or settings.get('device_serial'),cards=cards),ensure_ascii=False,default=StatsStore.json_default).encode('utf-8'))
        except (OSError, TypeError, ValueError):
            pass

    @classmethod
    def remembered_cards(cls,settings,serial):
        path=cls.memory_path(settings)
        if path:
            try:
                saved=json.loads(path.read_text(encoding='utf-8'))
                if saved.get('device_serial')==serial and cls.valid(saved.get('cards')):
                    return saved['cards']
            except (OSError,ValueError,TypeError,AttributeError):
                pass
        return None

    @classmethod
    def cards(cls,settings):
        recent=cls.remembered_cards(settings,settings.get('device_serial'))
        if recent:
            return recent, 'đội hình đã nhớ'
        # Explicit preset for the user's observed nine-card army. Counts are
        # tap budgets, not a claim that the current screen was recognized.
        def card(name,x,count=1):return dict(name=name,x=x,y=810,count=count)
        main_taps=max(1,min(300,int(settings.get('ultra_main_taps',17))))
        support_taps=max(1,min(300,int(settings.get('ultra_support_taps',3))))
        return dict(troops=[card('dragon',168,main_taps),card('wizard',303,support_taps)],
                    siege=[card('sky_wagon',439)],
                    heroes=[card(name,x) for name,x in (('king',574),('queen',710),('warden',845),('luke',981))],
                    spells=[card('no',1116,5),card('haste_spell',1252,1)]),'mẫu cố định 9 ô: rồng/wizard/xe/4 tướng/2 phép'

    @staticmethod
    def diverse_points(points,rotation=0):
        around=sorted(dict.fromkeys(points),key=lambda p:math.atan2(p[1]-430,p[0]-800))
        if not around:return []
        chosen=[around.pop(rotation%len(around))]
        def angle(point):return math.atan2(point[1]-430,point[0]-800)
        def separation(a,b):return abs((angle(a)-angle(b)+math.pi)%(2*math.pi)-math.pi)
        while around:
            point=max(around,key=lambda p:min(separation(p,q) for q in chosen))
            chosen.append(point);around.remove(point)
        return chosen

    @staticmethod
    def spell_points(points,count,settings,offset=0):
        inward=max(.05,min(.5,int(settings.get('ultra_spell_inward_pct',20))/100))
        lead_cap=max(15,min(60,int(settings.get('ultra_spell_max_lead_px',40))))
        result=[]
        for x,y in (points[(offset+i)%len(points)] for i in range(count)):
            distance=max(1,math.hypot(800-x,430-y))
            lead=min(distance*inward,lead_cap)
            result.append((round(x+(800-x)/distance*lead),round(y+(430-y)/distance*lead)))
        return result

    @classmethod
    def blocks(cls,cards,points,settings):
        blocks=[]
        spells=[dict(card) for card in cards['spells'] if card['name'] in ('no','angry_spell','haste_spell')]
        zones=[]
        def deploy(card,count,targets,kind):
            if count:
                blocks.append(dict(card=card,kind=kind,points=[targets[i%len(targets)] for i in range(count)]))
        def support(entries,limit=1):
            from core.targeting import SpellTargeter
            sent=0
            for card in spells:
                if card['count']<=0 or sent>=limit:continue
                for target in cls.spell_points(entries,len(entries),settings):
                    group=SpellTargeter.effect_group(card['name'])
                    if any(group==old_group and math.dist(target,old)<90 for old_group,old in zones):continue
                    cast=dict(card);cast['count']=1
                    blocks.append(dict(card=cast,kind='spells',points=[target],planned=True))
                    zones.append((group,target));card['count']-=1;sent+=1
                    break
        main=cards['troops'][0]
        opening=min(main['count'],4,len(points))
        deploy(main,opening,points,'troops')
        # Plan support during scouting, then cast as soon as the opening troops
        # are released; no health-bar/OCR wait in this initial timeline.
        support(points[:opening],limit=2)
        use_siege = settings.get('use_siege', True)
        siege_mode = settings.get('siege_deploy_mode', 'always')
        for card in cards['siege']:
            if not use_siege:
                continue
            if siege_mode == 'require_cc' and not card.get('has_cc', True):
                continue
            deploy(card,1,points,'siege')
        for i,card in enumerate(cards['heroes']):
            if settings.get('use_'+card['name'],True):
                deploy(card,1,[points[i%len(points)]],'heroes')
        cursor=opening
        for card in cards['troops']:
            remaining=card['count']-opening if card is main else card['count']
            while remaining:
                count=min(4,remaining)
                targets=points[cursor%len(points):]+points[:cursor%len(points)]
                deploy(card,count,targets,'troops')
                support(targets[:count])
                cursor+=count;remaining-=count
        return blocks

    @classmethod
    def sequence(cls,cards,points,settings):
        blocks=cls.blocks(cards,points,settings)
        commands=[point for block in blocks for point in [(block['card']['x'],block['card']['y'])]+block['points']]
        skills=[(card['x'],card['y']) for card in cards['heroes']
                if settings.get('use_'+card['name'],True) and settings.get('trigger_hero_abilities',True)]
        return commands,skills
