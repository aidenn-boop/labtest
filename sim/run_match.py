#!/usr/bin/env python3
"""Run a single match and optionally write a trace for the viewer.

  uv run --no-project --with numpy python sim/run_match.py --mode squad --bot "python3 -u bot/main.py" --seed 3 --trace /tmp/m3.jsonl.gz
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import apply_overrides, load_config, sim_config  # noqa: E402
from runner import run_match  # noqa: E402
from world import World  # noqa: E402


def kv_list(items):
    out = {}
    for it in items or []:
        k, v = it.split("=", 1)
        try:
            v = json.loads(v)
        except Exception:
            pass
        out[k] = v
    return out


def common_args(ap):
    ap.add_argument("--mode", choices=["squad", "ffa"], default="squad")
    ap.add_argument("--bot", default="python3 -u bot/main.py", help="entrant command (or builtin:<tier>)")
    ap.add_argument("--ffa", default=None, help="FFA lineup, e.g. 'bot*1,builtin:expert*5,builtin:hard*5,builtin:brawler*3,builtin:sniper*2'")
    ap.add_argument("--enforce", action="store_true", help="enforce the 50 ms reply budget (late = missed); default only measures")
    ap.add_argument("--budget-ms", type=float, default=None)
    ap.add_argument("--validate", action="store_true", help="schema-check every obs and action")
    ap.add_argument("--arena", default=None)
    ap.add_argument("--set", action="append", help="config override, e.g. --set weapons.gun.damage=50")
    ap.add_argument("--sim", action="append", help="simulator switch, e.g. --sim lightning_cone_half=false")
    ap.add_argument("--squad", action="append", help="squad director param, e.g. --squad all_engage_t=240")
    ap.add_argument("--full", action="store_true", help="keep simulating after the tracked bots die")
    ap.add_argument("--logs", default=None, help="directory for bot stderr logs")
    ap.add_argument("--cwd", default=None, help="working directory for bot commands (default: repo root)")


def build(args):
    cfg = load_config()
    if args.set:
        cfg = apply_overrides(cfg, kv_list(args.set))
    sim = sim_config(kv_list(args.sim))
    squad = kv_list(args.squad)
    return cfg, sim, squad


def main():
    ap = argparse.ArgumentParser()
    common_args(ap)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--trace", default=None, help="write trace JSONL(.gz) for sim/viewer.html")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg, sim, squad = build(args)
    world = World(args.arena)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    res = run_match(world, args.seed, mode=args.mode, bot_cmds=[args.bot], ffa_spec=args.ffa, enforce=args.enforce, budget_ms=args.budget_ms,
                    trace_path=args.trace, log_dir=args.logs, validate=args.validate, cfg=cfg, sim=sim, squad=squad,
                    stop_when_tracked_dead=not args.full, cwd=args.cwd or repo, verbose=True)
    if args.json:
        print(json.dumps(res))
        return
    print(f"seed {res['seed']} mode {res['mode']} sim_t {res['sim_t']} wall {res['wall_s']}s")
    for m in res["mechs"]:
        flag = "*" if m["tracked"] else " "
        lat = f" lat p50/p99/max {m.get('lat_p50')}/{m.get('lat_p99')}/{m.get('lat_max')} ms missed {m['missed']} late {m['late']}" if "lat_p50" in m else ""
        print(f"{flag}{m['id']:2d} {m['name'][:16]:16s} place {m['place']:2d} kills {m['kills']} alive {m['alive']!s:5s} death {m['death_t']} by {m['killed_by']} with {m['killed_with']} hp {m['hp']} cubes {m['cubes']} {m['loadout']} dealt {m['dmg_dealt']} taken {m['dmg_taken']}{lat}")


if __name__ == "__main__":
    main()
