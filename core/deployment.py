"""Find conservative drop points outside the observed red deployment boundary."""
import cv2
import numpy as np

class DeploymentPlanner:
    def points(self, screen, count=16):
        if screen is None or screen.shape[:2] != (900,1600): return []
        hsv=cv2.cvtColor(screen,cv2.COLOR_BGR2HSV)
        mask=cv2.inRange(hsv,(0,120,70),(22,245,225)) | cv2.inRange(hsv,(170,120,70),(179,245,225))
        mask[:115,:]=0;mask[710:,:]=0;mask[:,:210]=0;mask[:,1420:]=0
        mask[:260,1200:]=0;mask[550:,1290:]=0
        lines=cv2.HoughLinesP(mask,1,np.pi/180,threshold=35,minLineLength=90,maxLineGap=15)
        endpoints=[]
        if lines is not None:
            for x1,y1,x2,y2 in lines.reshape(-1,4):
                slope=abs((y2-y1)/float(x2-x1)) if x2!=x1 else 99
                if .45<=slope<=1.05:endpoints.extend([(x1,y1),(x2,y2)])
        if len(endpoints)<6:return []
        hull=cv2.convexHull(np.asarray(endpoints,dtype=np.float32))
        if cv2.contourArea(hull)<60000:return []
        polygon=hull.reshape(-1,2)
        center=polygon.mean(axis=0)
        result=[]
        for index,p1 in enumerate(polygon):
            p2=polygon[(index+1)%len(polygon)]
            dx,dy=p2-p1
            if dx==0 or not .45<=abs(dy/dx)<=1.05:
                continue
            for t in np.linspace(.1,.9,8):
                point=p1+(p2-p1)*t
                direction=point-center;norm=np.linalg.norm(direction)
                if norm==0:continue
                point=point+direction/norm*65
                x,y=map(int,point)
                if not (150<=x<=1440 and 120<=y<=695):continue
                if (x>1290 and y>550) or (x<780 and y>610):continue
                if cv2.pointPolygonTest(hull,(float(x),float(y)),True)>-25:continue
                if all((x-old[0])**2+(y-old[1])**2>35**2 for old in result):result.append((x,y))
        if len(result)<4:return []
        indices=np.linspace(0,len(result)-1,min(max(4,count),len(result)),dtype=int)
        return [result[i] for i in indices]
