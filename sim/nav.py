"""Coarse ground navigation for built-in bots: 4 m walkability grid + Dijkstra flow fields.

Fields are built toward a circle (the next zone) and cached per match, so all built-in bots share one
field per zone phase. Roofs are not part of the graph: bots jet over what the field cannot route around.
"""
from __future__ import annotations

import heapq
import math

import numpy as np

from world import EMPTY_T, RES

CELL = 4.0


class NavGrid:
    def __init__(self, world, radius=2.5):
        self.W = world
        k = int(CELL / RES)  # raster cells per nav cell
        H, Wd = world.H, world.W
        # ground height per raster cell
        ys = world.gy0 + (np.arange(H) + 0.5) * RES
        xs = world.gx0 + (np.arange(Wd) + 0.5) * RES
        gx, gy = np.meshgrid(xs, ys)
        g = world.ground(gx.ravel(), gy.ravel()).reshape(H, Wd)
        lo = (g + 1.2) * 100
        hi = (g + 9.0) * 100
        obst = (world.T0 > lo) & (world.B0 < hi)
        if world.two:
            obst |= (world.T1 > lo) & (world.B1 < hi)
        # dilate by the mech radius (square-ish disc via repeated shifts)
        n = int(math.ceil(radius / RES))
        dil = obst.copy()
        for dj in range(-n, n + 1):
            for di in range(-n, n + 1):
                if di * di + dj * dj <= n * n:
                    dil |= np.roll(np.roll(obst, dj, 0), di, 1)
        self.nh, self.nw = H // k, Wd // k
        # nav cell is free if its centre raster cell is free
        cj = (np.arange(self.nh) * k + k // 2)
        ci = (np.arange(self.nw) * k + k // 2)
        self.free = ~dil[np.ix_(cj, ci)]
        # steep terrain between neighbours handled by edge cost
        self.gz = g[np.ix_(cj, ci)]
        self.x0 = world.gx0
        self.y0 = world.gy0
        self.free_l = self.free.tolist()
        self.gz_l = self.gz.tolist()

    def cell_of(self, x, y):
        i = int((x - self.x0) / CELL)
        j = int((y - self.y0) / CELL)
        return min(max(j, 0), self.nh - 1), min(max(i, 0), self.nw - 1)

    def centre(self, j, i):
        return self.x0 + (i + 0.5) * CELL, self.y0 + (j + 0.5) * CELL

    def field_to_circle(self, cx, cy, r):
        """Dijkstra distance (m) from every free cell to the circle (cells inside r*0.8 are sources)."""
        nh, nw = self.nh, self.nw
        INF = float("inf")
        dist = [[INF] * nw for _ in range(nh)]
        free = self.free_l
        gz = self.gz_l
        pq = []
        rr = max(r * 0.8, CELL)
        j0, i0 = self.cell_of(cx - rr, cy - rr)
        j1, i1 = self.cell_of(cx + rr, cy + rr)
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                x, y = self.centre(j, i)
                if free[j][i] and (x - cx) ** 2 + (y - cy) ** 2 <= rr * rr:
                    dist[j][i] = 0.0
                    pq.append((0.0, j, i))
        if not pq:
            j, i = self.cell_of(cx, cy)
            dist[j][i] = 0.0
            pq.append((0.0, j, i))
        heapq.heapify(pq)
        nb = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0), (-1, -1, 1.4142), (-1, 1, 1.4142), (1, -1, 1.4142), (1, 1, 1.4142)]
        maxslope = math.tan(math.radians(50))
        while pq:
            d, j, i = heapq.heappop(pq)
            if d > dist[j][i]:
                continue
            gzi = gz[j][i]
            for dj, di, c in nb:
                nj, ni = j + dj, i + di
                if nj < 0 or ni < 0 or nj >= nh or ni >= nw or not free[nj][ni]:
                    continue
                if dj and di and not (free[j][ni] and free[nj][i]):
                    continue
                step = c * CELL
                if abs(gz[nj][ni] - gzi) / step > maxslope:
                    continue
                nd = d + step
                if nd < dist[nj][ni]:
                    dist[nj][ni] = nd
                    heapq.heappush(pq, (nd, nj, ni))
        return dist

    def next_waypoint(self, dist, x, y, look=3):
        """follow the field downhill `look` cells; returns (wx, wy) or None if (x,y) is not on the field"""
        j, i = self.cell_of(x, y)
        if dist[j][i] == float("inf"):
            # find the best free neighbour within 2 cells (we may stand in a dilated cell next to a wall)
            best = None
            for dj in range(-2, 3):
                for di in range(-2, 3):
                    nj, ni = j + dj, i + di
                    if 0 <= nj < self.nh and 0 <= ni < self.nw and dist[nj][ni] < float("inf"):
                        v = dist[nj][ni] + CELL * math.hypot(dj, di)
                        if best is None or v < best[0]:
                            best = (v, nj, ni)
            if best is None:
                return None
            _, j, i = best
            return self.centre(j, i)
        for _ in range(look):
            bd, bj, bi = dist[j][i], j, i
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    nj, ni = j + dj, i + di
                    if 0 <= nj < self.nh and 0 <= ni < self.nw and dist[nj][ni] < bd:
                        bd, bj, bi = dist[nj][ni], nj, ni
            if (bj, bi) == (j, i):
                break
            j, i = bj, bi
        return self.centre(j, i)
