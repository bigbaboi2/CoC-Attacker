"""Conservative world-building matching, run once before resource attacks."""
from pathlib import Path
import math
import cv2
import numpy as np


class ResourceTargetDetector:
    RESOURCE_KINDS=('gold_mine','elixir_collector','dark_elixir_drill',
                    'gold_storage','elixir_storage','dark_elixir_storage')

    def __init__(self, asset_dir=None):
        self.asset_dir=Path(asset_dir or Path(__file__).resolve().parents[1]/'assets')
        self._variants={}

    def variants(self,kind):
        if kind in self._variants:
            return self._variants[kind]
        image=cv2.imread(str(self.asset_dir/'ui2'/'icons'/'upgrade_main'/f'{kind}.png'),cv2.IMREAD_UNCHANGED)
        result=[]
        if image is not None and image.shape[2]==4:
            alpha=image[:,:,3]
            nonzero=cv2.findNonZero((alpha>180).astype(np.uint8))
            if nonzero is not None:
                x,y,w,h=cv2.boundingRect(nonzero)
                image=image[y:y+h,x:x+w]
                for scale in (.28,.36,.44,.52,.64,.78):
                    resized=cv2.resize(image,None,fx=scale*.75,fy=scale*.75)
                    if min(resized.shape[:2])<15:
                        continue
                    mask=(resized[:,:,3]>180).astype(np.uint8)*255
                    if np.count_nonzero(mask)<100:
                        continue
                    result.append((cv2.cvtColor(resized[:,:,:3],cv2.COLOR_BGR2GRAY),mask,resized[:,:,:3]))
        for path in (self.asset_dir.parent/'local_assets'/'resource_world').glob(kind+'*_observed.png'):
            observed=cv2.imread(str(path))
            if observed is None:
                continue
            for scale in (.8,.9,1.,1.1,1.2):
                color=cv2.resize(observed,None,fx=scale*.75,fy=scale*.75)
                h,w=color.shape[:2]
                mask=np.zeros((h,w),dtype=np.uint8)
                cv2.ellipse(mask,(w//2,h//2),(round(w*.44),round(h*.44)),0,0,360,255,-1)
                result.append((cv2.cvtColor(color,cv2.COLOR_BGR2GRAY),mask,color))
        self._variants[kind]=result
        return result

    def detect(self, screen, kinds=None, threshold=.78):
        if screen is None or screen.shape[:2]!=(900,1600):
            return []
        x0,y0,x1,y1=215,110,1400,695
        field=cv2.resize(screen[y0:y1,x0:x1],None,fx=.75,fy=.75)
        gray=cv2.cvtColor(field,cv2.COLOR_BGR2GRAY)
        found=[]
        for kind in (kinds or self.RESOURCE_KINDS):
            for needle,mask,color in self.variants(kind):
                h,w=needle.shape
                scores=cv2.matchTemplate(gray,needle,cv2.TM_CCOEFF_NORMED,mask=mask)
                scores=np.nan_to_num(scores,nan=-1,posinf=-1,neginf=-1)
                for _ in range(12):
                    _,score,_,location=cv2.minMaxLoc(scores)
                    if score<threshold:
                        break
                    x,y=location
                    patch=field[y:y+h,x:x+w]
                    color_error=np.abs(patch.astype(np.float32)-color.astype(np.float32))[mask>0].mean()
                    px,py=round(x0+(x+w/2)/.75),round(y0+(y+h/2)/.75)
                    if color_error<70 and not (px>1280 and py>545):
                        found.append(dict(kind=kind,x=px,y=py,score=float(score),
                                          width=round(w/.75),height=round(h/.75)))
                    scores[max(0,y-h//2):y+h//2+1,max(0,x-w//2):x+w//2+1]=-1
        result=[]
        for item in sorted(found,key=lambda item:item['score'],reverse=True):
            if all(math.dist((item['x'],item['y']),(old['x'],old['y']))>
                   max(22,min(item['width'],old['width'])*.6) for old in result):
                result.append(item)
        return result

    @staticmethod
    def deployment_targets(buildings, legal_points, budget):
        if not legal_points or budget<=0:
            return []
        selected=[]
        for item in buildings:
            nearest=min(legal_points,key=lambda point:math.dist(point,(item['x'],item['y'])))
            selected.append(dict(building=item,point=nearest,
                                 distance=math.dist(nearest,(item['x'],item['y']))))
        # External collectors first, then reachable storages. Never invent an
        # interior tap; pathing after release remains controlled by the game.
        selected.sort(key=lambda item:(item['building']['kind'].endswith('storage'),item['distance'],-item['building']['score']))
        return selected[:budget]
