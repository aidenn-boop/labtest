"""Built-in bots (in-process). They read the same observation dicts as external bots
(the cheap `full=False` variant) plus optional squad intel, and return protocol actions.

Tiers model the practice house squad (easy / medium / hard / expert). Parameters are
inferred from the public replay (see CALIBRATION.md) and are deliberately simple.
"""
from __future__ import annotations

import math
import random

import numpy as np

TAU = 2 * math.pi


def wrap(a):
    return (a + math.pi) % TAU - math.pi


TIERS = {
    # aim_sigma: per-decision aim noise (deg); bias: slowly varying aim offset (deg); lead: fraction of
    # correct lead; react: seconds before firing at a newly acquired target; range: max engage range;
    # pref: preferred fighting distance; strafe: lateral movement amplitude 0..1; loot: takes weapons;
    # retreat: HP fraction to disengage and heal (0 = never); burst: fire only when aim error < tol deg
    "easy":   dict(aim_sigma=2.2, bias=1.6, lead=0.0, react=0.9, range=120, pref=70, strafe=0.0, loot=False, retreat=0.0, tol=4.0, jets=False, hop=3, jink=(1.2, 2.5), secondary=False),
    "medium": dict(aim_sigma=1.4, bias=1.0, lead=0.5, react=0.6, range=160, pref=95, strafe=0.4, loot=True, retreat=0.0, tol=3.0, jets=True, hop=4, jink=(0.5, 1.2), secondary=True),
    "hard":   dict(aim_sigma=0.8, bias=0.5, lead=0.85, react=0.35, range=200, pref=115, strafe=0.7, loot=True, retreat=0.25, tol=2.0, jets=True, hop=6, jink=(0.4, 0.9), secondary=True),
    "expert": dict(aim_sigma=0.45, bias=0.25, lead=1.0, react=0.2, range=220, pref=130, strafe=1.0, loot=True, retreat=0.35, tol=1.5, jets=True, hop=6, jink=(0.4, 0.8), secondary=True),
    # FFA archetypes (scripted opponents)
    "brawler": dict(aim_sigma=0.8, bias=0.5, lead=0.85, react=0.3, range=150, pref=30, strafe=0.8, loot=True, retreat=0.0, tol=2.5, jets=True, hop=7, jink=(0.4, 0.9), secondary=True),
    "sniper":  dict(aim_sigma=0.5, bias=0.3, lead=1.0, react=0.3, range=230, pref=150, strafe=0.3, loot=True, retreat=0.4, tol=1.2, jets=True, hop=6, jink=(0.5, 1.0), secondary=True),
    "camper":  dict(aim_sigma=0.7, bias=0.4, lead=0.9, react=0.3, range=200, pref=120, strafe=0.2, loot=False, retreat=0.5, tol=1.5, jets=False, hop=2, jink=(1.0, 2.0), secondary=False),
    "idle":    dict(aim_sigma=9, bias=0, lead=0, react=9, range=0, pref=0, strafe=0, loot=False, retreat=0, tol=0, jets=False, hop=0, jink=(1, 2), secondary=False),
}

UPGRADE_PREF = ["armor", "plating", "nanites", "gun_damage", "gun_rate", "laser_damage", "servos", "gyros", "lightning_efficiency",
                "mortar_reload", "capacitor", "reactor", "jets", "gun_mag", "gun_velocity", "laser_charge", "sensors"]


class BuiltinBot:
    def __init__(self, tier, world, cfg, seed, slot, nav=None):
        self.nav = nav
        self.tier = tier
        self.p = dict(TIERS[tier])
        self.W = world
        self.cfg = cfg
        self.rng = random.Random(seed * 7919 + slot)
        self.slot = slot
        self.target = None
        self.target_since = 0.0
        self.bias = [0.0, 0.0]
        self.prev_pos = None
        self.stuck_t = 0.0
        self.detour = 0
        self.detour_until = 0.0
        self.jetting = False
        self.strafe_dir = 1
        self.strafe_t = 0.0
        self.wander = None
        self.last_known = None
        self.laser_hold = False
        self.healing = False
        self.hop_next = self.rng.uniform(3, 12)
        self.hop_z0 = None
        self.name = f"house:{tier}"

    # -------------------------------------------------------------- helpers
    def _can_shoot(self, me, tpos, theight):
        mz = me["pos"][2] + me["stats"]["muzzle_height"]
        a = [me["pos"][0], me["pos"][1], mz]
        b = [tpos[0], tpos[1], tpos[2] + theight * 0.5]
        return bool(self.W.los(np.array([a]), np.array([b]), vision=False, step=1.0)[0])

    def _steer(self, me, goal, t, boost_ok=True):
        """local obstacle avoidance: returns (move, jump, boost)"""
        x, y, z = me["pos"]
        dx, dy = goal[0] - x, goal[1] - y
        L = math.hypot(dx, dy)
        if L < 1.0:
            return [0.0, 0.0], False, False
        yaw = math.atan2(dy, dx)
        r = me["stats"]["radius"]
        jump = False
        boost = False
        # stuck detection
        if self.prev_pos is not None:
            moved = math.hypot(x - self.prev_pos[0], y - self.prev_pos[1])
            if moved < 0.25 and me["grounded"]:
                self.stuck_t += 0.1
            else:
                self.stuck_t = max(0.0, self.stuck_t - 0.05)
        self.prev_pos = (x, y)
        look = 6.0
        chosen = None
        if t >= self.detour_until:
            self.detour = 0
        offs = [0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5, 2.2, -2.2] if self.detour == 0 else [self.detour * k for k in (0.0, 0.5, 1.0, 1.5)] + [0, -0.5, -1.0]
        for o in offs:
            a = yaw + o
            px, py = x + math.cos(a) * look, y + math.sin(a) * look
            b, _ = self.W.disc_block(px, py, r, z, 1.2, me["stats"]["height"])
            if not b:
                chosen = a
                if o != 0 and self.detour == 0:
                    self.detour = 1 if o > 0 else -1
                    self.detour_until = t + 2.0
                break
        if chosen is None:
            chosen = yaw
            if self.p["jets"] or self.stuck_t > 0.6:
                jump = True
        if self.stuck_t > 0.8:
            jump = True
            self.stuck_t = 0.0
            self.detour = -self.detour if self.detour else (1 if self.rng.random() < 0.5 else -1)
            self.detour_until = t + 2.5
        if jump and me["grounded"]:
            self.jetting = True
        if not me["grounded"] and self.jetting:
            boost = me["energy"] > 5
            if me["vel"][2] < -2 or me["energy"] < 5:
                self.jetting = False
        elif me["grounded"]:
            self.jetting = False if not jump else True
            boost = boost_ok and me["energy"] > 40 and L > 40
        return [math.cos(chosen), math.sin(chosen)], jump, boost

    def _zone_goal(self, obs, me):
        z = obs["zone"]
        x, y = me["pos"][0], me["pos"][1]
        t = obs["t"]
        cx, cy = z["center"]
        R = z["radius"]
        nx, ny = z["next_center"]
        nR = z["next_radius"]
        d_now = math.hypot(x - cx, y - cy)
        d_next = math.hypot(x - nx, y - ny)
        time_to_shrink = z["shrink_start"] - t
        speed = me["stats"]["walk_speed"]
        need = (d_next - nR * 0.75) / speed
        if d_now > R * 0.92 or (d_next > nR * 0.8 and time_to_shrink < need + 15) or (t > z["shrink_start"] and d_next > nR * 0.8):
            return (nx, ny), True
        return None, False

    def _pick_upgrade(self, offer):
        best = 0
        bi = 99
        for i, c in enumerate(offer["choices"]):
            k = UPGRADE_PREF.index(c["id"]) if c["id"] in UPGRADE_PREF else 50
            if k < bi:
                bi, best = k, i
        return best

    def _aim_at(self, me, tgt, ws_speed, lead):
        mz = me["pos"][2] + me["stats"]["muzzle_height"]
        tp = np.array([tgt["pos"][0], tgt["pos"][1], tgt["pos"][2] + (4.5 if tgt.get("kind") == "mech" else 1.7)])
        tv = np.array(tgt["vel"], dtype=float)
        o = np.array([me["pos"][0], me["pos"][1], mz])
        aimp = tp
        if lead > 0 and ws_speed:
            tt = np.linalg.norm(tp - o) / ws_speed
            for _ in range(2):
                aimp = tp + tv * tt * lead
                tt = np.linalg.norm(aimp - o) / ws_speed
        d = aimp - o
        yaw = math.atan2(d[1], d[0])
        pitch = math.atan2(d[2], math.hypot(d[0], d[1]))
        return yaw, pitch, float(np.linalg.norm(d))

    @staticmethod
    def _mortar_pitch(dist, dz, v, g):
        # low arc solution
        v2 = v * v
        disc = v2 * v2 - g * (g * dist * dist + 2 * dz * v2)
        if disc < 0:
            return None
        return math.atan2(v2 - math.sqrt(disc), g * dist)

    # -------------------------------------------------------------- main
    def act(self, obs, intel=None):
        me = obs["self"]
        t = obs["t"]
        a = {"tick": obs["tick"]}
        if self.tier == "idle":
            return a
        if obs.get("offer"):
            a["pick"] = self._pick_upgrade(obs["offer"])
        P = self.p
        x, y, z = me["pos"]
        intel = intel or {}
        # --- target selection
        visible = [m for m in obs["mechs"] if m["kind"] == "mech"]
        allowed = intel.get("allowed_targets")  # squad mode: set of ids we may fight
        engaged = intel.get("engaged", True)
        cands = [m for m in visible if (allowed is None or m["id"] in allowed)]
        tgt = None
        if cands:
            def score(m):
                d = math.dist(m["pos"], me["pos"])
                return d * (0.6 + 0.4 * m.get("hp", 1400) / max(1, m.get("max_hp", 1400)))
            cands.sort(key=score)
            tgt = cands[0]
            if math.dist(tgt["pos"], me["pos"]) > P["range"] and not engaged:
                tgt = None
        hunt_pos = intel.get("hunt_pos")
        if tgt is not None and (self.target != tgt["id"]):
            self.target = tgt["id"]
            self.target_since = t
            self.bias = [self.rng.gauss(0, math.radians(P["bias"])), self.rng.gauss(0, math.radians(P["bias"]) * 0.6)]
        if tgt is None:
            self.target = None
        # NPC / crate targets for looting
        npc_t = None
        if tgt is None:
            npcs = [m for m in visible if False] + [m for m in obs["mechs"] if m["kind"] == "npc" and math.dist(m["pos"], me["pos"]) < 60]
            if npcs and (P["loot"] or any(math.dist(n["pos"], me["pos"]) < 35 for n in npcs)):
                npc_t = min(npcs, key=lambda n: math.dist(n["pos"], me["pos"]))
        # --- movement goal
        zgoal, urgent = self._zone_goal(obs, me)
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        if P["retreat"] > 0 and hp_frac < P["retreat"]:
            self.healing = True
        if self.healing and hp_frac > 0.85:
            self.healing = False
        goal = None
        boost_ok = True
        if urgent:
            goal = zgoal
        elif tgt is not None and not self.healing:
            d = math.dist(tgt["pos"][:2], me["pos"][:2])
            ang = math.atan2(tgt["pos"][1] - y, tgt["pos"][0] - x)
            self.strafe_t -= 0.1
            if self.strafe_t <= 0:
                self.strafe_dir = -self.strafe_dir
                self.strafe_t = self.rng.uniform(*P["jink"])
            if d > P["pref"] * 1.25:
                goal = (tgt["pos"][0], tgt["pos"][1])
            elif d < P["pref"] * 0.6:
                goal = (x - math.cos(ang) * 20, y - math.sin(ang) * 20)
            else:
                goal = None
            if P["strafe"] > 0:
                sx, sy = -math.sin(ang) * self.strafe_dir, math.cos(ang) * self.strafe_dir
                if goal is None:
                    goal = (x + sx * 15, y + sy * 15)
                else:
                    gx, gy = goal[0] - x, goal[1] - y
                    gl = max(1e-6, math.hypot(gx, gy))
                    goal = (x + gx / gl * 15 + sx * 15 * P["strafe"], y + gy / gl * 15 + sy * 15 * P["strafe"])
            boost_ok = False
        elif self.healing and tgt is not None:
            ang = math.atan2(tgt["pos"][1] - y, tgt["pos"][0] - x)
            goal = (x - math.cos(ang) * 30, y - math.sin(ang) * 30)
        elif hunt_pos is not None and engaged:
            goal = (hunt_pos[0], hunt_pos[1])
        else:
            # loot or wander inside the zone
            if P["loot"] and me["weapons"][1] is None and obs["pickups"]:
                pk = min(obs["pickups"], key=lambda p: math.dist(p["pos"], me["pos"]))
                if abs(pk["pos"][2] - z) < 4 or P["jets"]:
                    goal = (pk["pos"][0], pk["pos"][1])
            if goal is None and obs["cubes"]:
                c = min(obs["cubes"], key=lambda p: math.dist(p["pos"], me["pos"]))
                if abs(c["pos"][2] - z) < 4:
                    goal = (c["pos"][0], c["pos"][1])
            if goal is None and npc_t is not None:
                goal = (npc_t["pos"][0], npc_t["pos"][1]) if math.dist(npc_t["pos"], me["pos"]) > 25 else None
            if goal is None:
                zc = obs["zone"]["next_center"]
                if self.wander is None or math.hypot(self.wander[0] - x, self.wander[1] - y) < 8 or self.rng.random() < 0.01:
                    R = obs["zone"]["next_radius"] * 0.6
                    a_ = self.rng.random() * TAU
                    rr = R * math.sqrt(self.rng.random())
                    self.wander = (zc[0] + rr * math.cos(a_), zc[1] + rr * math.sin(a_))
                goal = self.wander
        if urgent and self.nav is not None:
            z_ = obs["zone"]
            wp = self.nav.waypoint(z_["next_center"], z_["next_radius"], x, y)
            if wp is not None:
                goal = wp
        if goal is not None:
            mv, jump, boost = self._steer(me, goal, t, boost_ok=boost_ok or urgent)
            if urgent and not me["in_zone"]:
                boost = boost or (me["grounded"] and me["energy"] > 10)
        else:
            mv, jump, boost = [0.0, 0.0], False, False
            self.prev_pos = (x, y)
        # jet hops (replay: ~6/min, ~3 s airborne, +14 m, many shots fired mid-air)
        if P["hop"] > 0:
            if self.hop_z0 is None and me["grounded"] and t >= self.hop_next and me["energy"] > 45 and (tgt is not None or goal is not None):
                self.hop_z0 = z
                jump = True
            if self.hop_z0 is not None:
                if not me["grounded"] and z < self.hop_z0 + 12.2 and me["energy"] > 5 and me["vel"][2] > -1:
                    boost = True
                elif me["grounded"] and not jump:
                    self.hop_z0 = None
                    self.hop_next = t + self.rng.expovariate(P["hop"] / 60.0) + 2.0
        a.update(move=mv, jump=jump, boost=boost)
        # take pickups in reach
        if P["loot"]:
            reach = self.cfg["loot"]["pickup_radius"]
            for pk in obs["pickups"]:
                if math.hypot(pk["pos"][0] - x, pk["pos"][1] - y) < reach - 0.3 and abs(pk["pos"][2] - z) < 3.5:
                    held = [w["kind"] for w in me["weapons"] if w]
                    if pk["kind"] in held:
                        continue
                    if me["weapons"][1] is None:
                        a["take"] = pk["id"]
                    break
        # --- aim & fire
        fire = [False, False]
        aim_t = tgt or npc_t
        gun = me["weapons"][0]
        sec = me["weapons"][1]
        if aim_t is not None:
            gspeed = gun["stats"]["speed"] if gun and gun["kind"] == "gun" else 252
            yaw, pitch, dist = self._aim_at(me, aim_t, gspeed, P["lead"])
            yaw += self.bias[0] + self.rng.gauss(0, math.radians(P["aim_sigma"]))
            pitch += self.bias[1] + self.rng.gauss(0, math.radians(P["aim_sigma"]) * 0.6)
            a["aim"] = [yaw, pitch]
            err = abs(wrap(me["aim"][0] - yaw))
            ready = (t - self.target_since >= P["react"]) or aim_t is npc_t
            theight = 9.0 if aim_t.get("kind") == "mech" else 3.5
            if ready and dist < P["range"] + 10 and err < math.radians(P["tol"] + 2.0):
                if self._can_shoot(me, aim_t["pos"], theight):
                    if gun and gun["kind"] == "gun" and dist <= gun["stats"]["range"]:
                        fire[0] = True
                    if sec and P["secondary"]:
                        k = sec["kind"]
                        if k == "lightning" and dist < sec["stats"]["range"] * 0.95 and me["energy"] > 20:
                            fire[1] = True
                        elif k == "laser" and dist < sec["stats"]["range"]:
                            # charge to full, release when on target
                            if sec.get("charge", 0) >= sec["stats"]["charge"] - 0.05 and err < math.radians(P["tol"]):
                                fire[1] = False
                            else:
                                fire[1] = True
                        elif k == "mortar" and dist > 25:
                            pass
            if sec and sec["kind"] == "mortar" and P["secondary"] and ready and 25 < dist < 220 and sec["cooldown"] <= 0:
                g = sec["stats"]["gravity"]
                v = sec["stats"]["speed"]
                tz = aim_t["pos"][2] - (z + me["stats"]["height"] + 0.5)
                dh = math.dist(aim_t["pos"][:2], me["pos"][:2])
                pm = self._mortar_pitch(dh, tz, v, g)
                if pm is not None:
                    a["aim"] = [yaw, pm]
                    if abs(me["aim"][1] - pm) < 0.08 and err < math.radians(P["tol"] + 2.5):
                        fire = [False, True]
        else:
            # look where we walk, or toward hunt target / last sound
            mv = a.get("move", [0, 0])
            if hunt_pos is not None and engaged:
                a["aim"] = [math.atan2(hunt_pos[1] - y, hunt_pos[0] - x), 0.0]
            elif mv[0] or mv[1]:
                a["aim"] = [math.atan2(mv[1], mv[0]), 0.0]
            # shoot crates in reach while looting
            if P["loot"] and obs["crates"]:
                c = min(obs["crates"], key=lambda c: math.dist(c["pos"], me["pos"]))
                d = math.dist(c["pos"], me["pos"])
                if d < 60:
                    cz = c["pos"][2] + 1.2
                    mz = z + me["stats"]["muzzle_height"]
                    a["aim"] = [math.atan2(c["pos"][1] - y, c["pos"][0] - x), math.atan2(cz - mz, max(1.0, math.hypot(c["pos"][0] - x, c["pos"][1] - y)))]
                    if abs(wrap(me["aim"][0] - a["aim"][0])) < 0.05 and self._can_shoot(me, c["pos"], 2.4):
                        fire[0] = True
            if sec and sec["kind"] == "laser" and sec.get("charge", 0) > 0:
                fire[1] = True  # keep holding (fires on its own after hold_s); avoid wasting
        a["fire"] = fire
        if gun and gun["kind"] == "gun" and gun["ammo"] < gun["stats"]["mag"] * 0.4 and aim_t is None and gun["reload"] == 0:
            a["reload"] = True
        return a


class NavContext:
    """per-match cache of zone flow fields shared by all built-in bots"""

    def __init__(self, world):
        from nav import NavGrid
        g = getattr(world, "_navgrid", None)
        if g is None:
            g = NavGrid(world)
            world._navgrid = g
        self.g = g
        self.fields = {}

    def waypoint(self, centre, radius, x, y):
        key = (round(centre[0], 1), round(centre[1], 1), round(radius, 1))
        f = self.fields.get(key)
        if f is None:
            f = self.g.field_to_circle(centre[0], centre[1], radius)
            self.fields[key] = f
        return self.g.next_waypoint(f, x, y)


class SquadDirector:
    """Practice house squad: shared vision, never fight each other, hunt the entrant(s)
    weakest-first; one engages early, more join as the squad thins, all engage late."""

    ORDER = {"easy": 0, "medium": 1, "hard": 2, "expert": 3}

    def __init__(self, house_ids, tiers, entrant_ids, params):
        self.house = list(house_ids)
        self.tiers = tiers
        self.entrants = set(entrant_ids)
        self.p = params
        self.last_seen = {}
        self.rng = random.Random(params.get("seed", 0))
        # engagement order: weakest tier first, random within tier
        self.order = sorted(self.house, key=lambda i: (self.ORDER.get(tiers[i], 9), self.rng.random()))

    def intel(self, M, vis):
        """vis: Senses.visible_set result for house bots. Returns per-bot intel dicts and the shared actor ids."""
        t = M.t
        seen = set()
        for hid in self.house:
            if hid in vis:
                seen |= {i for i in vis[hid]["actors"] if i in self.entrants}
        for e in seen:
            m = M.mechs[e]
            self.last_seen[e] = (t, list(m.pos))
        alive_house = [i for i in self.order if M.mechs[i].alive]
        dead = len(self.house) - len(alive_house)
        n_eng = self.p["early"] + int(dead * self.p["join_per_loss"])
        if t >= self.p["all_engage_t"]:
            n_eng = len(alive_house)
        engaged = set(alive_house[:n_eng])
        # weakest entrant first (lowest hp among known)
        known = [e for e in self.entrants if M.mechs[e].alive and e in self.last_seen and t - self.last_seen[e][0] < self.p["forget_s"]]
        hunt = None
        if known:
            e = min(known, key=lambda e: M.mechs[e].hp)
            hunt = self.last_seen[e][1]
        out = {}
        for hid in self.house:
            me = M.mechs[hid]
            eng = hid in engaged
            allowed = set()
            if eng:
                allowed = set(self.entrants)
            else:
                # self defence: entrant close or recently hurt us
                for e in self.entrants:
                    em = M.mechs[e]
                    if em.alive and (math.dist(em.pos, me.pos) < self.p["defend_range"] or t - getattr(me, "_hurt_by", {}).get(e, -99) < self.p["retaliate_s"]):
                        allowed.add(e)
            out[hid] = {"engaged": eng, "allowed_targets": allowed, "hunt_pos": hunt if eng else None}
        return out, seen
