"""Static world for the simulator: terrain heightfield, rasterised prisms, trees.

Prisms are rasterised onto a RES (0.5 m) grid as up to K=2 solid z-intervals per cell
(int16 centimetres). Trees go into vision-only grids (trunks: solid interval, crowns:
foliage interval used for the canopy transmittance rule). All hot queries are vectorised
with numpy. The raster is cached under sim/.cache keyed by the world.json hash.
"""
from __future__ import annotations

import hashlib
import json
import math
import os

import numpy as np

from paths import find_arena  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RES = 0.5
EMPTY_B = 32767
EMPTY_T = -32768


def _pip_mask(px, py, ring):
    """even-odd point in polygon for arrays px,py against flat ring [x0,y0,...]"""
    xs = np.asarray(ring[0::2], dtype=np.float64)
    ys = np.asarray(ring[1::2], dtype=np.float64)
    inside = np.zeros(px.shape, dtype=bool)
    n = len(xs)
    for a in range(n):
        b = (a + 1) % n
        xa, ya, xb, yb = xs[a], ys[a], xs[b], ys[b]
        if ya == yb:
            continue
        cond = (ya > py) != (yb > py)
        xint = xa + (py - ya) * (xb - xa) / (yb - ya)
        inside ^= cond & (px < xint)
    return inside


class World:
    def __init__(self, arena_dir: str | None = None):
        self.arena_dir = find_arena(arena_dir)
        wpath = os.path.join(self.arena_dir, "v2", "world.json")
        raw = open(wpath, "rb").read()
        self.w = json.loads(raw)
        b = self.w["bounds"]
        self.minx, self.maxx, self.miny, self.maxy = b["minX"], b["maxX"], b["minY"], b["maxY"]
        t = self.w["terrain"]
        self.tx0, self.ty0, self.tstep, self.tnx, self.tny = t["minX"], t["minY"], t["step"], t["nx"], t["ny"]
        ter = np.fromfile(os.path.join(self.arena_dir, "v2", "terrain.i16"), dtype="<i2")
        self.terrain = (ter.reshape(self.tny, self.tnx).astype(np.float32) * float(t.get("scale", 0.01)))
        # solids grid
        self.gx0, self.gy0 = self.tx0, self.ty0
        self.W = int(round((self.tnx - 1) * self.tstep / RES)) + 2
        self.H = int(round((self.tny - 1) * self.tstep / RES)) + 2
        self.prisms = self.w["prisms"]
        rules = self.w.get("rules", {})
        self.canopy_density = float(rules.get("canopyDensity", 0.35))
        self.canopy_block_t = float(rules.get("canopyBlockT", 0.25))
        key = hashlib.sha1(raw + b"v3").hexdigest()[:16]
        cdir = os.path.join(HERE, ".cache")
        cpath = os.path.join(cdir, f"world_{key}.npz")
        if os.path.exists(cpath):
            z = np.load(cpath)
            self.B, self.T, self.TB, self.TT, self.CB, self.CT, self.PID = (z[k] for k in ("B", "T", "TB", "TT", "CB", "CT", "PID"))
        else:
            self._rasterise()
            os.makedirs(cdir, exist_ok=True)
            tmp = cpath + f".{os.getpid()}.tmp.npz"
            np.savez_compressed(tmp, B=self.B, T=self.T, TB=self.TB, TT=self.TT, CB=self.CB, CT=self.CT, PID=self.PID)
            os.replace(tmp, cpath)
        self.TOPMAX = self.T.max(axis=0)
        self.has_solid = self.TOPMAX > EMPTY_T
        self.two = bool((self.T[1] > EMPTY_T).any())
        self.T0, self.B0, self.T1, self.B1 = self.T[0], self.B[0], self.T[1], self.B[1]
        self._ter = self.terrain.tolist()
        # 2 m tiles flagged when any solid lies within 8 m (fast path for disc queries with r <= 7)
        th, tw = (self.H + 3) // 4, (self.W + 3) // 4
        pad = np.zeros((th * 4, tw * 4), dtype=bool)
        pad[: self.H, : self.W] = self.has_solid
        tiles = pad.reshape(th, 4, tw, 4).any(axis=(1, 3))
        dil = tiles.copy()
        for dj in range(-4, 5):
            for di in range(-4, 5):
                dil |= np.roll(np.roll(tiles, dj, 0), di, 1)
        self._tile = dil.tolist()
        self._tw, self._th = tw, th
        # building roofs (for crate/loot placement): prism index list with area
        self._roof_cells = None
        self._open_cells = None

    # ------------------------------------------------------------------ rasterisation
    def _rasterise(self):
        W, H = self.W, self.H
        B = np.full((2, H, W), EMPTY_B, dtype=np.int16)
        T = np.full((2, H, W), EMPTY_T, dtype=np.int16)
        PID = np.full((H, W), -1, dtype=np.int16)  # topmost prism id (for roof lookups)
        merges = 0
        for pi, p in enumerate(self.prisms):
            ring = p["ring"]
            xs, ys = ring[0::2], ring[1::2]
            i0 = max(0, int((min(xs) - self.gx0) / RES) - 1)
            i1 = min(W - 1, int((max(xs) - self.gx0) / RES) + 1)
            j0 = max(0, int((min(ys) - self.gy0) / RES) - 1)
            j1 = min(H - 1, int((max(ys) - self.gy0) / RES) + 1)
            ii, jj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1))
            px = self.gx0 + (ii + 0.5) * RES
            py = self.gy0 + (jj + 0.5) * RES
            m = _pip_mask(px, py, ring)
            for hole in p.get("holes") or []:
                if len(hole) >= 6:
                    m &= ~_pip_mask(px, py, hole)
            if not m.any():
                continue
            jj, ii = jj[m], ii[m]
            bc = int(round(p["base"] * 100))
            tc = int(round(p["top"] * 100))
            # slot 0 empty?
            e0 = T[0, jj, ii] == EMPTY_T
            B[0, jj[e0], ii[e0]] = bc
            T[0, jj[e0], ii[e0]] = tc
            r = ~e0
            jr, ir = jj[r], ii[r]
            # overlapping/adjacent with slot0 -> merge
            ov0 = (B[0, jr, ir] <= tc + 5) & (T[0, jr, ir] >= bc - 5)
            B[0, jr[ov0], ir[ov0]] = np.minimum(B[0, jr[ov0], ir[ov0]], bc)
            T[0, jr[ov0], ir[ov0]] = np.maximum(T[0, jr[ov0], ir[ov0]], tc)
            jr, ir = jr[~ov0], ir[~ov0]
            e1 = T[1, jr, ir] == EMPTY_T
            B[1, jr[e1], ir[e1]] = bc
            T[1, jr[e1], ir[e1]] = tc
            jr, ir = jr[~e1], ir[~e1]
            if len(jr):
                merges += len(jr)
                B[1, jr, ir] = np.minimum(B[1, jr, ir], bc)
                T[1, jr, ir] = np.maximum(T[1, jr, ir], tc)
            # topmost prism id
            cur = PID[jj, ii]
            better = np.ones(len(jj), dtype=bool)
            has = cur >= 0
            if has.any():
                tops = np.array([self.prisms[c]["top"] if c >= 0 else -1e9 for c in cur])
                better = tops < p["top"]
            PID[jj[better], ii[better]] = pi
        self.B, self.T, self.PID = B, T, PID
        # trees
        tr = self.w["trees"]
        F = tr["fields"]
        d = np.asarray(tr["data"], dtype=np.float64).reshape(-1, len(F))
        col = {f: d[:, k] for k, f in enumerate(F)}
        TB = np.full((H, W), EMPTY_B, dtype=np.int16)
        TT = np.full((H, W), EMPTY_T, dtype=np.int16)
        CB = np.full((H, W), EMPTY_B, dtype=np.int16)
        CT = np.full((H, W), EMPTY_T, dtype=np.int16)
        for k in range(len(d)):
            x, y, z, h = col["x"][k], col["y"][k], col["z"][k], col["height"][k]
            for (cx, cy, r, zb, zt, GB, GT) in (
                (x, y, max(col["trunkR"][k], 0.25), z, z + h, TB, TT),
                (col["crownX"][k], col["crownY"][k], col["crownR"][k], z + col["crownBase"][k], z + h, CB, CT),
            ):
                i0 = max(0, int((cx - r - self.gx0) / RES))
                i1 = min(W - 1, int((cx + r - self.gx0) / RES) + 1)
                j0 = max(0, int((cy - r - self.gy0) / RES))
                j1 = min(H - 1, int((cy + r - self.gy0) / RES) + 1)
                ii, jj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1))
                px = self.gx0 + (ii + 0.5) * RES
                py = self.gy0 + (jj + 0.5) * RES
                m = (px - cx) ** 2 + (py - cy) ** 2 <= r * r
                if not m.any():
                    # tiny trunks: at least the centre cell
                    m = (px - cx) ** 2 + (py - cy) ** 2 <= (r + RES) ** 2
                jj, ii = jj[m], ii[m]
                GB[jj, ii] = np.minimum(GB[jj, ii], int(zb * 100))
                GT[jj, ii] = np.maximum(GT[jj, ii], int(zt * 100))
        self.TB, self.TT, self.CB, self.CT = TB, TT, CB, CT

    # ------------------------------------------------------------------ point queries
    def ground(self, x, y):
        """terrain height, bilinear; scalars or arrays"""
        if isinstance(x, (float, int)) and isinstance(y, (float, int)):
            fx = (x - self.tx0) / self.tstep
            fy = (y - self.ty0) / self.tstep
            if fx < 0:
                fx = 0.0
            elif fx > self.tnx - 1.000001:
                fx = self.tnx - 1.000001
            if fy < 0:
                fy = 0.0
            elif fy > self.tny - 1.000001:
                fy = self.tny - 1.000001
            i = int(fx)
            j = int(fy)
            ax = fx - i
            ay = fy - j
            r0 = self._ter[j]
            r1 = self._ter[j + 1]
            return (r0[i] * (1 - ax) + r0[i + 1] * ax) * (1 - ay) + (r1[i] * (1 - ax) + r1[i + 1] * ax) * ay
        fx = (np.asarray(x, dtype=np.float64) - self.tx0) / self.tstep
        fy = (np.asarray(y, dtype=np.float64) - self.ty0) / self.tstep
        fx = np.clip(fx, 0, self.tnx - 1.000001)
        fy = np.clip(fy, 0, self.tny - 1.000001)
        i = fx.astype(np.int64)
        j = fy.astype(np.int64)
        ax = fx - i
        ay = fy - j
        T = self.terrain
        h = (T[j, i] * (1 - ax) * (1 - ay) + T[j, i + 1] * ax * (1 - ay) + T[j + 1, i] * (1 - ax) * ay + T[j + 1, i + 1] * ax * ay)
        return h if h.ndim else float(h)

    def near_solid(self, x, y, r):
        """False when no solid can be within r (<= 7 m) of (x, y): fast path"""
        if r > 7.0:
            return True
        ti = int((x - self.gx0) / (RES * 4))
        tj = int((y - self.gy0) / (RES * 4))
        if ti < 0 or tj < 0 or ti >= self._tw or tj >= self._th:
            return True
        return self._tile[tj][ti]

    def cell(self, x, y):
        i = int((x - self.gx0) / RES)
        j = int((y - self.gy0) / RES)
        return min(max(j, 0), self.H - 1), min(max(i, 0), self.W - 1)

    def cells(self, x, y):
        i = ((np.asarray(x) - self.gx0) / RES).astype(np.int64)
        j = ((np.asarray(y) - self.gy0) / RES).astype(np.int64)
        np.clip(i, 0, self.W - 1, out=i)
        np.clip(j, 0, self.H - 1, out=j)
        return j, i

    def support(self, x, y, reach_z):
        """highest surface under (x,y) whose top is <= reach_z (terrain or prism top)."""
        g = self.ground(float(x), float(y))
        j, i = self.cell(x, y)
        rc = reach_z * 100.0 + 0.5
        best = g
        t = self.T0.item(j, i)
        if t != EMPTY_T and t <= rc and t / 100.0 > best:
            best = t / 100.0
        if self.two:
            t = self.T1.item(j, i)
            if t != EMPTY_T and t <= rc and t / 100.0 > best:
                best = t / 100.0
        return best

    def surface_top(self, x, y):
        """the highest solid top at (x,y) (roof or ground)"""
        j, i = self.cell(x, y)
        t = int(self.TOPMAX[j, i])
        g = self.ground(x, y)
        return max(g, t / 100.0) if t != EMPTY_T else g

    def inside_solid(self, x, y, z):
        j, i = self.cell(x, y)
        zc = z * 100
        for k in (0, 1):
            if self.B[k, j, i] <= zc <= self.T[k, j, i]:
                return True
        return False

    # disc offsets cache
    _disc_cache: dict = {}

    def disc_offsets(self, r):
        key = int(round(r * 20))
        d = self._disc_cache.get(key)
        if d is None:
            n = int(math.ceil(r / RES)) + 1
            oi, oj = np.meshgrid(np.arange(-n, n + 1), np.arange(-n, n + 1))
            m = (oi * RES) ** 2 + (oj * RES) ** 2 <= (r - 0.01) ** 2
            d = (oj[m].astype(np.int64), oi[m].astype(np.int64), oi[m] * RES, oj[m] * RES)
            self._disc_cache[key] = d
        return d

    def disc_cells(self, x, y, r):
        """cells whose centre is within r of (x, y) (exact position, not the centre cell)"""
        oj, oi, ox, oy = self.disc_offsets(r + RES)
        ci = int((x - self.gx0) / RES)
        cj = int((y - self.gy0) / RES)
        ex = ox + ((ci + 0.5) * RES + self.gx0 - x)
        ey = oy + ((cj + 0.5) * RES + self.gy0 - y)
        k = ex * ex + ey * ey <= r * r
        jj = np.clip(cj + oj[k], 0, self.H - 1)
        ii = np.clip(ci + oi[k], 0, self.W - 1)
        return jj, ii, ex[k], ey[k]

    _mask_cache: dict = {}

    def _disc_slice(self, x, y, r):
        """(slice_j, slice_i, mask, n, ci, cj) or None near the map edge"""
        key = int(round(r * 20))
        mk = self._mask_cache.get(key)
        if mk is None:
            n = int(math.ceil(r / RES)) + 1
            oi, oj = np.meshgrid(np.arange(-n, n + 1), np.arange(-n, n + 1))
            mask = (oi * RES) ** 2 + (oj * RES) ** 2 <= (r - 0.01) ** 2
            mk = (n, mask, oi * RES, oj * RES)
            self._mask_cache[key] = mk
        n = mk[0]
        ci = int((x - self.gx0) / RES)
        cj = int((y - self.gy0) / RES)
        if ci - n < 0 or cj - n < 0 or ci + n >= self.W or cj + n >= self.H:
            return None
        return slice(cj - n, cj + n + 1), slice(ci - n, ci + n + 1), mk, ci, cj

    def disc_block(self, x, y, r, feet, step, height):
        """True if a solid in the disc blocks a body standing at `feet` (allowing steps <= step).
        Returns (blocked, normal_xy or None)."""
        if not self.near_solid(x, y, r):
            return False, None
        lo = (feet + step) * 100.0
        hi = (feet + height) * 100.0
        sl = self._disc_slice(x, y, r)
        if sl is not None:
            sj, si, (n, mask, ox, oy), ci, cj = sl
            ex = ox + ((ci + 0.5) * RES + self.gx0 - x)
            ey = oy + ((cj + 0.5) * RES + self.gy0 - y)
            mask = ex * ex + ey * ey <= r * r
            m = (self.T0[sj, si] > lo) & (self.B0[sj, si] < hi)
            if self.two:
                m |= (self.T1[sj, si] > lo) & (self.B1[sj, si] < hi)
            m &= mask
            if not m.any():
                return False, None
            fx = (x - self.gx0) / RES - ci - 0.5
            fy = (y - self.gy0) / RES - cj - 0.5
            cx = ox[m].mean() / RES - fx
            cy = oy[m].mean() / RES - fy
            nn = math.hypot(cx, cy)
            if nn < 1e-6:
                return True, None
            return True, (-cx / nn, -cy / nn)
        jj, ii, ox, oy = self.disc_cells(x, y, r)
        T0 = self.T[0, jj, ii]
        B0 = self.B[0, jj, ii]
        T1 = self.T[1, jj, ii]
        B1 = self.B[1, jj, ii]
        m = ((T0 > lo) & (B0 < hi)) | ((T1 > lo) & (B1 < hi))
        if not m.any():
            return False, None
        # normal: away from the blocked cells' centroid
        cx = (ii[m] - (x - self.gx0) / RES + 0.5).mean()
        cy = (jj[m] - (y - self.gy0) / RES + 0.5).mean()
        n = math.hypot(cx, cy)
        if n < 1e-6:
            return True, None
        return True, (-cx / n, -cy / n)

    def disc_ceiling(self, x, y, r, z_lo, z_hi):
        """lowest solid base within (z_lo, z_hi] over the disc, or None"""
        if not self.near_solid(x, y, r):
            return None
        jj, ii, _, _ = self.disc_cells(x, y, r)
        lo, hi = z_lo * 100.0, z_hi * 100.0
        best = None
        for k in (0, 1):
            Bk = self.B[k, jj, ii]
            m = (Bk > lo) & (Bk <= hi)
            if m.any():
                v = Bk[m].min() / 100.0
                best = v if best is None else min(best, v)
        return best

    def disc_landing(self, x, y, r, z_new, z_old):
        """highest solid top in the disc with z_new <= top <= z_old (for landing on an edge)"""
        if not self.near_solid(x, y, r):
            return None, None
        jj, ii, ox, oy = self.disc_cells(x, y, r)
        lo, hi = z_new * 100.0 - 0.5, z_old * 100.0 + 0.5
        best = None
        bxy = None
        for k in (0, 1):
            Tk = self.T[k, jj, ii]
            m = (Tk >= lo) & (Tk <= hi)
            if m.any():
                v = Tk[m].max() / 100.0
                if best is None or v > best:
                    best = v
                    sel = m & (Tk == Tk[m].max())
                    bxy = (float(ox[sel].mean()), float(oy[sel].mean()))
        return best, bxy

    # ------------------------------------------------------------------ rays
    def _samples(self, P0, P1, step):
        P0 = np.asarray(P0, dtype=np.float64).reshape(-1, 3)
        P1 = np.asarray(P1, dtype=np.float64).reshape(-1, 3)
        D = P1 - P0
        L = np.sqrt((D * D).sum(1))
        n = np.maximum(1, np.ceil(L / step).astype(np.int64))
        idx = np.repeat(np.arange(len(P0)), n)
        starts = np.cumsum(n) - n
        k = np.arange(n.sum()) - np.repeat(starts, n)
        # sample at the end of each sub-step: t in (0,1]
        t = (k + 1) / np.repeat(n, n)
        S = P0[idx] + D[idx] * t[:, None]
        return S, idx, t, n, L

    def _solid_at(self, S):
        j, i = self.cells(S[:, 0], S[:, 1])
        zc = S[:, 2] * 100.0
        hit = ((self.B[0, j, i] <= zc) & (zc <= self.T[0, j, i])) | ((self.B[1, j, i] <= zc) & (zc <= self.T[1, j, i]))
        hit |= S[:, 2] < self.ground(S[:, 0], S[:, 1])
        out = (S[:, 0] < self.minx) | (S[:, 0] > self.maxx) | (S[:, 1] < self.miny) | (S[:, 1] > self.maxy)
        return hit | out, j, i, zc

    def los(self, P0, P1, vision=True, step=0.75):
        """bool array: clear line of sight between point pairs (vision applies trees)."""
        P0 = np.asarray(P0, dtype=np.float64).reshape(-1, 3)
        if len(P0) == 0:
            return np.zeros(0, dtype=bool)
        S, idx, t, n, L = self._samples(P0, P1, step)
        # ignore the very last sample (target point itself may be inside geometry, e.g. a roof crate)
        last = np.cumsum(n) - 1
        hit, j, i, zc = self._solid_at(S)
        hit[last] = False
        blocked = np.zeros(len(P0), dtype=bool)
        if vision:
            hit |= (self.TB[j, i] <= zc) & (zc <= self.TT[j, i])
            hit[last] = False
            fol = (self.CB[j, i] <= zc) & (zc <= self.CT[j, i])
            flen = np.bincount(idx, weights=fol.astype(np.float64), minlength=len(P0)) * (L / n)
            blocked |= np.exp(-self.canopy_density * flen) < self.canopy_block_t
        blocked |= np.bincount(idx, weights=hit.astype(np.float64), minlength=len(P0)) > 0
        return ~blocked

    def first_hit(self, P0, P1, step=0.4):
        """for each segment: fraction t in [0,1] of the first solid sample, or 2.0 if clear."""
        P0 = np.asarray(P0, dtype=np.float64).reshape(-1, 3)
        if len(P0) == 0:
            return np.zeros(0)
        S, idx, t, n, L = self._samples(P0, P1, step)
        hit, _, _, _ = self._solid_at(S)
        res = np.full(len(P0), 2.0)
        if hit.any():
            hi = idx[hit]
            ht = t[hit] - 1.0 / n[hi]  # back off one sample so the point is outside geometry
            np.minimum.at(res, hi, np.maximum(ht, 0.0))
        return res

    # ------------------------------------------------------------------ spawn helpers
    def open_ground_cells(self):
        """cells (x,y) with no prism at all and modest slope (candidates for spawns/loot/drops)"""
        if self._open_cells is None:
            m = ~self.has_solid
            # also require no tree trunk
            m &= self.TT == EMPTY_T
            jj, ii = np.nonzero(m[::4, ::4])
            xs = self.gx0 + (ii * 4 + 0.5) * RES
            ys = self.gy0 + (jj * 4 + 0.5) * RES
            g = self.ground(xs, ys)
            gx = self.ground(xs + 1.5, ys) - self.ground(xs - 1.5, ys)
            gy = self.ground(xs, ys + 1.5) - self.ground(xs, ys - 1.5)
            flat = np.hypot(gx, gy) / 3.0 < 0.5
            self._open_cells = np.stack([xs[flat], ys[flat], g[flat]], 1)
        return self._open_cells

    def roof_cells(self, min_h=3.0):
        """cells on building/stadium roofs: (x, y, top)"""
        if self._roof_cells is None:
            pid = self.PID[::4, ::4]
            top = self.TOPMAX[::4, ::4]
            jj, ii = np.nonzero(pid >= 0)
            xs = self.gx0 + (ii * 4 + 0.5) * RES
            ys = self.gy0 + (jj * 4 + 0.5) * RES
            tz = top[jj, ii] / 100.0
            kinds = np.array([self.prisms[p]["kind"] != "bridge" for p in pid[jj, ii]])
            g = self.ground(xs, ys)
            ok = kinds & (tz - g > min_h)
            self._roof_cells = np.stack([xs[ok], ys[ok], tz[ok]], 1)
        return self._roof_cells

    def is_open_disc(self, x, y, r, z):
        b, _ = self.disc_block(x, y, r, z, 1.2, 9.0)
        return not b


if __name__ == "__main__":
    import time
    t0 = time.time()
    w = World()
    print("load", time.time() - t0, w.W, w.H)
    print("slot1 used", int((w.T[1] > EMPTY_T).sum()), "solid cells", int(w.has_solid.sum()))
    print("ground origin", w.ground(0, 0), "support", w.support(0, 0, 100))
    rng = np.random.default_rng(0)
    P0 = np.c_[rng.uniform(-400, 400, 300), rng.uniform(-300, 300, 300), np.full(300, 10.0)]
    P1 = P0 + np.c_[rng.uniform(-150, 150, 300), rng.uniform(-150, 150, 300), np.zeros(300)]
    t0 = time.time()
    for _ in range(10):
        v = w.los(P0, P1)
    print("los 300 rays x10", time.time() - t0, v.mean())
    t0 = time.time()
    for _ in range(1000):
        w.disc_block(10.0, 10.0, 2.5, 0.0, 1.2, 9.0)
    print("disc_block x1000", time.time() - t0)
