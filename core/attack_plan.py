"""Geometry-only strategy selection; all output points come from the planner."""
import math


def strategy_points(points, strategy='perimeter', rotation=0):
    unique = list(dict.fromkeys(points))
    if not unique:
        return []
    cx = sum(p[0] for p in unique)/len(unique)
    cy = sum(p[1] for p in unique)/len(unique)
    around = sorted(unique,key=lambda p:math.atan2(p[1]-cy,p[0]-cx))
    if strategy == 'four_corners':
        chosen = []
        for dx,dy in ((0,-1),(1,0),(0,1),(-1,0)):
            candidates = [p for p in unique if p not in chosen]
            if not candidates:
                break
            chosen.append(max(candidates,key=lambda p:
                              ((p[0]-cx)*dx+(p[1]-cy)*dy)/max(1,math.hypot(p[0]-cx,p[1]-cy))))
        return chosen
    if strategy == 'smart_wave':
        size = max(2,len(around)//4)
        start = ((rotation%4)*len(around))//4
        return (around+around)[start:start+min(size,len(around))]
    return around
