"""Run one match: controllers (external processes over the real protocol, or built-in bots),
lockstep decisions, timing, trace output."""
from __future__ import annotations

import gzip
import json
import math
import os
import select
import shlex
import subprocess
import sys
import tempfile
import time

from builtin import TIERS, BuiltinBot, SquadDirector
from config import load_config, sim_config
from engine import Match, r2
from sense import Senses, build_obs

HOUSE_TIERS = ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3

SQUAD_DEFAULTS = {"early": 1, "join_per_loss": 1.0, "all_engage_t": 300.0, "forget_s": 8.0, "defend_range": 0.0, "retaliate_s": 0.0}


# ---------------------------------------------------------------------------- controllers
def _jsnum(v):
    """serialise like the (JavaScript) server: integral floats as ints (560 not 560.0); numpy scalars as numbers"""
    if isinstance(v, float):
        return int(v) if v.is_integer() and abs(v) < 1e15 else v
    if isinstance(v, dict):
        return {k: _jsnum(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsnum(x) for x in v]
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        return _jsnum(v.item())
    return v


class ProcBot:
    """External bot process speaking mechfight/1 on stdin/stdout."""

    external = True

    def __init__(self, cmd, label, log_path=None, cwd=None, env=None):
        self.cmd = cmd
        self.label = label
        self.log_path = log_path
        self.log = open(log_path, "w+b") if log_path else tempfile.TemporaryFile()
        e = dict(os.environ)
        e.update(env or {})
        self.stats_path = e.get("MECH_STATS")
        self.p = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, cwd=cwd, env=e,
                                  bufsize=0, start_new_session=True)
        self.fd = self.p.stdout.fileno()
        os.set_blocking(self.fd, False)
        self.buf = b""
        self.dead = False
        self.lat = []
        self.bad_lines = 0
        self.stale = 0
        self.name = label

    def send(self, msg):
        if self.dead:
            return
        try:
            self.p.stdin.write((json.dumps(_jsnum(msg), separators=(",", ":")) + "\n").encode())
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            self.dead = True

    def _lines(self):
        out = []
        try:
            while True:
                chunk = os.read(self.fd, 1 << 16)
                if not chunk:
                    self.dead = True
                    break
                self.buf += chunk
        except BlockingIOError:
            pass
        except OSError:
            self.dead = True
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            out.append(line)
        return out

    def handshake(self, hello, budget_s):
        self.send(hello)
        t0 = time.monotonic()
        while time.monotonic() - t0 < budget_s and not self.dead:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            for line in self._lines():
                try:
                    m = json.loads(line)
                except Exception:
                    self.bad_lines += 1
                    continue
                if isinstance(m, dict) and m.get("type") == "ready":
                    self.name = str(m.get("name", self.label))[:32]
                    self.ready_ms = (time.monotonic() - t0) * 1000
                    return True
        return False

    def close(self, end_msg=None):
        if end_msg is not None:
            self.send(end_msg)
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=3)
        except Exception:
            try:
                os.killpg(self.p.pid, 9)
            except Exception:
                pass
        # scan stderr for tracebacks, keep the tail of the last one
        self.tracebacks = 0
        self.last_tb = None
        try:
            self.log.flush()
            self.log.seek(0, 2)
            size = self.log.tell()
            self.log.seek(max(0, size - (4 << 20)))
            txt = self.log.read().decode("utf-8", "replace")
            self.tracebacks = txt.count("Traceback (most recent call last)")
            if self.tracebacks:
                k = txt.rfind("Traceback (most recent call last)")
                self.last_tb = txt[k:k + 2000].strip().splitlines()[-6:]
        except Exception:
            pass
        self.log.close()
        self.bot_stats = None
        if self.stats_path and os.path.exists(self.stats_path):
            try:
                with open(self.stats_path) as f:
                    raw = f.read()
                try:
                    self.bot_stats = json.loads(raw)
                except Exception:
                    self.bot_stats = raw[-4000:]
                os.unlink(self.stats_path)
            except Exception:
                pass


def collect(bots_ticks, budget_s, enforce, max_line):
    """bots_ticks: list of (bot, tick, t_sent). Waits for every bot's answer to its tick.
    enforce=True: stop at budget. Returns {bot: (action|None, latency_ms|None)}"""
    pending = {b: (tk, ts) for b, tk, ts in bots_ticks if not b.dead}
    res = {b: (None, None) for b, _, _ in bots_ticks}
    hard_cap = budget_s if enforce else 10.0
    t0 = min((ts for _, _, ts in bots_ticks), default=time.monotonic())
    while pending:
        now = time.monotonic()
        left = t0 + hard_cap - now
        if left <= 0:
            break
        fds = {b.fd: b for b in pending}
        r, _, _ = select.select(list(fds), [], [], left)
        for fd in r:
            b = fds[fd]
            for line in b._lines():
                if len(line) > max_line:
                    b.bad_lines += 1
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    b.bad_lines += 1
                    continue
                if not isinstance(m, dict) or b not in pending:
                    continue
                tk, ts = pending[b]
                if m.get("tick") != tk:
                    b.stale += 1
                    continue
                lat = (time.monotonic() - ts) * 1000
                b.lat.append(lat)
                res[b] = (m, lat)
                del pending[b]
            if b.dead and b in pending:
                del pending[b]
    return res


# ---------------------------------------------------------------------------- match
def parse_lineup(mode, bot_cmds, seed, ffa_spec=None):
    """returns list of 16 entries: ("proc", cmd, label) or ("builtin", tier) plus tracked slot set"""
    n = 16
    if mode == "squad":
        slot = seed % n if True else 0
        lineup = []
        tiers = list(HOUSE_TIERS)
        k = 0
        for i in range(n):
            if i == slot:
                lineup.append(("proc", bot_cmds[0], "entrant") if not bot_cmds[0].startswith("builtin:") else ("builtin", bot_cmds[0].split(":", 1)[1]))
            else:
                lineup.append(("house", tiers[k]))
                k += 1
        return lineup, {slot}
    # ffa: spec like "bot*4,builtin:hard*6,builtin:expert*6"; "bot" = bot_cmds[0], "bot2" = bot_cmds[1]
    items = []
    for part in (ffa_spec or "bot*1,builtin:expert*5,builtin:hard*5,builtin:brawler*3,builtin:sniper*2").split(","):
        part = part.strip()
        cnt = 1
        if "*" in part:
            part, c = part.rsplit("*", 1)
            cnt = int(c)
        for _ in range(cnt):
            items.append(part)
    if len(items) != n:
        raise ValueError(f"ffa lineup has {len(items)} entries, need {n}")
    import random as _r
    rr = _r.Random(seed * 31 + 7)
    rr.shuffle(items)
    lineup, tracked = [], set()
    for i, it in enumerate(items):
        if it.startswith("builtin:"):
            lineup.append(("builtin", it.split(":", 1)[1]))
        elif it in ("bot", "bot1"):
            lineup.append(("proc", bot_cmds[0], "bot"))
            tracked.add(i)
        elif it == "bot2":
            lineup.append(("proc", bot_cmds[1], "bot2"))
            tracked.add(i)
        elif it.startswith("cmd:"):
            lineup.append(("proc", it[4:], "cmd"))
        else:
            raise ValueError(f"bad lineup item {it}")
    return lineup, tracked


def run_match(world, seed, mode="squad", bot_cmds=("python3 -u bot/main.py",), ffa_spec=None, enforce=False, budget_ms=None,
              trace_path=None, log_dir=None, validate=False, cfg=None, sim=None, squad=None, stop_when_tracked_dead=True,
              cwd=None, verbose=False, trace_every=2):
    cfg = cfg or load_config()
    sim = sim or sim_config()
    squad_p = dict(SQUAD_DEFAULTS)
    squad_p.update(squad or {})
    squad_p["seed"] = seed
    lineup, tracked = parse_lineup(mode, list(bot_cmds), seed, ffa_spec)
    names = []
    for e in lineup:
        names.append(e[2] if e[0] == "proc" else f"house:{e[1]}" if e[0] == "house" else f"builtin:{e[1]}")
    trace = [] if trace_path else None
    M = Match(world, seed=seed, cfg=cfg, sim=sim, names=names, trace=trace)
    M.projs = []
    M._pending_takes = []
    M._senses = Senses(M)
    validator = None
    if validate:
        from schema import Validator
        validator = Validator()
    budget_s = (budget_ms if budget_ms is not None else cfg["bot"]["tick_budget_ms"]) / 1000.0
    ctrls = {}
    navctx = None
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    house_ids, house_tiers = [], {}
    wall0 = time.monotonic()
    for i, e in enumerate(lineup):
        if e[0] == "proc":
            lp = os.path.join(log_dir, f"seed{seed}_slot{i}.stderr.log") if log_dir else None
            stats_dir = os.environ.get("MECH_SIM_TMP") or tempfile.gettempdir()
            env = {"MECH_STATS": os.path.join(stats_dir, f"mechstats_{os.getpid()}_{seed}_{i}.json"), "MECH_SIM": "1"}
            ctrls[i] = ProcBot(e[1], e[2], log_path=lp, cwd=cwd, env=env)
        else:
            tier = e[1]
            if tier not in TIERS:
                raise ValueError(f"unknown built-in tier {tier}; known {sorted(TIERS)}")
            if navctx is None and sim.get("builtin_nav", True):
                from builtin import NavContext
                navctx = NavContext(world)
            b = BuiltinBot(tier, world, cfg, seed, i, nav=navctx)
            b.external = False
            ctrls[i] = b
            if e[0] == "house":
                house_ids.append(i)
                house_tiers[i] = tier
    director = SquadDirector(house_ids, house_tiers, tracked, squad_p) if mode == "squad" else None
    # handshake
    for i, c in ctrls.items():
        if getattr(c, "external", False):
            hello = {"type": "hello", "protocol": "mechfight/1", "slot": i, "seed": (seed * 1000003 + i * 7919) % (2 ** 31),
                     "map": {"dir": world.arena_dir, "files": ["v2/world.json", "v2/terrain.i16"]}, "config": cfg}
            ok = c.handshake(hello, cfg["bot"]["hello_budget_ms"] / 1000.0)
            if not ok:
                c.dead = True
                if verbose:
                    print(f"[seed {seed}] slot {i} failed to send ready", file=sys.stderr)
    if trace is not None:
        trace.append(_trace_header(M, lineup))
    max_line = cfg["bot"]["max_line_bytes"]
    debug_draw = {}
    while not M.over:
        if M.tick % M.decide_every == 0:
            alive = [m for m in M.mechs if m.alive]
            ext = [m for m in alive if getattr(ctrls[m.id], "external", False)]
            ints = [m for m in alive if not getattr(ctrls[m.id], "external", False)]
            focus = None
            if director is not None:
                ent = {i for i in tracked if M.mechs[i].alive}
                focus = {h: ent for h in house_ids}
            vis = M._senses.visible_set(alive, want_items={m.id for m in ext}, focus=focus)
            intel, shared = ({}, set())
            if director is not None:
                intel, shared = director.intel(M, vis)
                M._trace_eng = sorted(h for h, v in intel.items() if v.get("engaged"))
                M._trace_known = sorted(shared)
            # external: send all, then collect
            sent = []
            for m in ext:
                c = ctrls[m.id]
                obs = build_obs(M, m, vis[m.id], full=True)
                if validator is not None:
                    errs = validator.errors(obs, "ServerMessage")
                    if errs:
                        print(f"[seed {seed}] obs schema errors: {errs[:3]}", file=sys.stderr)
                if c.dead:
                    sent.append((c, None, None))
                    continue
                c.send(obs)
                sent.append((c, M.tick, time.monotonic()))
            answers = collect([(c, tk, ts) for c, tk, ts in sent if tk is not None], budget_s, enforce, max_line) if sent else {}
            for m in ext:
                c = ctrls[m.id]
                act, lat = answers.get(c, (None, None))
                if act is not None and validator is not None:
                    errs = validator.errors(act, "BotMessage")
                    if errs:
                        print(f"[seed {seed}] action schema errors slot {m.id}: {errs[:3]}", file=sys.stderr)
                if lat is not None and lat > budget_s * 1000:
                    m.late += 1
                if act is not None and isinstance(act.get("debug"), dict) and trace is not None:
                    debug_draw[m.id] = act["debug"]
                M.apply_action(m, act)
            for m in ints:
                c = ctrls[m.id]
                obs = build_obs(M, m, vis[m.id], full=False, shared_actor_ids=(shared if m.id in house_ids else None))
                act = c.act(obs, intel.get(m.id))
                M.apply_action(m, act)
            if trace is not None and M.tick % trace_every == 0:
                trace.append(_trace_frame(M, debug_draw))
                debug_draw = {}
        M.step()
        # send `end` to dead external bots right away
        for m in M.mechs:
            c = ctrls[m.id]
            if not m.alive and getattr(c, "external", False) and not m.end_sent and m.place:
                if sim["send_end_on_death"]:
                    m.end_sent = True
                    c.close({"type": "end", "place": m.place, "kills": m.kills})
        if stop_when_tracked_dead and tracked and all(not M.mechs[i].alive for i in tracked):
            # entrant outcomes are final; remaining mechs keep placeholder places
            break
    if not M.over:
        M._finish_match_early = True
    for m in M.mechs:
        c = ctrls[m.id]
        if getattr(c, "external", False) and not m.end_sent:
            m.end_sent = True
            c.close({"type": "end", "place": m.place or 1, "kills": m.kills})
    if trace is not None:
        trace.append(_trace_frame(M, {}))
        trace.append(_trace_result(M, lineup))
        os.makedirs(os.path.dirname(os.path.abspath(trace_path)), exist_ok=True)
        with gzip.open(trace_path, "wt") if trace_path.endswith(".gz") else open(trace_path, "w") as f:
            for rec in trace:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    wall = time.monotonic() - wall0
    out = {"seed": seed, "mode": mode, "sim_t": round(M.t, 2), "wall_s": round(wall, 2), "mechs": []}
    for m in M.mechs:
        c = ctrls[m.id]
        lat = sorted(getattr(c, "lat", []))
        rec = {"id": m.id, "name": names[m.id], "tracked": m.id in tracked, "place": m.place, "kills": m.kills, "alive": m.alive,
               "death_t": None if m.death_t is None else round(m.death_t, 2), "killed_by": m.killed_by, "killed_with": m.killed_with,
               "hp": round(m.hp), "cubes": m.cubes, "loadout": [w.kind for w in m.weapons if w], "dmg_dealt": round(m.damage_dealt),
               "dmg_taken": round(m.damage_taken), "zone_dmg": round(m.zone_damage), "t_outside": round(m.time_outside, 1),
               "missed": m.missed, "late": m.late}
        if getattr(c, "external", False):
            rec["stderr_tracebacks"] = getattr(c, "tracebacks", 0)
            if getattr(c, "last_tb", None):
                rec["last_traceback"] = c.last_tb
            if getattr(c, "bot_stats", None) is not None:
                rec["bot_stats"] = c.bot_stats
            rec["ready_ms"] = round(getattr(c, "ready_ms", -1), 1)
        if lat:
            rec["lat_p50"] = round(lat[len(lat) // 2], 2)
            rec["lat_p99"] = round(lat[min(len(lat) - 1, int(len(lat) * 0.99))], 2)
            rec["lat_max"] = round(lat[-1], 2)
            rec["bad_lines"] = c.bad_lines
            rec["stale"] = c.stale
        out["mechs"].append(rec)
    return out


# ---------------------------------------------------------------------------- trace
def _trace_header(M, lineup):
    return {"type": "header", "format": "mechreplay/1", "source": "sim", "tick_hz": M.cfg["match"]["tick_hz"], "frame_every": 2, "seed": M.seed,
            "fields": {"m": ["id", "x", "y", "z", "aim_yaw", "aim_pitch", "hp", "energy", "flags", "cubes", "max_hp", "slot0", "slot1"],
                       "n": ["id", "x", "y", "z", "yaw", "hp"], "p": ["id", "kind(0 gun,1 mortar,2 npc)", "x", "y", "z"], "z": ["cx", "cy", "r"],
                       "d": ["id", "state(0 inbound,1 landed,2 capturing,3 contested)", "holder", "progress 0..1"],
                       "flags": {"1": "grounded", "2": "boosting", "4": "laser charging"}},
            "weapon_kinds": ["gun", "mortar", "laser", "lightning"],
            "mechs": [{"id": m.id, "name": m.name, "spawn": [r2(v) for v in m.pos]} for m in M.mechs],
            "npcs": [{"id": n.id, "spawn": [r2(v) for v in n.pos]} for n in M.npcs],
            "crates": [{"id": c["id"], "pos": [r2(v) for v in c["pos"]]} for c in M.crates.values()],
            "pickups": [{"id": p["id"], "kind": p["kind"], "pos": [r2(v) for v in p["pos"]]} for p in M.pickups.values()],
            "zone": [{"center": [r2(p["center"][0]), r2(p["center"][1])], "radius": p["radius"], "shrink_start": p["shrink_start"], "shrink_end": p["shrink_end"]} for p in M.zone_phases],
            "drops": [{"id": d["id"], "call_t": d["call_t"], "land_t": d["land_t"]} for d in M.drops]}


WK = {"gun": 0, "mortar": 1, "laser": 2, "lightning": 3}
PK = {"gun": 0, "mortar": 1, "npc": 2}


def _trace_frame(M, debug_draw):
    ms = []
    for m in M.mechs:
        if not m.alive:
            continue
        fl = (1 if m.grounded else 0) | (2 if getattr(m, "_boosting", False) or (m.boost and not m.grounded and m.energy > 0) else 0) | (4 if M._charging(m) else 0)
        ms.append([m.id, r2(m.pos[0]), r2(m.pos[1]), r2(m.pos[2]), round(m.aim[0], 3), round(m.aim[1], 3), round(m.hp), round(m.energy, 1), fl, m.cubes,
                   round(m.max_hp), WK[m.weapons[0].kind] if m.weapons[0] else -1, WK[m.weapons[1].kind] if m.weapons[1] else -1])
    ns = [[n.id, r2(n.pos[0]), r2(n.pos[1]), r2(n.pos[2]), round(n.yaw, 2), round(n.hp)] for n in M.npcs if n.alive]
    ps = [[p.id, PK[p.kind], r2(p.pos[0]), r2(p.pos[1]), r2(p.pos[2])] for p in M.projs]
    z = M.zone_state()
    fr = {"t": M.tick, "m": ms, "n": ns, "p": ps, "z": [r2(z["center"][0]), r2(z["center"][1]), r2(z["radius"])]}
    ds = []
    for d in M.drops:
        if d["called"] and not d["taken"]:
            st = 0 if not d["landed"] else (2 if d["holder"] is not None else 1)
            ds.append([d["id"], st, d["holder"], round(d["progress"] / M.cfg["drops"]["capture_s"], 2)])
    if ds:
        fr["d"] = ds
    if getattr(M, "_trace_eng", None) is not None:
        fr["eng"] = M._trace_eng      # squad mode: house bots currently engaged (hunting)
        fr["seen"] = M._trace_known   # entrants seen by the house squad this decision
    if M.trace_events:
        fr["ev"] = M.trace_events
        M.trace_events = []
    if debug_draw:
        fr["dbg"] = {str(k): v for k, v in debug_draw.items()}
    # crates/cubes/pickups state for the viewer (compact, every 10th frame)
    if M.tick % 20 == 0:
        fr["items"] = {"c": [[c["id"], r2(c["pos"][0]), r2(c["pos"][1])] for c in M.crates.values() if c["alive"]],
                       "q": [[c["id"], r2(c["pos"][0]), r2(c["pos"][1])] for c in M.cubes.values()],
                       "w": [[p["id"], WK[p["kind"]], r2(p["pos"][0]), r2(p["pos"][1])] for p in M.pickups.values()]}
    return fr


def _trace_result(M, lineup):
    return {"type": "result", "ticks": M.tick, "mechs": [{"id": m.id, "name": m.name, "place": m.place, "kills": m.kills,
                                                         "death_t": m.death_t, "killed_by": m.killed_by, "killed_with": m.killed_with,
                                                         "damage_dealt": round(m.damage_dealt), "damage_taken": round(m.damage_taken),
                                                         "cubes": m.cubes, "upgrades": m.upgrades,
                                                         "loadout": [w.kind for w in m.weapons if w]} for m in M.mechs]}
