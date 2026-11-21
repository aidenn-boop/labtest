"""Headless mechfight/1 match engine.

One `Match` simulates 16 mechs at config.match.tick_hz (20 Hz) with a decision every
config.match.decide_every (2) ticks. Controllers (external processes or built-in bots) are
asked for actions by `run_match` in sim/runner.py; this module is pure game logic.
"""
from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass, field

import numpy as np

from config import load_config, sim_config

TAU = 2 * math.pi
WEAPON_KINDS = ["gun", "mortar", "laser", "lightning"]


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def r2(v):
    return round(float(v), 2)


@dataclass
class Weapon:
    kind: str
    cooldown: float = 0.0
    ammo: int = 0
    reload: float = 0.0
    charge: float = 0.0
    charging: bool = False
    hold: float = 0.0
    release_pending: bool = False


@dataclass
class Mech:
    id: int
    name: str
    pos: list
    vel: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    aim: list = field(default_factory=lambda: [0.0, 0.0])
    cmd_aim: list = field(default_factory=lambda: [0.0, 0.0])
    hp: float = 1400.0
    max_hp: float = 1400.0
    energy: float = 100.0
    grounded: bool = True
    cubes: int = 0
    upgrades: list = field(default_factory=list)
    weapons: list = field(default_factory=lambda: [None, None])
    alive: bool = True
    place: int = 0
    kills: int = 0
    death_t: float | None = None
    killed_by: int | None = None
    killed_with: str | None = None
    # controls
    move: tuple = (0.0, 0.0)
    boost: bool = False
    fire: tuple = (False, False)
    last_valid_t: float = 0.0
    # timers
    last_spend_t: float = -99.0
    last_combat_t: float = -99.0
    shorted_until: float = -1.0
    step_timer: float = 0.0
    edge_dir: tuple | None = None
    in_zone: bool = True
    # sensing
    seen_t: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    last_obs_t: float = -1.0
    offer: dict | None = None
    offer_queue: int = 0
    offer_seq: int = 0
    stats: dict = field(default_factory=dict)
    wstats: dict = field(default_factory=dict)
    # bookkeeping
    damage_dealt: float = 0.0
    damage_taken: float = 0.0
    zone_damage: float = 0.0
    time_outside: float = 0.0
    missed: int = 0
    late: int = 0
    end_sent: bool = False


@dataclass
class NPC:
    id: int
    home: list
    pos: list
    vel: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    yaw: float = 0.0
    hp: float = 350.0
    alive: bool = True
    target: int | None = None
    returning: bool = False
    cooldown: float = 0.0
    goal: list | None = None
    goal_t: float = 0.0
    los_t: float = 0.0
    los_ok: bool = False


@dataclass
class Proj:
    id: int
    kind: str  # gun | mortar | npc
    owner: int
    pos: np.ndarray
    vel: np.ndarray
    travelled: float = 0.0
    rng: float = 1e9
    ttl: float = 1e9
    damage: float = 0.0
    splash: float = 0.0
    radius: float = 0.0
    born: float = 0.0
    wstats: dict | None = None
    dmg_mult: float = 1.0


class Match:
    def __init__(self, world, seed=0, cfg=None, sim=None, names=None, trace=None):
        self.world = world
        self.cfg = cfg or load_config()
        self.sim = sim or sim_config()
        self.seed = seed
        self.rng = random.Random(seed)
        self.nrng = np.random.default_rng(seed)
        c = self.cfg
        self.dt = 1.0 / c["match"]["tick_hz"]
        self.decide_every = c["match"]["decide_every"]
        self.duration = c["match"]["duration_s"]
        self.tick = 0
        self.t = 0.0
        self.next_id = {"proj": 1, "cube": 5000, "pickup": 8000}
        self.sounds = []   # (t, kind, (x,y,z), src)
        self.beams = []    # (t, by, kind, path)
        self.trace = trace
        self.trace_events = []
        self.deaths_this_tick = []
        self.over = False
        self.upgrade_defs = {u["id"]: u for u in c["upgrades"]}
        self._setup_zone()
        n = c["match"]["mechs"]
        names = names or [f"mech{i}" for i in range(n)]
        self.mechs = []
        for i, p in enumerate(self._spawns(n)):
            m = Mech(id=i, name=names[i], pos=list(p))
            yaw = math.atan2(self.zone_phases[0]["center"][1] - p[1], self.zone_phases[0]["center"][0] - p[0])
            m.aim = [yaw, 0.0]
            m.cmd_aim = [yaw, 0.0]
            self._recompute_stats(m)
            m.hp = m.max_hp = self._max_hp(m)
            m.energy = m.stats["energy"]
            g = Weapon("gun", ammo=int(m.wstats["gun"]["mag"]))
            m.weapons[0] = g
            self.mechs.append(m)
        self.alive_count = n
        self._setup_npcs()
        self._setup_crates_loot()
        self._setup_drops()
        self.cubes = {}
        self.projs = []
        self._pending_takes = []
        self._zone_now = None
        self.result_extra = {}

    # ------------------------------------------------------------------ setup
    def _setup_zone(self):
        z = self.cfg["zone"]
        phases = []
        cx, cy = z["start_center"]
        R = z["start_radius"]
        t = 0.0
        phases.append({"center": [cx, cy], "radius": R, "shrink_start": 0.0, "shrink_end": 0.0, "dps": z["phases"][0]["dps"]})
        for ph in z["phases"]:
            r = ph["radius"]
            maxoff = R - r
            if self.sim["zone_center_uniform_area"]:
                d = maxoff * math.sqrt(self.rng.random())
            else:
                d = maxoff * self.rng.random()
            a = self.rng.random() * TAU
            nx, ny = cx + d * math.cos(a), cy + d * math.sin(a)
            s0 = t + ph["hold_s"]
            s1 = s0 + ph["shrink_s"]
            phases.append({"center": [nx, ny], "radius": r, "shrink_start": s0, "shrink_end": s1, "dps": ph["dps"], "hold_start": t})
            t = s1
            cx, cy, R = nx, ny, r
        self.zone_phases = phases

    def zone_state(self, t=None):
        """returns dict(center, radius, next_center, next_radius, shrink_start, shrink_end, dps)"""
        t = self.t if t is None else t
        P = self.zone_phases
        for k in range(1, len(P)):
            ph = P[k]
            if t < ph["shrink_end"] or k == len(P) - 1:
                prev = P[k - 1]
                if t <= ph["shrink_start"]:
                    c, r = prev["center"], prev["radius"]
                elif t >= ph["shrink_end"]:
                    c, r = ph["center"], ph["radius"]
                else:
                    f = (t - ph["shrink_start"]) / (ph["shrink_end"] - ph["shrink_start"])
                    c = [prev["center"][0] + f * (ph["center"][0] - prev["center"][0]), prev["center"][1] + f * (ph["center"][1] - prev["center"][1])]
                    r = prev["radius"] + f * (ph["radius"] - prev["radius"])
                return {"center": c, "radius": r, "next_center": ph["center"], "next_radius": ph["radius"],
                        "shrink_start": ph["shrink_start"], "shrink_end": ph["shrink_end"], "dps": ph["dps"]}

    def _rand_open(self, n, margin=30.0, within=None, minsep=0.0, avoid=(), tries=4000):
        cells = self.world.open_ground_cells()
        out = []
        w = self.world
        for _ in range(tries):
            if len(out) >= n:
                break
            x, y, g = cells[self.rng.randrange(len(cells))]
            x += self.rng.uniform(-1, 1)
            y += self.rng.uniform(-1, 1)
            if x < w.minx + margin or x > w.maxx - margin or y < w.miny + margin or y > w.maxy - margin:
                continue
            if within is not None:
                (cx, cy), R = within
                if (x - cx) ** 2 + (y - cy) ** 2 > R * R:
                    continue
            if any((x - p[0]) ** 2 + (y - p[1]) ** 2 < minsep * minsep for p in list(out) + list(avoid)):
                continue
            g = w.support(x, y, w.ground(x, y) + 0.5)
            if not w.is_open_disc(x, y, 2.6, g):
                continue
            out.append([float(x), float(y), float(g)])
        return out

    def _spawns(self, n):
        mc = self.cfg["match"]
        z = self.cfg["zone"]
        sep = mc["spawn_min_separation"]
        while True:
            pts = self._rand_open(n, margin=mc["arena_margin"], within=(z["start_center"], z["start_radius"] * mc["spawn_zone_fraction"]), minsep=sep)
            if len(pts) >= n:
                return pts
            sep *= 0.9

    def _setup_npcs(self):
        c = self.cfg["npc"]
        pts = self._rand_open(c["count"], margin=20, minsep=40)
        self.npcs = [NPC(id=100 + i, home=list(p), pos=list(p), hp=c["hp"], yaw=self.rng.uniform(-math.pi, math.pi)) for i, p in enumerate(pts)]

    def _roof_points(self, n):
        cells = self.world.roof_cells()
        out = []
        for _ in range(n * 20):
            if len(out) >= n:
                break
            x, y, top = cells[self.rng.randrange(len(cells))]
            # keep away from roof edges: the disc must be all at roof height
            if not self.world.is_open_disc(x, y, 1.5, top):
                continue
            jj, ii, _, _ = self.world.disc_cells(x, y, 2.0)
            if (self.world.TOPMAX[jj, ii] != int(round(top * 100))).any():
                continue
            out.append([float(x), float(y), float(top)])
        return out

    def _setup_crates_loot(self):
        cc = self.cfg["crates"]
        n_roof = int(round(cc["count"] * cc["roof_fraction"]))
        pts = self._roof_points(n_roof) + self._rand_open(cc["count"] - n_roof, margin=15, minsep=15)
        self.crates = {1000 + i: {"id": 1000 + i, "pos": p, "hp": float(cc["hp"]), "alive": True} for i, p in enumerate(pts)}
        lc = self.cfg["loot"]
        n_roof = int(round(lc["floor_count"] * lc["floor_roof_fraction"]))
        pts = self._roof_points(n_roof) + self._rand_open(lc["floor_count"] - n_roof, margin=15, minsep=10)
        self.pickups = {}
        for p in pts:
            self._spawn_pickup(self._rand_weapon_kind(), p, log=False)

    def _rand_weapon_kind(self):
        w = self.cfg["loot"]["weights"]
        kinds = [k for k in WEAPON_KINDS if w.get(k, 0) > 0]
        return self.rng.choices(kinds, weights=[w[k] for k in kinds])[0]

    def _spawn_pickup(self, kind, pos, ammo=None, log=True, src=None):
        pid = self.next_id["pickup"]
        self.next_id["pickup"] += 1
        self.pickups[pid] = {"id": pid, "kind": kind, "pos": [float(pos[0]), float(pos[1]), float(pos[2])], "ammo": ammo, "w": None}
        if log:
            self._tev({"e": "weapon", "op": "spawn", "id": pid, "kind": kind, "at": [r2(v) for v in pos], "src": src})
        return pid

    def _setup_drops(self):
        d = self.cfg["drops"]
        self.drops = []
        for k, tc in enumerate(d["times"]):
            self.drops.append({"id": 20000 + k, "call_t": tc, "land_t": tc + d["fall_s"], "pos": None, "holder": None,
                               "progress": 0.0, "taken": False, "called": False, "landed": False})

    # ------------------------------------------------------------------ stats
    def _recompute_stats(self, m):
        st = copy.deepcopy(self.cfg["mech"])
        ws = copy.deepcopy(self.cfg["weapons"])
        for uid in m.upgrades:
            for e in self.upgrade_defs[uid]["effects"]:
                parts = e["path"].split(".")
                d = st if parts[0] == "mech" else ws[parts[1]]
                key = parts[-1]
                if "mul" in e:
                    d[key] = d[key] * e["mul"]
                if "add" in e:
                    d[key] = d[key] + e["add"]
        s = 1 + self.cfg["cubes"]["size_per_cube"] * m.cubes
        st["radius"] = self.cfg["mech"]["radius"] * s
        st["height"] = self.cfg["mech"]["height"] * s
        st["eye_height"] = self.cfg["mech"]["eye_height"] * s
        st["muzzle_height"] = self.cfg["mech"]["muzzle_height"] * s
        m.stats = st
        m.wstats = ws

    def _max_hp(self, m):
        cc = self.cfg["cubes"]
        return m.stats["hp"] * (1 + cc["hp_per_cube"] * min(m.cubes, cc["stat_cap"]))

    def _dmg_mult(self, m):
        cc = self.cfg["cubes"]
        return 1 + cc["damage_per_cube"] * min(m.cubes, cc["stat_cap"])

    def _refresh_max(self, m):
        old_max, old_emax = m.max_hp, None
        new_max = self._max_hp(m)
        if new_max > old_max:
            m.hp += new_max - old_max
        m.max_hp = new_max
        m.hp = min(m.hp, m.max_hp)

    # ------------------------------------------------------------------ events/trace
    def _tev(self, e):
        if self.trace is not None:
            e = dict(e)
            e["t"] = self.tick
            self.trace_events.append(e)

    def _sound(self, kind, pos, src):
        self.sounds.append((self.t, kind, (float(pos[0]), float(pos[1]), float(pos[2])), src))

    # ------------------------------------------------------------------ actions
    def apply_action(self, m: Mech, a: dict | None):
        """a = validated action dict, or None if no valid answer arrived this decision."""
        if not m.alive:
            return
        coast = self.cfg["bot"]["coast_s"]
        if a is None:
            m.missed += 1
            m.fire = (False, False)
            if self.t - m.last_valid_t > coast - 1e-9:
                m.move = (0.0, 0.0)
                m.boost = False
            return
        m.last_valid_t = self.t
        mv = a.get("move") or [0.0, 0.0]
        try:
            mx, my = float(mv[0]), float(mv[1])
        except Exception:
            mx, my = 0.0, 0.0
        L = math.hypot(mx, my)
        if L > 1:
            mx, my = mx / L, my / L
        if not (math.isfinite(mx) and math.isfinite(my)):
            mx, my = 0.0, 0.0
        m.move = (mx, my)
        m.boost = bool(a.get("boost", False))
        aim = a.get("aim")
        if isinstance(aim, (list, tuple)) and len(aim) == 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in aim):
            m.cmd_aim = [float(aim[0]), min(max(float(aim[1]), m.stats["min_pitch"]), m.stats["max_pitch"])]
        f = a.get("fire") or [False, False]
        m.fire = (bool(f[0]) if len(f) > 0 else False, bool(f[1]) if len(f) > 1 else False)
        if a.get("jump") and m.grounded:
            m.vel[2] = m.stats["jump_speed"]
            m.grounded = False
            m.edge_dir = None
        if a.get("reload"):
            g = self._slot_of(m, "gun")
            if g is not None:
                w = m.weapons[g]
                if w.reload <= 0 and w.ammo < int(m.wstats["gun"]["mag"]):
                    w.reload = m.wstats["gun"]["reload"]
                    self._sound("reload", m.pos, m.id)
        pk = a.get("pick")
        if pk is not None and m.offer is not None and isinstance(pk, int) and 0 <= pk < len(m.offer["choices"]):
            self._resolve_offer(m, pk)
        take = a.get("take")
        drop = a.get("drop")
        if drop is not None and drop not in (0, 1):
            drop = None
        if take is not None:
            self._pending_takes.append((m, take, drop))
        elif drop is not None:
            self._do_drop(m, drop)

    def _slot_of(self, m, kind):
        for i, w in enumerate(m.weapons):
            if w is not None and w.kind == kind:
                return i
        return None

    def _do_drop(self, m, slot, log_event=True):
        w = m.weapons[slot]
        if w is None:
            return None
        if w.kind == "laser" and w.charging:
            return None
        pid = self._spawn_pickup(w.kind, [m.pos[0], m.pos[1], m.pos[2]], ammo=w.ammo if w.kind == "gun" else None)
        self.pickups[pid]["w"] = w
        m.weapons[slot] = None
        if log_event:
            m.events.append({"type": "dropped", "id": pid, "kind": w.kind, "slot": slot})
        return pid

    def _resolve_takes(self):
        if not self._pending_takes:
            return
        # closest wins per pickup
        claims = {}
        for m, pid, drop in self._pending_takes:
            p = self.pickups.get(pid)
            if p is None:
                m.events.append({"type": "take_failed", "id": pid, "reason": "gone"})
                continue
            d = math.hypot(p["pos"][0] - m.pos[0], p["pos"][1] - m.pos[1])
            if d > self.cfg["loot"]["pickup_radius"] or abs(p["pos"][2] - m.pos[2]) > 4.0:
                m.events.append({"type": "take_failed", "id": pid, "reason": "range"})
                continue
            if self._slot_of(m, p["kind"]) is not None:
                m.events.append({"type": "take_failed", "id": pid, "reason": "have"})
                continue
            if drop is not None:
                slot = drop
                w = m.weapons[slot]
                if w is not None and w.kind == "laser" and w.charging:
                    m.events.append({"type": "take_failed", "id": pid, "reason": "busy"})
                    continue
            else:
                slot = 0 if m.weapons[0] is None else (1 if m.weapons[1] is None else None)
                if slot is None:
                    m.events.append({"type": "take_failed", "id": pid, "reason": "full"})
                    continue
            claims.setdefault(pid, []).append((d, m.id, m, slot))
        for pid, cl in claims.items():
            cl.sort(key=lambda c: (c[0], c[1]))
            for k, (d, _, m, slot) in enumerate(cl):
                if k > 0:
                    m.events.append({"type": "take_failed", "id": pid, "reason": "gone"})
                    continue
                p = self.pickups.pop(pid)
                if m.weapons[slot] is not None:
                    self._do_drop(m, slot)
                w = p["w"] or Weapon(p["kind"])
                if p["w"] is None and p["kind"] == "gun":
                    w.ammo = int(p["ammo"] if p["ammo"] is not None else m.wstats["gun"]["mag"])
                w.charging = False
                w.charge = 0.0
                w.hold = 0.0
                w.release_pending = False
                w.cooldown = max(w.cooldown, self.cfg["loot"]["equip_s"], m.wstats[p["kind"]]["cooldown"])
                m.weapons[slot] = w
                m.events.append({"type": "took", "id": pid, "kind": p["kind"], "slot": slot})
                self._tev({"e": "weapon", "op": "take", "id": pid, "kind": p["kind"], "by": m.id, "slot": slot})
        self._pending_takes = []

    # ------------------------------------------------------------------ offers
    def _queue_offers(self, m, old, new):
        for th in self.cfg["cubes"]["upgrade_thresholds"]:
            if old < th <= new:
                m.offer_queue += 1
        # beyond the listed thresholds: keep the last gap pattern (rare)
        self._next_offer(m)

    def _next_offer(self, m):
        if m.offer is not None or m.offer_queue <= 0:
            return
        held = {w.kind for w in m.weapons if w is not None}
        counts = {}
        for u in m.upgrades:
            counts[u] = counts.get(u, 0) + 1
        elig = [u for u in self.cfg["upgrades"] if counts.get(u["id"], 0) < u.get("max", 1) and (u.get("requires") is None or u["requires"] in held)]
        m.offer_queue -= 1
        if not elig:
            return self._next_offer(m)
        ch = self.rng.sample(elig, min(3, len(elig)))
        m.offer_seq += 1
        m.offer = {"id": m.offer_seq, "expires": round(self.t + self.cfg["bot"]["offer_timeout_s"], 2),
                   "choices": [{"id": u["id"], "name": u["name"], "desc": u["desc"]} for u in ch]}

    def _resolve_offer(self, m, idx):
        uid = m.offer["choices"][idx]["id"]
        m.offer = None
        old_emax = m.stats["energy"]
        old_hpbase = m.stats["hp"]
        m.upgrades.append(uid)
        self._recompute_stats(m)
        if m.stats["energy"] > old_emax:
            m.energy += m.stats["energy"] - old_emax
        self._refresh_max(m)
        m.events.append({"type": "upgrade", "id": uid})
        self._tev({"e": "upgrade", "id": m.id, "up": uid})
        self._next_offer(m)

    def _add_cubes(self, m, n):
        old = m.cubes
        m.cubes += n
        self._recompute_stats(m)
        self._refresh_max(m)
        self._queue_offers(m, old, m.cubes)

    # ------------------------------------------------------------------ main step
    def step(self):
        """advance one sim tick (actions must have been applied for decision ticks)"""
        dt = self.dt
        self._pending_takes = getattr(self, "_pending_takes", [])
        self._resolve_takes()
        self.deaths_this_tick = []
        zs = self.zone_state()
        self._zone_now = zs
        for m in self.mechs:
            if m.alive:
                self._slew(m, dt)
                self._move(m, dt)
        self._separate()
        for m in self.mechs:
            if m.alive:
                self._weapons(m, dt)
        self._step_projectiles(dt)
        self._step_npcs(dt)
        self._zone_and_regen(zs, dt)
        self._pickups_and_drops(dt)
        for m in self.mechs:
            if m.alive and m.offer is not None and self.t + 1e-9 >= m.offer["expires"]:
                self._resolve_offer(m, 0)
        self._finish_deaths()
        self.tick += 1
        self.t = self.tick * dt
        # prune
        if self.tick % 20 == 0:
            self.sounds = [s for s in self.sounds if self.t - s[0] < 2.0]
            self.beams = [b for b in self.beams if self.t - b[0] < 1.0]
        if self.alive_count <= 1 or self.t >= self.duration - 1e-9:
            self._finish_match()

    # ------------------------------------------------------------------ movement
    def _slew(self, m, dt):
        st = m.stats
        dy = wrap(m.cmd_aim[0] - m.aim[0])
        mx = st["turn_rate"] * dt
        m.aim[0] = wrap(m.aim[0] + max(-mx, min(mx, dy)))
        dp = m.cmd_aim[1] - m.aim[1]
        mp = st["pitch_rate"] * dt
        m.aim[1] = min(max(m.aim[1] + max(-mp, min(mp, dp)), st["min_pitch"]), st["max_pitch"])

    def _col_r(self, m):
        """wall-collision radius. Replay: 14,390 grounded positions are all legal with the base radius and 0.9% are
        not with the cube-scaled one, so by default collision does not grow with cubes (hit cylinders do)."""
        r = m.stats["radius"] if self.sim["collision_radius_scales"] else self.cfg["mech"]["radius"]
        return r - 0.01

    def _charging(self, m):
        return any(w is not None and w.kind == "laser" and w.charging for w in m.weapons)

    def _move(self, m, dt):
        st = m.stats
        W = self.world
        x, y, z = m.pos
        vx, vy, vz = m.vel
        mx, my = m.move
        mlen = math.hypot(mx, my)
        r, h, step = self._col_r(m), st["height"], st["step_up"]
        spent = False
        boosting = False
        if m.grounded:
            sp = st["walk_speed"]
            if m.boost and mlen > 0.05 and m.energy > 0:
                sp = st["boost_speed"]
                m.energy = max(0.0, m.energy - st["boost_drain"] * dt)
                spent = boosting = True
            if self._charging(m):
                sp *= st["charge_move_mult"]
            tx, ty = mx * sp, my * sp
            acc = st["ground_accel"]
        else:
            sp = st["walk_speed"]
            if self._charging(m):
                sp *= st["charge_move_mult"]
            if self.sim["air_keeps_boost_speed"]:
                sp = max(sp, min(math.hypot(vx, vy), st["boost_speed"]))
            tx, ty = mx * sp, my * sp
            acc = st["air_accel"]
        dvx, dvy = tx - vx, ty - vy
        dl = math.hypot(dvx, dvy)
        amax = acc * dt
        if dl > amax:
            dvx, dvy = dvx * amax / dl, dvy * amax / dl
        vx += dvx
        vy += dvy
        # vertical
        if not m.grounded:
            # order calibrated on the recorded check stream: jet + cap first, then gravity (vz pins at 10 - 22*0.05 = 8.9)
            if m.boost and m.energy > 0:
                if vz < st["max_rise_speed"]:
                    vz = min(vz + st["jet_accel"] * dt, st["max_rise_speed"])
                m.energy = max(0.0, m.energy - st["jet_drain"] * dt)
                spent = True
                if self.tick % 6 == 0:
                    self._sound("jet", m.pos, m.id)
            vz -= st["gravity"] * dt
        if spent:
            m.last_spend_t = self.t
        elif self.t - m.last_spend_t >= st["energy_regen_delay"] - 1e-9:
            m.energy = min(st["energy"], m.energy + st["energy_regen"] * dt)
        # horizontal collide & slide
        dx, dy = vx * dt, vy * dt
        if dx or dy:
            nx, ny, ok, nrm = self._try_h(m, x, y, z, dx, dy)
            if not ok:
                moved = False
                if nrm is not None:
                    dn = dx * nrm[0] + dy * nrm[1]
                    if dn < 0:
                        tx_, ty_ = dx - dn * nrm[0], dy - dn * nrm[1]
                        nx, ny, ok2, _ = self._try_h(m, x, y, z, tx_, ty_)
                        if ok2:
                            moved = True
                            vn = vx * nrm[0] + vy * nrm[1]
                            if vn < 0:
                                vx -= vn * nrm[0]
                                vy -= vn * nrm[1]
                if not moved:
                    for ax, ay in ((dx, 0.0), (0.0, dy)):
                        if ax == 0 and ay == 0:
                            continue
                        nx, ny, ok3, _ = self._try_h(m, x, y, z, ax, ay)
                        if ok3:
                            moved = True
                            if ax == 0:
                                vx = 0.0
                            else:
                                vy = 0.0
                            break
                if not moved:
                    nx, ny = x, y
                    vx = vy = 0.0
            x, y = nx, ny
        # vertical integrate
        if m.grounded:
            s = W.support(x, y, z + step)
            if s >= z - self.sim["ground_snap"]:
                if s < z - 0.02:
                    # could be an edge: check the disc for support at our height
                    pass
                z = s
                vz = 0.0
                m.edge_dir = None
            else:
                # walked off: are we still overhanging a ledge at our height?
                top, bxy = W.disc_landing(x, y, r * 0.999, z - 0.05, z + 0.05)
                if top is not None:
                    self._edge(m, x, y, bxy, mx, my, dt)
                    x, y = m._ex, m._ey
                    if m.edge_dir is None:
                        m.grounded = False
                else:
                    m.grounded = False
                    vz = 0.0
        if not m.grounded:
            nz = z + vz * dt
            if vz > 0:
                c = W.disc_ceiling(x, y, r * 0.98, z + h - 0.01, nz + h)
                if c is not None:
                    nz = c - h
                    vz = 0.0
            if vz <= 0:
                s = W.support(x, y, z + 0.05)
                if nz <= s:
                    nz = s
                    vz = 0.0
                    m.grounded = True
                else:
                    top, bxy = W.disc_landing(x, y, r * 0.999, nz, z + 0.05)
                    if top is not None and top > s:
                        nz = top
                        vz = 0.0
                        m.grounded = True
                        self._edge(m, x, y, bxy, mx, my, dt)
                        x, y = m._ex, m._ey
            z = nz
        # arena edge
        x = min(max(x, W.minx + r), W.maxx - r)
        y = min(max(y, W.miny + r), W.maxy - r)
        # steps sound
        hs = math.hypot(vx, vy)
        if m.grounded and hs > self.cfg["sound"]["step_speed"]:
            m.step_timer += dt
            if m.step_timer >= self.cfg["sound"]["step_every"]:
                m.step_timer = 0.0
                self._sound("boost" if boosting else "step", (x, y, z), m.id)
        m.pos = [x, y, z]
        m.vel = [vx, vy, vz]
        m._boosting = boosting

    def _edge(self, m, x, y, bxy, mx, my, dt):
        """standing on a roof edge with the centre off it: step on if moving toward it, else slide off"""
        m._ex, m._ey = x, y
        if bxy is None:
            m.edge_dir = None
            return
        L = math.hypot(*bxy)
        if L < 1e-6:
            m.edge_dir = None
            return
        ux, uy = bxy[0] / L, bxy[1] / L
        if mx * ux + my * uy > 0.2:
            m.edge_dir = (ux, uy)  # moving onto it: keep standing
            return
        # slide away (up to 0.6 m per tick)
        nx, ny = x - ux * 0.6, y - uy * 0.6
        b, _ = self.world.disc_block(nx, ny, self._col_r(m), m.pos[2] - 50, 0, 49)  # only check walls well below
        m._ex, m._ey = nx, ny
        m.edge_dir = None

    def _try_h(self, m, x, y, z, dx, dy):
        st = m.stats
        W = self.world
        nx, ny = x + dx, y + dy
        r, h, step = self._col_r(m), st["height"], st["step_up"]
        b, nrm = W.disc_block(nx, ny, r, z, step, h)
        if b:
            return nx, ny, False, nrm
        if m.grounded:
            s = W.support(nx, ny, z + step)
            if s > z + 0.01:
                # terrain slope limit
                g0 = W.ground(x, y)
                g1 = W.ground(nx, ny)
                if abs(s - g1) < 1e-3 and g1 > g0:
                    d = math.hypot(dx, dy)
                    # slope over a look-ahead baseline (replay: 0 of 10,990 grounded steps exceed tan 50deg over 2 m,
                    # while per-tick rises on LiDAR spikes reach 1.7 m/m)
                    bl = self.sim["slope_baseline"]
                    if d > 1e-6:
                        ux, uy = dx / d, dy / d
                        rise = (W.ground(x + ux * bl, y + uy * bl) - g0) / bl
                        if rise > math.tan(math.radians(st["max_slope_deg"])):
                            return nx, ny, False, (-ux, -uy)
                # head room after stepping up
                b2, n2 = W.disc_block(nx, ny, r, s, step, h)
                if b2:
                    return nx, ny, False, n2
        return nx, ny, True, None

    def _separate(self):
        ms = [m for m in self.mechs if m.alive]
        for i in range(len(ms)):
            a = ms[i]
            for j in range(i + 1, len(ms)):
                b = ms[j]
                dx = b.pos[0] - a.pos[0]
                dy = b.pos[1] - a.pos[1]
                rr = a.stats["radius"] + b.stats["radius"]
                if abs(dx) >= rr or abs(dy) >= rr:
                    continue
                d = math.hypot(dx, dy)
                if d >= rr:
                    continue
                if a.pos[2] + a.stats["height"] < b.pos[2] or b.pos[2] + b.stats["height"] < a.pos[2]:
                    continue
                if d < 1e-6:
                    dx, dy, d = 1.0, 0.0, 1.0
                push = (rr - d) / 2
                ux, uy = dx / d, dy / d
                for mm, sgn in ((a, -1), (b, 1)):
                    nx = mm.pos[0] + sgn * ux * push
                    ny = mm.pos[1] + sgn * uy * push
                    blk, _ = self.world.disc_block(nx, ny, self._col_r(mm), mm.pos[2], mm.stats["step_up"], mm.stats["height"])
                    if not blk:
                        mm.pos[0], mm.pos[1] = nx, ny

    # ------------------------------------------------------------------ weapons
    def _shot_origin(self, m, height, yaw, pitch):
        """muzzle point + (radius + 0.5) m out along the aim. sim switch `shot_offset_horizontal` (default, fitted on
        the public replay's mortar shells): the offset is the horizontal projection only (no vertical component)."""
        cp = math.cos(pitch)
        d = (cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch))
        mz = (m.pos[0], m.pos[1], m.pos[2] + height)
        off = m.stats["radius"] + 0.5
        dz = 0.0 if self.sim["shot_offset_horizontal"] else d[2]
        end = (mz[0] + d[0] * off, mz[1] + d[1] * off, mz[2] + dz * off)
        t = self.world.first_hit([mz], [end], step=0.25)[0]
        if t <= 1.0:
            o = (mz[0] + d[0] * off * t, mz[1] + d[1] * off * t, mz[2] + dz * off * t)
            return o, d, True
        return end, d, False

    def _weapons(self, m, dt):
        # check stream: a held gun fires on the first tick after a decision only (gaps of exactly 6 ticks at a
        # 0.24 s cooldown, observed cooldown never 0.14); the replay shows the same for mortar, lightning, laser start
        first = (self.tick % self.decide_every == 0) or not self.sim["fire_first_tick_only"]
        carry = not self.sim["fire_first_tick_only"]
        for slot in (0, 1):
            w = m.weapons[slot]
            if w is None:
                continue
            ws = m.wstats[w.kind]
            trig = m.fire[slot]
            w.cooldown -= dt
            if w.kind == "gun":
                if w.reload > 0:
                    w.reload -= dt
                    if w.reload <= 1e-9:
                        w.reload = 0.0
                        w.ammo = int(ws["mag"])
                if trig and first and w.cooldown <= 1e-9 and w.reload <= 0 and w.ammo > 0:
                    self._fire_gun(m, ws)
                    w.ammo -= 1
                    w.cooldown = (w.cooldown if carry else 0.0) + ws["cooldown"]
                    if w.ammo <= 0:
                        w.reload = ws["reload"]
                        self._sound("reload", m.pos, m.id)
                if w.cooldown < 0:
                    w.cooldown = 0.0
            elif w.kind == "mortar":
                if trig and first and w.cooldown <= 1e-9:
                    self._fire_mortar(m, ws)
                    w.cooldown = ws["cooldown"]
                if w.cooldown < 0:
                    w.cooldown = 0.0
            elif w.kind == "lightning":
                if trig and first and w.cooldown <= 1e-9 and m.energy >= ws["energy"] - 1e-9:
                    m.energy -= ws["energy"]
                    m.last_spend_t = self.t
                    self._fire_lightning(m, ws)
                    w.cooldown = (w.cooldown if carry else 0.0) + ws["cooldown"]
                if w.cooldown < 0:
                    w.cooldown = 0.0
            elif w.kind == "laser":
                if w.cooldown < 0:
                    w.cooldown = 0.0
                shorted = self.t < m.shorted_until
                if w.charging:
                    if shorted:
                        w.charging = False
                        w.charge = 0.0
                        w.hold = 0.0
                        w.release_pending = False
                        continue
                    w.charge = min(w.charge + dt, ws["charge"])
                    if w.charge >= ws["charge"] - 1e-9:
                        w.hold += dt
                    if not trig and w.charge >= ws["min_charge"] - 1e-9:
                        self._fire_laser(m, w, ws)
                    elif not trig:
                        w.release_pending = True
                    elif w.release_pending and w.charge >= ws["min_charge"] - 1e-9:
                        self._fire_laser(m, w, ws)
                    elif w.hold >= ws["hold_s"] - 1e-9:
                        self._fire_laser(m, w, ws)
                elif trig and first and w.cooldown <= 1e-9 and not shorted:
                    w.charging = True
                    w.charge = 0.0
                    w.hold = 0.0
                    w.release_pending = False
                    self._sound("laser_charge", m.pos, m.id)
                    self._tev({"e": "charge", "id": m.id})

    def _fire_gun(self, m, ws):
        sig = math.radians(ws["spread_deg"]) * (1.0 if self.sim["spread_is_sigma"] else 0.5)
        yaw = m.aim[0] + self.rng.gauss(0, sig)
        pitch = m.aim[1] + self.rng.gauss(0, sig)
        o, d, inwall = self._shot_origin(m, m.stats["muzzle_height"], yaw, pitch)
        sp = ws["speed"]
        p = Proj(id=self._pid(), kind="gun", owner=m.id, pos=np.array(o), vel=np.array(d) * sp, rng=ws["range"],
                 damage=ws["damage"] * self._dmg_mult(m), radius=self.cfg["projectile"]["gun_radius"], born=self.t)
        self._sound("gun", m.pos, m.id)
        self._tev({"e": "fire", "id": m.id, "w": "gun"})
        if inwall:
            self._tev({"e": "impact", "p": p.id, "at": [r2(v) for v in o], "hit": None})
            return
        self.projs.append(p)

    def _fire_mortar(self, m, ws):
        o, d, inwall = self._shot_origin(m, m.stats["height"] + 0.5, m.aim[0], m.aim[1])
        p = Proj(id=self._pid(), kind="mortar", owner=m.id, pos=np.array(o), vel=np.array(d) * ws["speed"], ttl=ws["ttl"],
                 damage=ws["damage"] * self._dmg_mult(m), splash=ws["splash"] * self._dmg_mult(m), born=self.t, wstats=dict(ws))
        self._sound("mortar", m.pos, m.id)
        self._tev({"e": "fire", "id": m.id, "w": "mortar"})
        if inwall:
            self._explode(p, np.array(o), None)
            return
        self.projs.append(p)

    def _pid(self):
        i = self.next_id["proj"]
        self.next_id["proj"] += 1
        return i

    # targets: list of (kind, obj, cx, cy, zbase, radius, height)
    def _targets(self, exclude_mech=None):
        out = []
        for m in self.mechs:
            if m.alive and m.id != exclude_mech:
                out.append(("mech", m, m.pos[0], m.pos[1], m.pos[2], m.stats["radius"], m.stats["height"]))
        nc = self.cfg["npc"]
        for n in self.npcs:
            if n.alive:
                out.append(("npc", n, n.pos[0], n.pos[1], n.pos[2], nc["radius"], nc["height"]))
        cc = self.cfg["crates"]
        for c in self.crates.values():
            if c["alive"]:
                out.append(("crate", c, c["pos"][0], c["pos"][1], c["pos"][2], cc["radius"], cc["height"]))
        return out

    @staticmethod
    def _ray_cyl(o, d, L, cx, cy, zb, r, h):
        """entry distance along ray o + s*d (s in [0,L]) into a vertical cylinder, or None"""
        ox, oy = o[0] - cx, o[1] - cy
        a = d[0] * d[0] + d[1] * d[1]
        if a < 1e-12:
            if ox * ox + oy * oy <= r * r:
                # vertical ray
                if d[2] < 0 and o[2] >= zb + h:
                    s = (o[2] - zb - h) / -d[2]
                    return s if s <= L else None
                if d[2] > 0 and o[2] <= zb:
                    s = (zb - o[2]) / d[2]
                    return s if s <= L else None
                if zb <= o[2] <= zb + h:
                    return 0.0
            return None
        b = ox * d[0] + oy * d[1]
        c = ox * ox + oy * oy - r * r
        disc = b * b - a * c
        if disc < 0:
            return None
        sq = math.sqrt(disc)
        s0 = (-b - sq) / a
        s1 = (-b + sq) / a
        if s1 < 0 or s0 > L:
            return None
        s0 = max(s0, 0.0)
        s1 = min(s1, L)
        z0 = o[2] + d[2] * s0
        z1 = o[2] + d[2] * s1
        top = zb + h
        if zb <= z0 <= top:
            return s0
        if abs(d[2]) < 1e-12:
            return None
        # find where z enters [zb, top] within [s0, s1]
        if z0 > top and z1 <= top:
            s = s0 + (z0 - top) / (z0 - z1) * (s1 - s0)
            return s
        if z0 < zb and z1 >= zb:
            s = s0 + (zb - z0) / (z1 - z0) * (s1 - s0)
            return s
        return None

    def _step_projectiles(self, dt):
        if not self.projs:
            return
        W = self.world
        g = self.cfg["weapons"]["mortar"]["gravity"]
        alive = []
        # world collision for all in one batch
        starts, ends = [], []
        for p in self.projs:
            if p.kind == "mortar":
                np_ = p.pos + p.vel * dt + np.array([0, 0, -0.5 * g * dt * dt])
            else:
                np_ = p.pos + p.vel * dt
            p._next = np_
            starts.append(p.pos)
            ends.append(np_)
        th = W.first_hit(np.array(starts), np.array(ends), step=0.4)
        tg_all = self._targets()
        for k, p in enumerate(self.projs):
            seg = p._next - p.pos
            L = float(np.linalg.norm(seg))
            if L < 1e-9:
                alive.append(p)
                continue
            d = seg / L
            best_s = th[k] * L if th[k] <= 1.0 else None
            best_t = None
            for tg in tg_all:
                kind, obj = tg[0], tg[1]
                if kind == "mech" and obj.id == p.owner:
                    continue
                if p.kind == "npc" and kind != "mech":
                    continue
                cx, cy = tg[2], tg[3]
                # quick reject
                mxr = tg[5] + p.radius + L + 1
                if abs(cx - p.pos[0]) > mxr or abs(cy - p.pos[1]) > mxr:
                    continue
                s = self._ray_cyl(p.pos, d, L, cx, cy, tg[4] - p.radius, tg[5] + p.radius, tg[6] + 2 * p.radius)
                if s is not None and (best_s is None or s < best_s):
                    best_s, best_t = s, tg
            # range limit (straight shots)
            if p.kind in ("gun", "npc") and p.travelled + (best_s if best_s is not None else L) >= p.rng:
                rem = max(0.0, p.rng - p.travelled)
                if best_s is None or best_s > rem:
                    continue  # expires in flight
            if best_s is not None:
                at = p.pos + d * best_s
                if p.kind == "mortar":
                    self._explode(p, at, best_t)
                else:
                    if best_t is not None:
                        self._proj_hit(p, best_t, at)
                    self._tev({"e": "impact", "p": p.id, "at": [r2(v) for v in at], "hit": (best_t[1].id if best_t is not None and best_t[0] != "crate" else (best_t[1]["id"] if best_t else None))})
                continue
            p.travelled += L
            p.pos = p._next
            if p.kind == "mortar":
                p.vel = p.vel + np.array([0, 0, -g * dt])
                if self.t - p.born >= p.ttl:
                    self._explode(p, p.pos, None)
                    continue
            alive.append(p)
        self.projs = alive

    def _src_mech(self, pid):
        if 0 <= pid < len(self.mechs):
            return self.mechs[pid]
        return None

    def _proj_hit(self, p, tg, at):
        kind, obj = tg[0], tg[1]
        src = p.pos - p.vel  # direction it came from
        if p.kind == "npc":
            self._damage(obj, kind, p.damage, "npc", p.owner, (float(src[0]), float(src[1])))
        else:
            self._damage(obj, kind, p.damage, "gun", p.owner, (float(src[0]), float(src[1])))

    def _explode(self, p, at, direct):
        ws = p.wstats
        R = ws["splash_radius"]
        edge = ws["splash_edge"]
        at = np.asarray(at, dtype=float)
        self._sound("explosion", at, p.owner)
        self._tev({"e": "boom", "p": p.id, "at": [r2(v) for v in at], "r": R})
        src = (float(at[0]), float(at[1]))
        owner = self._src_mech(p.owner)
        if direct is not None:
            self._damage(direct[1], direct[0], p.damage, "mortar", p.owner, (float(p.pos[0]), float(p.pos[1])), effect=("concuss" if direct[0] == "mech" else None))
        # splash: all targets incl. the owner
        cand = []
        for tg in self._targets():
            kind, obj, cx, cy, zb, r, h = tg
            # distance from blast to nearest point of the cylinder
            dxy = max(0.0, math.hypot(cx - at[0], cy - at[1]) - r)
            dz = 0.0 if zb <= at[2] <= zb + h else min(abs(at[2] - zb), abs(at[2] - zb - h))
            dist = math.hypot(dxy, dz)
            if dist > R:
                continue
            cand.append((tg, dist))
        if not cand:
            return
        P0 = np.array([at + np.array([0, 0, 0.3])] * len(cand))
        P1 = np.array([[tg[2], tg[3], tg[4] + tg[6] / 2] for tg, _ in cand])
        vis = self.world.los(P0, P1, vision=False, step=0.4)
        for (tg, dist), ok in zip(cand, vis):
            if not ok:
                continue
            kind, obj = tg[0], tg[1]
            is_direct = direct is not None and obj is direct[1]
            f = 1.0 if is_direct else 1.0 - (1.0 - edge) * (dist / R)
            if not is_direct or self.sim["mortar_direct_plus_splash"]:
                dmg = p.splash * f
                if kind == "mech" and obj.id == p.owner:
                    dmg *= obj.stats["self_damage_mult"]
                self._damage(obj, kind, dmg, "mortar", p.owner, src, effect=("concuss" if kind == "mech" else None))
            if kind == "mech" and obj.alive:
                hx, hy = obj.pos[0] - at[0], obj.pos[1] - at[1]
                hl = math.hypot(hx, hy)
                if hl < 1e-6:
                    hx, hy, hl = 1.0, 0.0, 1.0
                kv = ws["knock"] * f
                obj.vel[0] += hx / hl * kv
                obj.vel[1] += hy / hl * kv
                obj.vel[2] = max(obj.vel[2], 0.0) + ws["knock_up"] * f
                obj.grounded = False
                obj.edge_dir = None
                drain = ws["drain"] * f
                obj.energy = max(0.0, obj.energy - drain)
                if obj.id != p.owner:
                    obj.events.append({"type": "effect", "effect": "concuss", "by": p.owner})
                self._tev({"e": "concuss", "id": obj.id, "by": p.owner, "v": [r2(hx / hl * kv), r2(hy / hl * kv), r2(ws["knock_up"] * f)], "drain": r2(drain)})

    def _fire_laser(self, m, w, ws):
        frac = min(w.charge / ws["charge"], 1.0) if ws["charge"] > 0 else 1.0
        dmg = ws["damage"] * (ws["min_frac"] + (1 - ws["min_frac"]) * frac) * self._dmg_mult(m)
        w.charging = False
        w.charge = 0.0
        w.hold = 0.0
        w.release_pending = False
        w.cooldown = ws["cooldown"]
        o, d, inwall = self._shot_origin(m, m.stats["muzzle_height"], m.aim[0], m.aim[1])
        L = ws["range"]
        if inwall:
            L = 0.0
        else:
            end = (o[0] + d[0] * L, o[1] + d[1] * L, o[2] + d[2] * L)
            t = self.world.first_hit([o], [end], step=0.5)[0]
            if t <= 1.0:
                L *= t
        hits = []
        for tg in self._targets(exclude_mech=m.id):
            s = self._ray_cyl(o, d, L, tg[2], tg[3], tg[4], tg[5], tg[6])
            if s is not None:
                hits.append((s, tg))
        hits.sort(key=lambda h: h[0])
        n = 1 + int(ws.get("pierce", 0))
        hit_ids = []
        for s, tg in hits[:n]:
            kind, obj = tg[0], tg[1]
            amt = dmg
            eff = None
            if kind == "mech" and any(x is not None and x.kind == "mortar" for x in obj.weapons):
                amt *= 1 + ws["cookoff"]
                eff = "cookoff"
                obj.events.append({"type": "effect", "effect": "cookoff", "by": m.id})
                self._tev({"e": "cookoff", "id": obj.id, "by": m.id, "at": [r2(obj.pos[0]), r2(obj.pos[1]), r2(obj.pos[2])]})
            self._damage(obj, kind, amt, "laser", m.id, (o[0], o[1]), effect=eff)
            hit_ids.append(obj.id if kind != "crate" else obj["id"])
        if hits[:n]:
            L = max(L, hits[min(n, len(hits)) - 1][0])
        end = [o[0] + d[0] * L, o[1] + d[1] * L, o[2] + d[2] * L]
        self.beams.append((self.t, m.id, "laser", [list(o), end]))
        self._sound("laser", m.pos, m.id)
        self._tev({"e": "laser", "id": m.id, "from": [r2(v) for v in o], "to": [r2(v) for v in end], "hits": hit_ids})

    def _fire_lightning(self, m, ws):
        o, d, inwall = self._shot_origin(m, m.stats["muzzle_height"], m.aim[0], m.aim[1])
        mz = (m.pos[0], m.pos[1], m.pos[2] + m.stats["muzzle_height"])
        rng_ = ws["range"]
        half = math.radians(ws["cone_deg"]) * (1.0 if self.sim["lightning_cone_half"] else 0.5)
        cands = []
        for tg in self._targets(exclude_mech=m.id):
            cx, cy, zb, h = tg[2], tg[3], tg[4], tg[6]
            dxy = math.hypot(cx - mz[0], cy - mz[1])
            dz = (zb + h / 2) - mz[2]
            dist = math.hypot(dxy, dz)
            if dist > rng_ + tg[5]:
                continue
            if abs(wrap(math.atan2(cy - mz[1], cx - mz[0]) - m.aim[0])) > half:
                continue
            cands.append((dist, tg))
        cands.sort(key=lambda c: c[0])
        target = None
        if cands and not inwall:
            P0 = np.array([mz] * len(cands))
            P1 = np.array([[tg[2], tg[3], tg[4] + tg[6] / 2] for _, tg in cands])
            vis = self.world.los(P0, P1, vision=False, step=0.5)
            for (dist, tg), ok in zip(cands, vis):
                if ok:
                    target = tg
                    break
        path = [list(o)]
        hits = []
        if target is None:
            L = 0.0 if inwall else rng_
            if L > 0:
                end = (o[0] + d[0] * L, o[1] + d[1] * L, o[2] + d[2] * L)
                t = self.world.first_hit([o], [end], step=0.5)[0]
                if t <= 1.0:
                    L *= t
            path.append([o[0] + d[0] * L, o[1] + d[1] * L, o[2] + d[2] * L])
        else:
            dmg = ws["damage"] * self._dmg_mult(m)
            cur = target
            prev_pt = (o[0], o[1])
            hit_objs = set()
            for k in range(int(ws["chain"]) + 1):
                kind, obj = cur[0], cur[1]
                hit_objs.add(id(obj))
                pt = [cur[2], cur[3], cur[4] + cur[6] / 2]
                path.append(pt)
                eff = None
                if kind == "mech" and any(x is not None and x.kind == "laser" for x in obj.weapons):
                    eff = "short"
                self._damage(obj, kind, dmg * (ws["chain_falloff"] ** k), "lightning", m.id, prev_pt, effect=eff)
                if eff == "short":
                    for x in obj.weapons:
                        if x is not None and x.kind == "laser":
                            x.charging = False
                            x.charge = 0.0
                            x.hold = 0.0
                            x.release_pending = False
                    obj.shorted_until = self.t + ws["short_s"]
                    obj.events.append({"type": "effect", "effect": "short", "by": m.id})
                    self._tev({"e": "short", "id": obj.id, "by": m.id, "dur": ws["short_s"]})
                hits.append(obj.id if kind != "crate" else obj["id"])
                if k >= int(ws["chain"]):
                    break
                # next: nearest un-hit target within chain_range with LOS
                nxt = []
                for tg in self._targets(exclude_mech=m.id):
                    if id(tg[1]) in hit_objs:
                        continue
                    dd = math.dist((tg[2], tg[3], tg[4] + tg[6] / 2), pt)
                    if dd <= ws["chain_range"]:
                        nxt.append((dd, tg))
                if not nxt:
                    break
                nxt.sort(key=lambda c: c[0])
                vis = self.world.los(np.array([pt] * len(nxt)), np.array([[tg[2], tg[3], tg[4] + tg[6] / 2] for _, tg in nxt]), vision=False, step=0.5)
                found = None
                for (dd, tg), ok in zip(nxt, vis):
                    if ok:
                        found = tg
                        break
                if found is None:
                    break
                prev_pt = (pt[0], pt[1])
                cur = found
        self.beams.append((self.t, m.id, "lightning", path))
        self._sound("lightning", m.pos, m.id)
        self._tev({"e": "zap", "id": m.id, "path": [[r2(v) for v in p] for p in path], "hits": hits})

    # ------------------------------------------------------------------ damage
    def _damage(self, obj, kind, amount, weapon, by, src_xy, effect=None):
        if kind == "mech":
            m = obj
            if not m.alive:
                return
            rear = False
            if weapon not in ("zone",) and src_xy is not None:
                if not (by == m.id):
                    ang = math.atan2(src_xy[1] - m.pos[1], src_xy[0] - m.pos[0])
                    if abs(wrap(ang - m.aim[0])) > math.radians(m.stats["front_arc_deg"]) / 2:
                        rear = True
                        amount *= m.stats["rear_mult"]
                amount *= max(0.0, 1 - m.stats["armor"])
            amount = float(amount)
            m.hp -= amount
            m.damage_taken += amount
            m.last_combat_t = self.t
            bearing = None
            if src_xy is not None:
                bearing = round(math.atan2(src_xy[1] - m.pos[1], src_xy[0] - m.pos[0]), 3)
            if by != m.id or weapon == "mortar":
                ev = {"type": "damaged", "amount": round(amount, 1), "weapon": weapon, "bearing": bearing, "by": by}
                if rear:
                    ev["rear"] = True
                m.events.append(ev)
            att = self._src_mech(by) if isinstance(by, int) and by < 100 else None
            if att is not None and att is not m:
                if not hasattr(m, "_hurt_by"):
                    m._hurt_by = {}
                m._hurt_by[att.id] = self.t
                att.last_combat_t = self.t
                att.damage_dealt += amount
                hv = {"type": "hit", "target": m.id, "amount": round(amount, 1), "weapon": weapon}
                if effect:
                    hv["effect"] = effect
                if rear:
                    hv["rear"] = True
                att.events.append(hv)
            if weapon != "zone":
                self._tev({"e": "hit", "src": by, "dst": m.id, "amt": round(amount, 1), "w": weapon, **({"rear": True} if rear else {})})
            if m.hp <= 0:
                m.hp = 0.0
                self._kill(m, by, weapon)
        elif kind == "npc":
            n = obj
            if not n.alive:
                return
            n.hp -= amount
            att = self._src_mech(by) if isinstance(by, int) and by < 100 else None
            if att is not None:
                att.last_combat_t = self.t
                att.events.append({"type": "hit", "target": n.id, "amount": round(amount, 1), "weapon": weapon})
                if n.target is None or not n.returning:
                    n.target = att.id
                    n.returning = False
            self._tev({"e": "hit", "src": by, "dst": n.id, "amt": round(amount, 1), "w": weapon})
            if n.hp <= 0:
                n.alive = False
                self._tev({"e": "death", "id": n.id, "by": by})
                self._sound("death", n.pos, n.id)
                for _ in range(self.cfg["npc"]["cubes"]):
                    self._spawn_cube(n.pos)
        elif kind == "crate":
            c = obj
            if not c["alive"]:
                return
            c["hp"] -= amount
            att = self._src_mech(by) if isinstance(by, int) and by < 100 else None
            if att is not None:
                att.events.append({"type": "hit", "target": c["id"], "amount": round(amount, 1), "weapon": weapon})
            if c["hp"] <= 0:
                c["alive"] = False
                self._sound("crate", c["pos"], -1)
                self._tev({"e": "crate", "id": c["id"], "by": by})
                for _ in range(self.cfg["crates"]["cubes"]):
                    self._spawn_cube(c["pos"])
                if self.rng.random() < self.cfg["loot"]["crate_weapon_chance"]:
                    self._spawn_pickup(self._rand_weapon_kind(), c["pos"], src=c["id"])

    def _spawn_cube(self, pos, scatter=None):
        s = self.sim["cube_scatter"] if scatter is None else scatter
        cid = self.next_id["cube"]
        self.next_id["cube"] += 1
        a = self.rng.random() * TAU
        r = s * math.sqrt(self.rng.random())
        x, y = pos[0] + r * math.cos(a), pos[1] + r * math.sin(a)
        z = pos[2]
        if self.world.inside_solid(x, y, z + 0.5):
            x, y = pos[0], pos[1]
        self.cubes[cid] = {"id": cid, "pos": [x, y, z]}
        self._tev({"e": "cube", "id": cid, "at": [r2(x), r2(y), r2(z)]})

    def _kill(self, m, by, weapon):
        m.alive = False
        m.death_t = self.t
        m.killed_by = by if (isinstance(by, int) and by < 100 and by != m.id) else None
        m.killed_with = weapon
        self.deaths_this_tick.append(m)
        self._sound("death", m.pos, m.id)
        self._tev({"e": "death", "id": m.id, "by": by})
        att = self._src_mech(by) if isinstance(by, int) and by < 100 else None
        if att is not None and att is not m and att.alive:
            att.kills += 1
            att.events.append({"type": "kill", "target": m.id})
        for slot, w in enumerate(m.weapons):
            if w is not None and w.kind != "gun":
                if w.kind == "laser":
                    w.charging = False
                pid = self._spawn_pickup(w.kind, m.pos, src=m.id)
                self.pickups[pid]["w"] = None
        n = m.cubes + self.cfg["cubes"]["death_drop_extra"]
        for _ in range(n):
            self._spawn_cube(m.pos)

    def _finish_deaths(self):
        if not self.deaths_this_tick:
            return
        place = self.alive_count
        for m in self.deaths_this_tick:
            m.place = place
        self.alive_count -= len(self.deaths_this_tick)
        self.deaths_this_tick = []

    def _finish_match(self):
        if self.over:
            return
        self.over = True
        surv = [m for m in self.mechs if m.alive]
        for m in surv:
            m.place = 1 + sum(1 for o in surv if o.hp > m.hp + 1e-9)
        self._tev({"e": "end", "tick": self.tick, "places": [m.place for m in self.mechs]})

    # ------------------------------------------------------------------ zone & regen
    def _zone_and_regen(self, zs, dt):
        cx, cy = zs["center"]
        R = zs["radius"]
        for m in self.mechs:
            if not m.alive:
                continue
            inside = (m.pos[0] - cx) ** 2 + (m.pos[1] - cy) ** 2 < R * R
            m.in_zone = inside
            if not inside:
                dmg = zs["dps"] * dt
                m.hp -= dmg
                m.zone_damage += dmg
                m.time_outside += dt
                m.last_combat_t = self.t
                m._zone_acc = getattr(m, "_zone_acc", 0.0) + dmg
                if m.hp <= 0:
                    m.hp = 0.0
                    self._kill(m, None, "zone")
            elif m.hp < m.max_hp and self.t - m.last_combat_t >= m.stats["regen_delay"] - 1e-9:
                m.hp = min(m.max_hp, m.hp + m.stats["regen_per_s"] * dt)

    # ------------------------------------------------------------------ cubes, drops
    def _pickups_and_drops(self, dt):
        pr = self.cfg["cubes"]["pickup_radius"]
        if self.cubes:
            live = [m for m in self.mechs if m.alive]
            for cid in list(self.cubes):
                c = self.cubes[cid]
                best = None
                for m in live:
                    d = math.hypot(m.pos[0] - c["pos"][0], m.pos[1] - c["pos"][1])
                    if d <= pr and abs(m.pos[2] - c["pos"][2]) <= 4.0:
                        if best is None or d < best[0] - 1e-9:
                            best = (d, m)
                if best is not None:
                    m = best[1]
                    del self.cubes[cid]
                    self._add_cubes(m, 1)
                    m.events.append({"type": "pickup", "cubes": m.cubes})
                    self._tev({"e": "pickup", "id": cid, "by": m.id})
        dc = self.cfg["drops"]
        for d in self.drops:
            if d["taken"]:
                continue
            if not d["called"] and self.t >= d["call_t"] - 1e-9:
                d["called"] = True
                zs = self.zone_state()
                R = zs["next_radius"] * dc["spread"]
                pts = self._rand_open(1, margin=10, within=(zs["next_center"], max(R, 10.0)))
                if not pts:
                    pts = self._rand_open(1, margin=10, within=(zs["next_center"], max(zs["next_radius"], 30.0)))
                if not pts:
                    pts = [[zs["next_center"][0], zs["next_center"][1], self.world.ground(*zs["next_center"])]]
                d["pos"] = pts[0]
                self._tev({"e": "drop", "op": "call", "id": d["id"], "at": [r2(v) for v in d["pos"]], "lands": int(round(d["land_t"] / dt)), "r": dc["radius"]})
            if d["called"] and not d["landed"] and self.t >= d["land_t"] - 1e-9:
                d["landed"] = True
                self._sound("drop", d["pos"], -1)
                self._tev({"e": "drop", "op": "land", "id": d["id"]})
            if d["landed"]:
                inside = [m for m in self.mechs if m.alive and math.hypot(m.pos[0] - d["pos"][0], m.pos[1] - d["pos"][1]) <= dc["radius"]
                          and abs(m.pos[2] - d["pos"][2]) <= self.sim["drop_vertical_reach"] + 9]
                if len(inside) == 0:
                    d["holder"] = None
                    d["progress"] = 0.0
                elif len(inside) == 1:
                    m = inside[0]
                    if d["holder"] != m.id:
                        d["holder"] = m.id
                        d["progress"] = 0.0
                    d["progress"] += dt
                    if d["progress"] >= dc["capture_s"] - 1e-9:
                        d["taken"] = True
                        self._add_cubes(m, dc["cubes"])
                        if dc.get("repair", 1):
                            m.hp = m.max_hp
                        m.energy = m.stats["energy"]
                        m.events.append({"type": "drop", "id": d["id"], "cubes": m.cubes})
                        self._tev({"e": "drop", "op": "take", "id": d["id"], "by": m.id})
                # contested: frozen

    # ------------------------------------------------------------------ NPCs
    def _step_npcs(self, dt):
        nc = self.cfg["npc"]
        W = self.world
        live = [m for m in self.mechs if m.alive]
        for n in self.npcs:
            if not n.alive:
                continue
            hd = math.hypot(n.pos[0] - n.home[0], n.pos[1] - n.home[1])
            tgt = self.mechs[n.target] if n.target is not None else None
            if tgt is not None and (not tgt.alive or math.hypot(tgt.pos[0] - n.pos[0], tgt.pos[1] - n.pos[1]) > nc["deaggro_range"] or hd > nc["chase_leash"]):
                n.target = None
                n.returning = True
                tgt = None
            if n.returning and hd < 3.0:
                n.returning = False
            # aggro scan (every 0.5 s)
            if tgt is None and not n.returning and hd <= nc["leash"] and self.t >= n.los_t:
                n.los_t = self.t + 0.5
                near = [m for m in live if math.dist(m.pos, n.pos) <= nc["aggro_range"]]
                if near:
                    near.sort(key=lambda m: math.dist(m.pos, n.pos))
                    eye = [n.pos[0], n.pos[1], n.pos[2] + nc["height"] * 0.8]
                    vis = W.los(np.array([eye] * len(near)), np.array([[m.pos[0], m.pos[1], m.pos[2] + m.stats["height"] / 2] for m in near]), vision=False)
                    for m, ok in zip(near, vis):
                        if ok:
                            n.target = m.id
                            tgt = m
                            break
            # movement goal
            if tgt is not None:
                gx, gy = tgt.pos[0], tgt.pos[1]
                dd = math.hypot(gx - n.pos[0], gy - n.pos[1])
                want = dd > 20.0
            elif n.returning:
                gx, gy = n.home[0], n.home[1]
                want = True
            else:
                if n.goal is None or self.t >= n.goal_t or math.hypot(n.goal[0] - n.pos[0], n.goal[1] - n.pos[1]) < 1.5:
                    a = self.rng.random() * TAU
                    rr = nc["leash"] * math.sqrt(self.rng.random())
                    n.goal = [n.home[0] + rr * math.cos(a), n.home[1] + rr * math.sin(a)]
                    n.goal_t = self.t + self.rng.uniform(3, 8)
                gx, gy = n.goal
                want = True
            vx = vy = 0.0
            if want:
                dx, dy = gx - n.pos[0], gy - n.pos[1]
                L = math.hypot(dx, dy)
                if L > 0.5:
                    vx, vy = dx / L * nc["speed"], dy / L * nc["speed"]
                    nx, ny = n.pos[0] + vx * dt, n.pos[1] + vy * dt
                    b, _ = W.disc_block(nx, ny, nc["radius"], n.pos[2], 1.2, nc["height"])
                    if b:
                        vx = vy = 0.0
                        n.goal = None
                    else:
                        s = W.support(nx, ny, n.pos[2] + 1.2)
                        n.pos = [nx, ny, s]
                        n.yaw = math.atan2(dy, dx)
            n.vel = [vx, vy, 0.0]
            # shooting
            n.cooldown -= dt
            if tgt is not None:
                tc = [tgt.pos[0], tgt.pos[1], tgt.pos[2] + tgt.stats["height"] / 2]
                n.yaw = math.atan2(tc[1] - n.pos[1], tc[0] - n.pos[0])
                if n.cooldown <= 0:
                    if self.t >= n.los_t:
                        n.los_t = self.t + 0.5
                        eye = [n.pos[0], n.pos[1], n.pos[2] + nc["height"] * 0.8]
                        n.los_ok = bool(W.los(np.array([eye]), np.array([tc]), vision=False)[0])
                    if n.los_ok:
                        n.cooldown = nc["cooldown"]
                        o = np.array([n.pos[0], n.pos[1], n.pos[2] + nc["height"] * 0.8])
                        lead = self.sim["npc_lead"]
                        tt = math.dist(o, tc) / nc["projectile_speed"]
                        aimp = np.array(tc) + lead * tt * np.array(tgt.vel)
                        d = aimp - o
                        yaw = math.atan2(d[1], d[0]) + self.rng.gauss(0, math.radians(nc["spread_deg"]))
                        pitch = math.atan2(d[2], math.hypot(d[0], d[1])) + self.rng.gauss(0, math.radians(nc["spread_deg"]))
                        dv = np.array([math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)])
                        o = o + dv * (nc["radius"] + 0.5)
                        self.projs.append(Proj(id=self._pid(), kind="npc", owner=n.id, pos=o, vel=dv * nc["projectile_speed"],
                                               rng=self.sim["npc_bullet_range"], damage=nc["damage"], radius=self.cfg["projectile"]["npc_radius"], born=self.t))
                        self._sound("npc_gun", n.pos, n.id)
