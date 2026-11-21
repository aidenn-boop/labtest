"""Game config (the recorded hello.config) plus simulator-only switches.

Every rule number comes from `hello_config.json` (copied verbatim from the kit's recorded hello).
`SIM` holds choices the rulebook leaves open; each is documented in ASSUMPTIONS.md.
"""
from __future__ import annotations

import copy
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path: str | None = None) -> dict:
    with open(path or os.path.join(HERE, "data", "hello_config.json")) as f:
        return json.load(f)


# Simulator switches for ambiguous rules (see ASSUMPTIONS.md). Override with --sim key=value.
SIM_DEFAULTS = {
    "slope_baseline": 2.0,            # terrain slope measured from here to 2 m ahead (replay-fitted; see CALIBRATION.md)
    "fire_first_tick_only": True,     # check stream: held triggers fire only on the first tick of a decision, no cooldown carry
    "spread_is_sigma": True,          # gun spread_deg is the Gaussian sigma (else: 2-sigma)
    "lightning_cone_half": True,      # cone_deg is the half-angle (|dyaw| <= cone_deg); else full width
    "mortar_direct_plus_splash": False,  # replay: a direct hit deals `damage` only (one hit event), full concussion
    "air_keeps_boost_speed": True,    # in the air, horizontal target speed = max(walk, current) (keeps run momentum)
    "ground_snap": 1.2,               # walking down: snap to surface when drop <= this (m)
    "npc_bullet_range": 100.0,        # NPC bullet max travel (m)
    "npc_lead": 0.0,                  # fraction of lead NPC drones apply
    "zone_center_uniform_area": True,  # next centre uniform over the allowed disc (else uniform radius)
    "drop_vertical_reach": 4.0,       # supply-drop ring vertical tolerance (m)
    "cube_scatter": 2.5,              # cubes dropped on death scatter radius (m)
    "send_end_on_death": True,        # send `end` right when a mech dies (its place is final then)
    "los_step": 0.75,                 # vision ray sample spacing (m)
    "builtin_nav": True,              # built-in bots follow a 4 m ground flow field to the next zone
    "builtin_item_radius": 90.0,      # built-in bots see items within this radius without LOS (cheap)
    "collision_radius_scales": False,  # replay: wall collision uses the base radius even with cubes (hit cylinder scales)
    "shot_offset_horizontal": True,   # shot origin offset is horizontal only (fitted on replay mortar shells; docs/SIM_ISSUES.md #1)
}


def sim_config(overrides: dict | None = None) -> dict:
    s = copy.deepcopy(SIM_DEFAULTS)
    for k, v in (overrides or {}).items():
        if k not in s:
            raise KeyError(f"unknown sim switch {k}; known: {sorted(s)}")
        t = type(s[k])
        if t is bool and isinstance(v, str):
            v = v.lower() in ("1", "true", "yes", "on")
        s[k] = t(v)
    return s


def apply_overrides(cfg: dict, overrides: dict) -> dict:
    """dotted overrides, e.g. {'zone.phases.0.hold_s': 30, 'weapons.gun.damage': 50}"""
    cfg = copy.deepcopy(cfg)
    for path, v in overrides.items():
        cur = cfg
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur[int(p)] if isinstance(cur, list) else cur[p]
        last = parts[-1]
        if isinstance(cur, list):
            cur[int(last)] = v
        else:
            old = cur.get(last)
            if isinstance(old, bool):
                v = str(v).lower() in ("1", "true", "yes")
            elif isinstance(old, (int, float)) and isinstance(v, str):
                v = float(v)
            cur[last] = v
    return cfg
