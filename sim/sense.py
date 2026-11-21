"""Vision, hearing and observation building (protocol.md §6, §8)."""
from __future__ import annotations

import math

import numpy as np

from engine import wrap, r2


def _r3(v):
    return round(float(v), 3)


class Senses:
    """Per-decision visibility, batched across observers to amortise numpy overhead."""

    def __init__(self, match):
        self.M = match
        self.vis_cache = {}

    def visible_set(self, observers, want_items, focus=None):
        """observers: list of mechs. Returns {mech_id: {"actors": set(ids), "items": set(keys), "proj": set(ids)}}.
        Items/projectiles only for observers in want_items."""
        M = self.M
        W = M.world
        vc = M.cfg["vision"]
        rays0, rays1, owner = [], [], []
        near_flags = []
        out = {}
        actors = []
        for m in M.mechs:
            if m.alive:
                actors.append(("m", m.id, m.pos, m.stats["height"]))
        nh = M.cfg["npc"]["height"]
        for n in M.npcs:
            if n.alive:
                actors.append(("n", n.id, n.pos, nh))
        for ob in observers:
            st = ob.stats
            eye = (ob.pos[0], ob.pos[1], ob.pos[2] + st["eye_height"])
            near_r = vc["near_range"] + st.get("near_range_bonus", 0)
            far_r = vc["range"] + st.get("vision_bonus", 0)
            half = math.radians(vc["fov_deg"]) / 2
            yaw = ob.aim[0]
            out[ob.id] = {"actors": set(), "items": set(), "proj": set()}
            cand = []
            foc = focus.get(ob.id) if focus else None
            for a in actors:
                if a[0] == "m" and a[1] == ob.id:
                    continue
                if foc is not None:
                    # restricted observer (squad house bot): only the focus mechs, plus NPCs nearby
                    if a[0] == "m" and a[1] not in foc:
                        continue
                    if a[0] == "n" and (abs(a[2][0] - eye[0]) > 60 or abs(a[2][1] - eye[1]) > 60):
                        continue
                cand.append((a[0], a[1], a[2], a[3]))
            if ob.id in want_items:
                ch = M.cfg["crates"]["height"]
                for c in M.crates.values():
                    if c["alive"]:
                        cand.append(("c", c["id"], c["pos"], ch))
                for c in M.cubes.values():
                    cand.append(("q", c["id"], c["pos"], 0.6))
                for p in M.pickups.values():
                    cand.append(("w", p["id"], p["pos"], 1.0))
                for p in M.projs:
                    if p.owner != ob.id:
                        cand.append(("p", p.id, (p.pos[0], p.pos[1], p.pos[2]), 0.0))
            for kind, i, pos, h in cand:
                dx, dy = pos[0] - eye[0], pos[1] - eye[1]
                if abs(dx) > far_r or abs(dy) > far_r:
                    continue
                cz = pos[2] + h / 2
                d = math.sqrt(dx * dx + dy * dy + (cz - eye[2]) ** 2)
                near = d <= near_r
                if not near:
                    if d > far_r:
                        continue
                    if abs(wrap(math.atan2(dy, dx) - yaw)) > half:
                        continue
                rays0.append(eye)
                rays1.append((pos[0], pos[1], cz))
                owner.append((ob.id, kind, i))
                near_flags.append(near)
                if h > 0:
                    rays0.append(eye)
                    rays1.append((pos[0], pos[1], pos[2] + h))
                    owner.append((ob.id, kind, i))
                    near_flags.append(near)
        if rays0:
            nf = np.array(near_flags)
            R0 = np.array(rays0)
            R1 = np.array(rays1)
            ok = np.zeros(len(R0), dtype=bool)
            step = M.sim["los_step"]
            if nf.any():
                ok[nf] = W.los(R0[nf], R1[nf], vision=not M.cfg["vision"].get("canopy_blocks", True) and False, step=step)
            if (~nf).any():
                ok[~nf] = W.los(R0[~nf], R1[~nf], vision=M.cfg["vision"].get("canopy_blocks", True), step=step)
            for (oid, kind, i), v in zip(owner, ok):
                if v:
                    if kind in ("m", "n"):
                        out[oid]["actors"].add(i)
                    elif kind == "p":
                        out[oid]["proj"].add(i)
                    else:
                        out[oid]["items"].add((kind, i))
        return out

    def point_visible(self, ob, pts):
        """visibility of arbitrary points (for beams) from observer ob"""
        M = self.M
        vc = M.cfg["vision"]
        st = ob.stats
        eye = (ob.pos[0], ob.pos[1], ob.pos[2] + st["eye_height"])
        near_r = vc["near_range"] + st.get("near_range_bonus", 0)
        far_r = vc["range"] + st.get("vision_bonus", 0)
        half = math.radians(vc["fov_deg"]) / 2
        idx, P1, nflag = [], [], []
        for k, p in enumerate(pts):
            dx, dy = p[0] - eye[0], p[1] - eye[1]
            d = math.sqrt(dx * dx + dy * dy + (p[2] - eye[2]) ** 2)
            near = d <= near_r
            if not near and (d > far_r or abs(wrap(math.atan2(dy, dx) - ob.aim[0])) > half):
                continue
            idx.append(k)
            P1.append(p)
            nflag.append(near)
        if not idx:
            return False
        ok = M.world.los(np.array([eye] * len(P1)), np.array(P1), vision=False, step=1.0)
        return bool(ok.any())


def actor_obs_mech(M, o):
    d = {"id": o.id, "kind": "mech", "pos": [r2(o.pos[0]), r2(o.pos[1]), r2(o.pos[2])], "vel": [r2(o.vel[0]), r2(o.vel[1]), r2(o.vel[2])],
         "aim": [_r3(o.aim[0]), _r3(o.aim[1])]}
    if M.cfg["vision"].get("show_enemy_hp", True):
        d["hp"] = round(o.hp, 2)
        d["max_hp"] = round(o.max_hp, 2)
    d["cubes"] = o.cubes
    d["weapons"] = [w.kind for w in o.weapons if w is not None]
    d["charging"] = any(w is not None and w.kind == "laser" and w.charging for w in o.weapons)
    return d


def actor_obs_npc(M, n):
    d = {"id": n.id, "kind": "npc", "pos": [r2(n.pos[0]), r2(n.pos[1]), r2(n.pos[2])], "vel": [r2(n.vel[0]), r2(n.vel[1]), r2(n.vel[2])],
         "aim": [_r3(n.yaw), 0.0]}
    if M.cfg["vision"].get("show_enemy_hp", True):
        d["hp"] = round(n.hp)
        d["max_hp"] = round(M.cfg["npc"]["hp"])
    return d


def weapon_obs(m, w):
    ws = m.wstats[w.kind]
    if w.kind == "gun":
        keys = ("damage", "cooldown", "speed", "range", "spread_deg", "mag", "reload")
    elif w.kind == "mortar":
        keys = ("damage", "splash", "splash_radius", "splash_edge", "cooldown", "speed", "gravity", "ttl", "knock", "knock_up", "drain")
    elif w.kind == "laser":
        keys = ("damage", "charge", "min_charge", "min_frac", "hold_s", "cooldown", "range", "pierce", "charge_turn_mult", "cookoff")
    else:
        keys = ("damage", "cooldown", "range", "cone_deg", "chain", "chain_range", "chain_falloff", "energy", "short_s")
    st = {k: (round(ws[k], 4) if isinstance(ws[k], float) else ws[k]) for k in keys if k in ws}
    if "mag" in st:
        st["mag"] = int(round(st["mag"]))
    d = {"kind": w.kind, "cooldown": round(max(0.0, w.cooldown), 2), "stats": st}
    if w.kind == "gun":
        d["ammo"] = int(w.ammo)
        d["reload"] = round(max(0.0, w.reload), 2)
    if w.kind == "laser":
        d["charge"] = round(w.charge, 2) if w.charging else 0
    return d


def self_obs(M, m):
    st = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.stats.items()}
    return {"id": m.id, "pos": [r2(m.pos[0]), r2(m.pos[1]), r2(m.pos[2])], "vel": [r2(m.vel[0]), r2(m.vel[1]), r2(m.vel[2])],
            "aim": [_r3(m.aim[0]), _r3(m.aim[1])], "hp": round(m.hp, 2), "max_hp": round(m.max_hp, 2), "energy": round(m.energy, 1),
            "max_energy": round(m.stats["energy"], 1), "grounded": bool(m.grounded), "cubes": m.cubes, "upgrades": list(m.upgrades),
            "weapons": [weapon_obs(m, w) if w is not None else None for w in m.weapons], "in_zone": bool(m.in_zone), "stats": st}


def zone_obs(M):
    z = M._zone_now if getattr(M, "_zone_now", None) else M.zone_state()
    return {"center": [r2(z["center"][0]), r2(z["center"][1])], "radius": r2(z["radius"]), "next_center": [r2(z["next_center"][0]), r2(z["next_center"][1])],
            "next_radius": r2(z["next_radius"]), "shrink_start": r2(z["shrink_start"]), "shrink_end": r2(z["shrink_end"]), "dps": z["dps"]}


def drops_obs(M):
    out = []
    dc = M.cfg["drops"]
    for d in M.drops:
        if d["called"] and not d["taken"]:
            out.append({"id": d["id"], "pos": [r2(v) for v in d["pos"]], "radius": dc["radius"], "lands_in": r2(max(0.0, d["land_t"] - M.t)),
                        "capture_s": dc["capture_s"], "progress": r2(d["progress"]), "holder": d["holder"]})
    return out


def build_obs(M, m, vis, full=True, shared_actor_ids=None):
    """Observation dict for mech m. vis = Senses.visible_set()[m.id]. full=False builds a cheaper
    observation for built-in bots (items within builtin_item_radius without LOS, no projectiles/sounds/beams)."""
    t = M.t
    mem = M.cfg["vision"]["memory_s"]
    for i in vis["actors"]:
        m.seen_t[i] = t
    if shared_actor_ids:
        for i in shared_actor_ids:
            m.seen_t[i] = t
    mechs = []
    if full:
        for o in M.mechs:
            if o.id == m.id or not o.alive:
                continue
            st = m.seen_t.get(o.id)
            if st is not None and t - st <= mem + 1e-9:
                mechs.append(actor_obs_mech(M, o))
        for n in M.npcs:
            if not n.alive:
                continue
            st = m.seen_t.get(n.id)
            if st is not None and t - st <= mem + 1e-9:
                mechs.append(actor_obs_npc(M, n))
    else:
        cache = getattr(M, "_actor_cache", None)
        if cache is None or cache[0] != M.tick:
            d = {}
            for o in M.mechs:
                if o.alive:
                    d[o.id] = {"id": o.id, "kind": "mech", "pos": list(o.pos), "vel": list(o.vel), "aim": list(o.aim), "hp": o.hp, "max_hp": o.max_hp,
                               "cubes": o.cubes, "weapons": [w.kind for w in o.weapons if w is not None],
                               "charging": any(w is not None and w.kind == "laser" and w.charging for w in o.weapons)}
            for n in M.npcs:
                if n.alive:
                    d[n.id] = {"id": n.id, "kind": "npc", "pos": list(n.pos), "vel": list(n.vel), "aim": [n.yaw, 0.0], "hp": n.hp, "max_hp": M.cfg["npc"]["hp"]}
            cache = (M.tick, d)
            M._actor_cache = cache
        for i, st in m.seen_t.items():
            if t - st <= mem + 1e-9 and i != m.id and i in cache[1]:
                mechs.append(cache[1][i])
    crates, cubes, pickups, projectiles, sounds, beams = [], [], [], [], [], []
    if full:
        items = vis["items"]
        for c in M.crates.values():
            if c["alive"] and ("c", c["id"]) in items:
                crates.append({"id": c["id"], "pos": [r2(v) for v in c["pos"]], "hp": round(c["hp"])})
        for c in M.cubes.values():
            if ("q", c["id"]) in items:
                cubes.append({"id": c["id"], "pos": [r2(v) for v in c["pos"]]})
        for p in M.pickups.values():
            if ("w", p["id"]) in items:
                pickups.append({"id": p["id"], "kind": p["kind"], "pos": [r2(v) for v in p["pos"]]})
        for p in M.projs:
            if p.id in vis["proj"]:
                projectiles.append({"id": p.id, "kind": p.kind, "pos": [r2(v) for v in p.pos], "vel": [r2(v) for v in p.vel]})
        # sounds since last obs
        sc = M.cfg["sound"]
        sig = math.radians(sc["bearing_noise_deg"]) * m.stats.get("hearing", 1.0)
        for (st_, kind, pos, src) in M.sounds:
            if st_ <= m.last_obs_t - 1e-9 or src == m.id:
                continue
            d = math.hypot(pos[0] - m.pos[0], pos[1] - m.pos[1])
            if d > sc["range"].get(kind, 0):
                continue
            b = math.atan2(pos[1] - m.pos[1], pos[0] - m.pos[0]) + M.rng.gauss(0, sig)
            sounds.append({"kind": kind, "bearing": _r3(wrap(b)), "dist": "near" if d < sc["near"] else ("mid" if d < sc["mid"] else "far"),
                           "age": r2(max(0.0, t - st_))})
        sense = M._senses
        for (bt, by, kind, path) in M.beams:
            if bt <= m.last_obs_t - 1e-9 or by == m.id:
                continue
            pts = list(path)
            for a, b in zip(path, path[1:]):
                pts.append([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2])
            if sense.point_visible(m, pts):
                beams.append({"by": by, "kind": kind, "path": [[r2(v) for v in p] for p in path], "age": r2(max(0.0, t - bt))})
    else:
        R = M.sim["builtin_item_radius"]
        x, y = m.pos[0], m.pos[1]
        for c in M.crates.values():
            if c["alive"] and abs(c["pos"][0] - x) < R and abs(c["pos"][1] - y) < R:
                crates.append({"id": c["id"], "pos": c["pos"], "hp": c["hp"]})
        for c in M.cubes.values():
            if abs(c["pos"][0] - x) < R and abs(c["pos"][1] - y) < R:
                cubes.append({"id": c["id"], "pos": c["pos"]})
        for p in M.pickups.values():
            if abs(p["pos"][0] - x) < R and abs(p["pos"][1] - y) < R:
                pickups.append({"id": p["id"], "kind": p["kind"], "pos": p["pos"]})
        for p in M.projs:
            if p.owner != m.id and abs(p.pos[0] - x) < 60 and abs(p.pos[1] - y) < 60:
                projectiles.append({"id": p.id, "kind": p.kind, "pos": [float(v) for v in p.pos], "vel": [float(v) for v in p.vel]})
    events = m.events
    za = getattr(m, "_zone_acc", 0.0)
    if za > 0:
        events = events + [{"type": "damaged", "amount": round(za, 1), "weapon": "zone", "bearing": None, "by": None}]
        m._zone_acc = 0.0
    m.events = []
    m.last_obs_t = t
    if full:
        so = self_obs(M, m)
    else:
        so = {"id": m.id, "pos": list(m.pos), "vel": list(m.vel), "aim": list(m.aim), "hp": m.hp, "max_hp": m.max_hp, "energy": m.energy,
              "max_energy": m.stats["energy"], "grounded": m.grounded, "cubes": m.cubes, "upgrades": m.upgrades,
              "weapons": [weapon_obs(m, w) if w is not None else None for w in m.weapons], "in_zone": m.in_zone, "stats": m.stats}
    return {"type": "obs", "tick": M.tick, "t": round(t, 2), "alive": M.alive_count, "self": so, "mechs": mechs,
            "projectiles": projectiles, "crates": crates, "cubes": cubes, "pickups": pickups, "sounds": sounds, "beams": beams,
            "events": events, "zone": zone_obs(M), "offer": m.offer, "drops": drops_obs(M)}
