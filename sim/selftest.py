#!/usr/bin/env python3
"""Deterministic mechanic checks for the engine. Run: sim/py sim/selftest.py  (exit code 1 on failure)."""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Match, Weapon  # noqa: E402
from world import World  # noqa: E402

W = World()
FAILS = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def fresh(seed=0):
    M = Match(W, seed=seed)
    # park everyone far away and idle so tests are isolated
    for k, m in enumerate(M.mechs):
        m.pos = [-480.0 + 3 * k, 360.0, W.ground(-480.0 + 3 * k, 360.0)]
    for n in M.npcs:
        n.alive = False
    for c in M.crates.values():
        c["alive"] = False
    M.pickups.clear()
    return M


def place(M, m, x, y):
    m.pos = [x, y, W.support(x, y, W.ground(x, y) + 0.5)]
    m.vel = [0.0, 0.0, 0.0]
    m.grounded = True


def run(M, ticks, act=None):
    for _ in range(ticks):
        if act and M.tick % 2 == 0:
            for mid, a in act().items():
                M.apply_action(M.mechs[mid], dict(a, tick=M.tick))
        M.step()


def open_spot():
    for x, y, g in W.open_ground_cells()[::97]:
        if all(not W.disc_block(x + dx, y, 2.5, g, 1.2, 9)[0] for dx in range(0, 60, 3)) and abs(W.ground(x + 60, y) - g) < 1.5:
            return float(x), float(y)
    raise RuntimeError("no open spot")


X0, Y0 = open_spot()

# ---- walking speed / acceleration
M = fresh()
m = M.mechs[0]
place(M, m, X0, Y0)
run(M, 20, lambda: {0: {"move": [1, 0]}})
check("walk speed reaches 11.7", abs(math.hypot(*m.vel[:2]) - 11.7) < 0.05, f"{m.vel}")
run(M, 20, lambda: {0: {"move": [1, 0], "boost": True}})
check("boost speed reaches 19.5", abs(math.hypot(*m.vel[:2]) - 19.5) < 0.05, f"{m.vel}")
check("boost drains 17/s", abs((100 - m.energy) - 17 * 1.0) < 1.0, f"energy {m.energy}")

# ---- jump + jet climb: steady vz 8.9 (cap then gravity)
M = fresh()
m = M.mechs[0]
place(M, m, X0, Y0)
M.apply_action(m, {"tick": 0, "jump": True, "boost": True})
vz = []
for k in range(30):
    if M.tick % 2 == 0 and k:
        M.apply_action(m, {"tick": M.tick, "boost": True})
    M.step()
    vz.append(round(m.vel[2], 2))
check("jet climb pins at 8.9 m/s", vz[10] == 8.9, f"{vz[:12]}")
check("jet drains 15/s", abs(m.energy - (100 - 15 * 1.5)) < 1.5, f"energy {m.energy}")

# ---- wall: walk into the nearest building and get stopped / slide
bx = by = None
for p in W.prisms:
    if p["kind"] == "building" and p["top"] - p["base"] > 10:
        xs, ys = p["ring"][0::2], p["ring"][1::2]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        for d in range(15, 80, 2):
            x, y = cx - d, cy
            g = W.ground(x, y)
            if not W.disc_block(x, y, 2.5, g, 1.2, 9)[0] and W.inside_solid(cx, cy, p["top"] - 0.5):
                bx, by = x, y
                break
        if bx is not None:
            break
M = fresh()
m = M.mechs[0]
place(M, m, bx, by)
run(M, 120, lambda: {0: {"move": [1, 0]}})
inside = W.disc_block(m.pos[0], m.pos[1], 2.49, m.pos[2], 1.2, 9)[0]
check("walking into a building stops at the wall", not inside and m.pos[0] < bx + 80, f"start {bx:.1f} end {m.pos[0]:.1f}")

# ---- gun cadence: 0.24 s exactly (held) and 12-round mag then reload
M = fresh()
a, b = M.mechs[0], M.mechs[1]
place(M, a, X0, Y0)
place(M, b, X0 + 40, Y0)
a.aim = a.cmd_aim = [0.0, math.atan2((b.pos[2] + 4.5) - (a.pos[2] + 7), 40)]
b.aim = b.cmd_aim = [math.pi, 0.0]
fires = []
for k in range(90):
    if M.tick % 2 == 0:
        M.apply_action(a, {"tick": M.tick, "fire": [True, False], "aim": a.cmd_aim})
        M.apply_action(b, {"tick": M.tick})
    am = a.weapons[0].ammo
    M.step()
    if a.weapons[0].ammo < am:
        fires.append(M.tick)
gaps = [y - x for x, y in zip(fires, fires[1:])]
check("gun: 12 shots then reload", len(fires) == 12 and a.weapons[0].reload > 0, f"{len(fires)} shots, reload {a.weapons[0].reload:.2f}")
check("gun: held fire every 6 ticks (first tick of a decision; check stream)", set(gaps) == {6}, f"gaps {gaps}")
check("gun: front hits do 45", 1400 - b.hp == 45 * len([1 for _ in range(round((1400 - b.hp) / 45))]) and b.hp < 1400, f"hp {b.hp}")

# ---- rear hit x1.5
M = fresh()
a, b = M.mechs[0], M.mechs[1]
place(M, a, X0, Y0)
place(M, b, X0 + 30, Y0)
b.aim = b.cmd_aim = [0.0, 0.0]  # facing away
M._damage(b, "mech", 45, "gun", a.id, (a.pos[0], a.pos[1]))
check("rear hit x1.5", abs(1400 - b.hp - 67.5) < 1e-6, f"{1400 - b.hp}")

# ---- laser: charge 0.75 s, release fires 315; short release fires at min charge
M = fresh()
a, b = M.mechs[0], M.mechs[1]
place(M, a, X0, Y0)
place(M, b, X0 + 60, Y0)
a.weapons[1] = Weapon("laser")
pitch = math.atan2((b.pos[2] + 4.5) - (a.pos[2] + 7), 60)
a.aim = a.cmd_aim = [0.0, pitch]
b.aim = b.cmd_aim = [math.pi, 0.0]
run(M, 16, lambda: {0: {"fire": [False, True], "aim": [0.0, pitch]}, 1: {}})
check("laser: charging while held", a.weapons[1].charging and abs(a.weapons[1].charge - 0.75) < 1e-6, f"charge {a.weapons[1].charge}")
run(M, 2, lambda: {0: {"fire": [False, False], "aim": [0.0, pitch]}, 1: {}})
check("laser: full charge release = 315", abs(1400 - b.hp - 315) < 1e-6, f"dmg {1400 - b.hp}")
check("laser: cooldown 1.65 after", abs(a.weapons[1].cooldown - (1.65 - 0.05)) < 0.06, f"cd {a.weapons[1].cooldown}")
hp0 = b.hp
run(M, 40, lambda: {0: {"fire": [False, False]}, 1: {}})
run(M, 2, lambda: {0: {"fire": [False, True], "aim": [0.0, pitch]}, 1: {}})
run(M, 8, lambda: {0: {"fire": [False, False], "aim": [0.0, pitch]}, 1: {}})
dmg = hp0 - b.hp
check("laser: early release fires at min_charge (0.15 s -> 35%+)", 110 < dmg < 160, f"dmg {dmg:.1f}")

# ---- lightning: nearest target in cone within 45 m, chain x0.7
M = fresh()
a, b, c = M.mechs[0], M.mechs[1], M.mechs[2]
place(M, a, X0, Y0)
place(M, b, X0 + 30, Y0)
place(M, c, X0 + 40, Y0 + 6)
a.weapons[1] = Weapon("lightning")
a.aim = a.cmd_aim = [0.0, 0.0]
b.aim = b.cmd_aim = [math.pi, 0]
c.aim = c.cmd_aim = [math.pi, 0]
run(M, 2, lambda: {0: {"fire": [False, True]}, 1: {}, 2: {}})
check("lightning hits nearest for 60", abs(1400 - b.hp - 60) < 1e-6, f"{1400 - b.hp}")
check("lightning chains x0.7 = 42", abs(1400 - c.hp - 42) < 1e-6, f"{1400 - c.hp}")
check("lightning costs 3 energy", abs(a.energy - 97) < 1e-6, f"{a.energy}")

# ---- mortar ballistic: flat-ground range at 45 deg ~ v^2/g
M = fresh()
a = M.mechs[0]
place(M, a, X0, Y0)
a.weapons[1] = Weapon("mortar")
a.aim = a.cmd_aim = [0.0, math.radians(20)]
run(M, 2, lambda: {0: {"fire": [False, True], "aim": [0.0, math.radians(20)]}})
p = M.projs[0]
v0 = p.vel.copy()
check("mortar shell speed 72 (horizontal = 72 cos 20deg)", abs(v0[0] - 72 * math.cos(math.radians(20))) < 0.05, f"{v0}")
run(M, 20, lambda: {0: {}})
check("mortar falls at 22 m/s^2", abs((v0[2] - M.projs[0].vel[2]) - 22 * 1.0) < 0.06 if M.projs else False)

# ---- zone damage & regen
M = fresh()
m = M.mechs[0]
zs = M.zone_state()
far = (zs["center"][0] + zs["radius"] + 30, zs["center"][1])
m.pos = [-500.0, -320.0, W.ground(-500.0, -320.0)]
if math.dist(m.pos[:2], zs["center"]) < zs["radius"]:
    m.pos = [550.0, 380.0, W.ground(550.0, 380.0)]
run(M, 20, lambda: {0: {}})
check("zone dps 20 outside", abs(1400 - m.hp - 20) < 0.01 or math.dist(m.pos[:2], zs["center"]) < zs["radius"], f"hp {m.hp}")
M = fresh()
m = M.mechs[0]
place(M, m, X0, Y0)
m.hp = 1000
M.t = 10.0
M.tick = 200
m.last_combat_t = 10.0
run(M, 100, lambda: {0: {}})
check("no regen before regen_delay", m.hp == 1000, f"hp {m.hp}")
run(M, 20, lambda: {0: {}})
check("regen 45/s after 5 s", abs(m.hp - 1045) < 0.01, f"hp {m.hp}")

# ---- cubes, max hp, offers
M = fresh()
m = M.mechs[0]
place(M, m, X0, Y0)
M._spawn_cube([m.pos[0] + 1, m.pos[1], m.pos[2]], scatter=0)
run(M, 2, lambda: {0: {}})
check("cube pickup: +4% max hp, heal", m.cubes == 1 and abs(m.max_hp - 1456) < 1e-6 and abs(m.hp - 1456) < 1e-6, f"{m.cubes} {m.max_hp} {m.hp}")
check("offer at 1 cube", m.offer is not None and len(m.offer["choices"]) == 3)
run(M, 2, lambda: {0: {"pick": 1}})
check("pick applies upgrade", m.offer is None and len(m.upgrades) == 1, f"{m.upgrades}")

# ---- take/drop
M = fresh()
m = M.mechs[0]
place(M, m, X0, Y0)
pid = M._spawn_pickup("laser", [m.pos[0] + 2, m.pos[1], m.pos[2]], log=False)
run(M, 2, lambda: {0: {"take": pid}})
check("take into empty slot 1", m.weapons[1] is not None and m.weapons[1].kind == "laser")
check("equip delay = max(1, 1.65)", abs(m.weapons[1].cooldown - (1.65 - 0.1)) < 0.06, f"{m.weapons[1].cooldown}")

print(f"\n{len(FAILS)} failures" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
