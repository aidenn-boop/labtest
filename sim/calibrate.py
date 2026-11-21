#!/usr/bin/env python3
"""Mine a mechreplay/1 file (the public sample replay, or a sim trace) for physics and behaviour
statistics. Used to write CALIBRATION.md and to compare sim traces against reality.

  uv run --no-project --with numpy python sim/calibrate.py <replay.jsonl.gz> [--world]
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def pct(v, ps=(10, 50, 90)):
    if not v:
        return None
    v = sorted(v)
    return [round(v[min(len(v) - 1, int(len(v) * p / 100))], 2) for p in ps]


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def load(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as f:
        L = [json.loads(l) for l in f]
    H = L[0]
    R = L[-1] if L[-1].get("type") == "result" else None
    F = [x for x in L[1:] if "m" in x]
    return H, F, R


def analyse(path, world=None):
    H, F, R = load(path)
    out = collections.OrderedDict()
    tick_dt = 1.0 / H.get("tick_hz", 20)
    names = {m["id"]: m["name"] for m in H["mechs"]}
    # ---------------------------------------------------------------- movement
    gs, bs, air_hs = [], [], []
    az_free, az_jet, vz_jet = [], [], []
    prev = {}
    seqs = collections.defaultdict(list)
    for f in F:
        cur = {m[0]: m for m in f["m"]}
        for i, m in cur.items():
            seqs[i].append((f["t"], m))
            p = prev.get(i)
            if p is not None and f["t"] - p[0] == 2:
                p = p[1]
                dt = 2 * tick_dt
                hs = math.hypot(m[1] - p[1], m[2] - p[2]) / dt
                if m[8] & 1 and p[8] & 1:
                    (bs if (m[8] & 2) else gs).append(hs)
                elif not (m[8] & 1) and not (p[8] & 1):
                    air_hs.append(hs)
        prev = {i: (f["t"], m) for i, m in cur.items()}
    for i, s in seqs.items():
        for a, b, c in zip(s, s[1:], s[2:]):
            if b[0] - a[0] == 2 and c[0] - b[0] == 2 and not (a[1][8] & 1) and not (b[1][8] & 1) and not (c[1][8] & 1):
                dt = 2 * tick_dt
                acc = (c[1][3] - 2 * b[1][3] + a[1][3]) / dt ** 2
                v = (c[1][3] - a[1][3]) / (2 * dt)
                if b[1][8] & 2 and a[1][8] & 2 and c[1][8] & 2:
                    az_jet.append(acc)
                    vz_jet.append(v)
                elif not (b[1][8] & 2) and not (a[1][8] & 2):
                    az_free.append(acc)
    out["ground walk speed p10/50/90/99"] = pct(gs, (10, 50, 90, 99))
    out["ground boost speed p10/50/90/99"] = pct(bs, (10, 50, 90, 99))
    out["air horizontal speed p10/50/90/99"] = pct(air_hs, (10, 50, 90, 99))
    out["free-fall vertical accel p10/50/90"] = pct(az_free)
    out["jetting vertical accel p10/50/90"] = pct(az_jet)
    out["jetting vertical speed p50/90/99"] = pct(vz_jet, (50, 90, 99))
    # energy drain/regen
    drains_b, drains_j, regen = [], [], []
    for i, s in seqs.items():
        for a, b in zip(s, s[1:]):
            if b[0] - a[0] != 2:
                continue
            de = (b[1][7] - a[1][7]) / (2 * tick_dt)
            if a[1][8] & 2 and b[1][8] & 2:
                (drains_b if (a[1][8] & 1 and b[1][8] & 1) else drains_j).append(-de)
            elif de > 0:
                regen.append(de)
    out["boost drain e/s p10/50/90"] = pct(drains_b)
    out["jet drain e/s p10/50/90"] = pct(drains_j)
    out["energy regen e/s p50/90"] = pct(regen, (50, 90))
    # hp regen
    hpreg = []
    for i, s in seqs.items():
        for a, b in zip(s, s[1:]):
            if b[0] - a[0] == 2 and b[1][6] > a[1][6] and b[1][9] == a[1][9]:
                hpreg.append((b[1][6] - a[1][6]) / (2 * tick_dt))
    out["hp regen hp/s p10/50/90"] = pct(hpreg)
    # ---------------------------------------------------------------- projectiles
    ppos = collections.defaultdict(list)
    for f in F:
        for p in f["p"]:
            ppos[p[0]].append((f["t"], p))
    gsp, msp_h, maz = [], [], []
    for pid, s in ppos.items():
        if len(s) < 2:
            continue
        kind = s[0][1][1]
        for a, b in zip(s, s[1:]):
            dt = (b[0] - a[0]) * tick_dt
            if dt <= 0:
                continue
            d = math.dist(a[1][2:5], b[1][2:5]) / dt
            if kind == 0:
                gsp.append(d)
            elif kind == 1:
                msp_h.append(math.hypot(b[1][2] - a[1][2], b[1][3] - a[1][3]) / dt)
        if kind == 1 and len(s) >= 3:
            for a, b, c in zip(s, s[1:], s[2:]):
                if b[0] - a[0] == c[0] - b[0]:
                    dt = (b[0] - a[0]) * tick_dt
                    maz.append((c[1][4] - 2 * b[1][4] + a[1][4]) / dt ** 2)
    out["gun bullet speed p10/50/90"] = pct(gsp)
    out["mortar horizontal speed p10/50/90"] = pct(msp_h)
    out["mortar vertical accel p10/50/90"] = pct(maz)
    # ---------------------------------------------------------------- events
    evs = [e for f in F for e in f.get("ev", [])]
    hits = collections.defaultdict(list)
    for e in evs:
        if e["e"] == "hit" and isinstance(e.get("dst"), int) and e["dst"] < 100:
            hits[e["w"]].append(e["amt"])
    for w, v in sorted(hits.items()):
        c = collections.Counter(round(x, 1) for x in v)
        out[f"hit amounts vs mechs [{w}] most common"] = c.most_common(8)
    cnt = collections.Counter(e["e"] for e in evs)
    out["event counts"] = dict(cnt)
    # zone
    zr = [(f["t"] * tick_dt, f["z"][2]) for f in F]
    out["zone radius at t=0,60,110,155,195"] = [r for (t, r) in zr if any(abs(t - x) < 0.051 for x in (0, 60, 110, 155, 195))]
    out["counts crates/pickups/npcs"] = [len(H.get("crates", [])), len(H.get("pickups", [])), len(H.get("npcs", []))]
    if world is not None:
        roof = sum(1 for c in H.get("crates", []) if c["pos"][2] - world.ground(c["pos"][0], c["pos"][1]) > 2.5)
        roofp = sum(1 for c in H.get("pickups", []) if c["pos"][2] - world.ground(c["pos"][0], c["pos"][1]) > 2.5)
        out["roof crates / roof floor-weapons"] = [roof, roofp]
        sp = [m["spawn"] for m in H["mechs"]]
        dmin = min(math.dist(a[:2], b[:2]) for i, a in enumerate(sp) for b in sp[i + 1:])
        out["spawn min separation"] = round(dmin, 1)
        zc = H["zone"][0]["center"] if isinstance(H.get("zone"), list) else [41.3, -72.8]
        out["spawn dist from zone centre max"] = round(max(math.dist(s[:2], zc) for s in sp), 1)
    # ---------------------------------------------------------------- behaviour per bot type
    pos_at = {}
    for f in F:
        pos_at[f["t"]] = {m[0]: m for m in f["m"]}
    ticks = sorted(pos_at)

    def frame_at(t):
        # nearest frame at or before t
        lo, hi = 0, len(ticks) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ticks[mid] <= t:
                lo = mid
            else:
                hi = mid - 1
        return pos_at[ticks[lo]]

    beh = collections.defaultdict(lambda: collections.defaultdict(list))
    fires = collections.Counter()
    for e in evs:
        if e["e"] == "fire":
            fires[(e["id"], e["w"])] += 1
            fr = frame_at(e["t"])
            me = fr.get(e["id"])
            if me is None:
                continue
            # nearest enemy within 25 deg of aim
            best = None
            for j, o in fr.items():
                if j == e["id"]:
                    continue
                d = math.dist(me[1:3], o[1:3])
                ang = math.atan2(o[2] - me[2], o[1] - me[1])
                err = abs(wrap(ang - me[4]))
                if err < math.radians(25) and (best is None or d < best[0]):
                    best = (d, err, o)
            if best:
                beh[names[e["id"]]][f"fire_dist_{e['w']}"].append(best[0])
                beh[names[e["id"]]][f"aim_err_deg_{e['w']}"].append(math.degrees(best[1]))
    gun_hits = collections.Counter()
    for e in evs:
        if e["e"] == "hit" and isinstance(e.get("src"), int) and e["src"] < 16:
            gun_hits[(e["src"], e["w"], "mech" if e["dst"] < 100 else ("npc" if e["dst"] < 1000 else "crate"))] += 1
    for i in names:
        n = names[i]
        f = fires[(i, "gun")]
        h = sum(gun_hits[(i, "gun", k)] for k in ("mech", "npc", "crate"))
        if f:
            beh[n]["gun_hit_rate"].append(h / f)
        # air time, boost time, roof time
        s = seqs[i]
        if s:
            beh[n]["air_frac"].append(sum(1 for _, m in s if not m[8] & 1) / len(s))
            beh[n]["boost_frac"].append(sum(1 for _, m in s if m[8] & 2) / len(s))
            if world is not None:
                beh[n]["roof_frac"].append(sum(1 for _, m in s if m[8] & 1 and m[3] - world.ground(m[1], m[2]) > 3.0) / len(s))
            beh[n]["life_s"].append(len(s) * 2 * tick_dt)
            ffire = [e["t"] * tick_dt for e in evs if e["e"] == "hit" and e.get("src") == i and isinstance(e.get("dst"), int) and e["dst"] < 16]
            if ffire:
                beh[n]["first_mech_hit_t"].append(ffire[0])
    out["behaviour"] = {}
    for n, d in sorted(beh.items()):
        out["behaviour"][n] = {k: (round(st.fmean(v), 3) if k in ("gun_hit_rate", "air_frac", "boost_frac", "roof_frac", "life_s", "first_mech_hit_t") else pct(v)) for k, v in d.items()}
    if R:
        out["result"] = [(m["id"], m["name"], m["place"], m["kills"], m.get("death_t"), m.get("killed_with"), m.get("loadout"), m.get("first_weapon_t"), m.get("cubes_max")) for m in R["mechs"]]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--world", action="store_true", help="load the map for roof/terrain statistics")
    a = ap.parse_args()
    w = None
    if a.world:
        from world import World
        w = World()
    res = analyse(a.path, w)
    for k, v in res.items():
        if k == "behaviour":
            print("behaviour by bot type:")
            for n, d in v.items():
                print(f"  {n}: {json.dumps(d)}")
        elif k == "result":
            print("result:")
            for r in v:
                print("  ", r)
        else:
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
