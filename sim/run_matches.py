#!/usr/bin/env python3
"""Run many matches in parallel and summarise; optional paired-seed comparison of two bots.

  sim/run_matches.py --mode squad --bot "python3 -u bot/main.py" --seeds 0-47 --jobs 4
  sim/run_matches.py --mode squad --bot "python3 -u bot/main.py" --bot-b "python3 -u /tmp/old/bot/main.py" --seeds 0-95 --jobs 4
  sim/run_matches.py --mode ffa --bot "python3 -u bot/main.py" --ffa "bot*2,builtin:expert*6,builtin:hard*8" --seeds 0-31

Score = mean of (16 - place) over the tracked bot's matches (in FFA with several copies of the bot,
each copy is one sample). Use `uv run --no-project --with numpy python sim/run_matches.py ...`.
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from run_match import build, common_args  # noqa: E402

_WORLD = None


def parse_seeds(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def _init(arena):
    global _WORLD
    from world import World
    _WORLD = World(arena)


def _job(job):
    from runner import run_match
    (seed, bots, label, args_d) = job
    class A:  # noqa
        pass
    a = A()
    a.__dict__.update(args_d)
    cfg, sim, squad = build(a)
    trace = None
    if a.traces:
        os.makedirs(a.traces, exist_ok=True)
        trace = os.path.join(a.traces, f"{label}_seed{seed}.jsonl.gz")
    logs = None
    if a.logs:
        logs = os.path.join(a.logs, label)
        os.makedirs(logs, exist_ok=True)
    try:
        res = run_match(_WORLD, seed, mode=a.mode, bot_cmds=bots, ffa_spec=a.ffa, enforce=a.enforce, budget_ms=a.budget_ms, trace_path=trace,
                        log_dir=logs, validate=a.validate, cfg=cfg, sim=sim, squad=squad, stop_when_tracked_dead=not a.full, cwd=a.cwd)
    except Exception as e:  # keep the batch going
        import traceback
        return {"seed": seed, "label": label, "error": f"{e}\n{traceback.format_exc()}"}
    res["label"] = label
    return res


def tracked_points(res):
    return [16 - m["place"] for m in res["mechs"] if m["tracked"]]


def t_crit(n):
    table = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23, 15: 2.13, 20: 2.09, 30: 2.04, 40: 2.02, 60: 2.00, 120: 1.98}
    df = max(1, n - 1)
    for k in sorted(table):
        if df <= k:
            return table[k]
    return 1.96


def summarize(results, label):
    pts, kills, alive_t, deaths = [], [], [], {}
    lat99, missed, late = [], 0, 0
    for r in results:
        if "error" in r:
            continue
        for m in r["mechs"]:
            if not m["tracked"]:
                continue
            pts.append(16 - m["place"])
            kills.append(m["kills"])
            alive_t.append(m["death_t"] if m["death_t"] is not None else r["sim_t"])
            deaths[m["killed_with"] or "survived"] = deaths.get(m["killed_with"] or "survived", 0) + 1
            missed += m["missed"]
            late += m["late"]
            if "lat_p99" in m:
                lat99.append(m["lat_p99"])
    if not pts:
        print(f"[{label}] no results")
        return None
    mu = statistics.fmean(pts)
    sd = statistics.stdev(pts) if len(pts) > 1 else 0.0
    ci = t_crit(len(pts)) * sd / math.sqrt(len(pts)) if len(pts) > 1 else float("nan")
    print(f"[{label}] n={len(pts)} score mean(16-place)={mu:.3f} ±{ci:.3f} (95% CI)  kills/match={statistics.fmean(kills):.2f}  "
          f"time alive mean={statistics.fmean(alive_t):.1f}s  causes={deaths}  missed={missed} late={late}"
          + (f"  lat_p99 max={max(lat99):.1f}ms" if lat99 else ""))
    return mu


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common_args(ap)
    ap.add_argument("--bot-b", default=None, help="second bot command for a paired comparison (same seeds)")
    ap.add_argument("--seeds", default="0-15")
    ap.add_argument("--jobs", type=int, default=2, help="parallel matches (max 4; the machine is shared)")
    ap.add_argument("--traces", default=None, help="directory to write per-match traces for the viewer")
    ap.add_argument("--out", default=None, help="write all results as JSONL here")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    jobs = max(1, min(os.cpu_count() or 4, args.jobs))
    seeds = parse_seeds(args.seeds)
    repo = os.path.dirname(HERE)
    args.cwd = args.cwd or repo
    args_d = {k: v for k, v in vars(args).items()}
    h2h = args.mode == "ffa" and args.ffa and "bot2" in args.ffa
    if h2h:
        if not args.bot_b:
            ap.error("--ffa uses bot2: pass --bot-b")
        work = [(s, [args.bot, args.bot_b], "H2H", args_d) for s in seeds]
    else:
        work = [(s, [args.bot], "A", args_d) for s in seeds]
        if args.bot_b:
            work += [(s, [args.bot_b], "B", args_d) for s in seeds]
    t0 = time.time()
    results = []
    ctx = mp.get_context("fork")
    with ctx.Pool(jobs, initializer=_init, initargs=(args.arena,)) as pool:
        for r in pool.imap_unordered(_job, work):
            results.append(r)
            if "error" in r:
                print(f"[{r['label']}] seed {r['seed']} ERROR {r['error']}", file=sys.stderr)
                continue
            if not args.quiet:
                for m in r["mechs"]:
                    if m["tracked"]:
                        lat = f" p99 {m.get('lat_p99')}ms missed {m['missed']} late {m['late']}" if "lat_p99" in m else ""
                        print(f"[{r['label']}] seed {r['seed']:3d} slot {m['id']:2d} place {m['place']:2d} pts {16 - m['place']:2d} kills {m['kills']} "
                              f"alive {m['death_t'] if m['death_t'] is not None else r['sim_t']:6.1f}s cause {m['killed_with'] or 'survived'}"
                              f"{'' if m['killed_by'] is None else ' by ' + str(m['killed_by']) + ':' + r['mechs'][m['killed_by']]['name']} "
                              f"loadout {'+'.join(m['loadout'])} cubes {m['cubes']} zone_dmg {m['zone_dmg']}{lat}"
                              f"{' TRACEBACKS ' + str(m['stderr_tracebacks']) if m.get('stderr_tracebacks') else ''}  (wall {r['wall_s']}s)", flush=True)
    wall = time.time() - t0
    if h2h:
        # split each head-to-head match into an A view (copies of --bot) and a B view (copies of --bot-b)
        def view(r, name, lab):
            if "error" in r:
                return dict(r, label=lab)
            v = dict(r, label=lab)
            v["mechs"] = [dict(m, tracked=m["tracked"] and m["name"] == name) for m in r["mechs"]]
            return v
        results = [view(r, "bot", "A") for r in results] + [view(r, "bot2", "B") for r in results]
    if args.out:
        with open(args.out, "w") as f:
            for r in sorted(results, key=lambda r: (r.get("label", ""), r["seed"])):
                if "error" in r:
                    f.write(json.dumps({"seed": r["seed"], "label": r.get("label"), "error": r["error"]}) + "\n")
                    continue
                for m in r["mechs"]:
                    if not m["tracked"]:
                        continue
                    kb = m["killed_by"]
                    rec = {"seed": r["seed"], "mode": r["mode"], "label": r["label"], "bot": args.bot if r["label"] == "A" else args.bot_b,
                           "slot": m["id"], "place": m["place"], "score": 16 - m["place"], "death_t": m["death_t"],
                           "death_cause": m["killed_with"] or ("survived" if m["alive"] else "unknown"),
                           "killed_by": kb, "killed_by_name": None if kb is None else r["mechs"][kb]["name"], "kills": m["kills"],
                           "cubes": m["cubes"], "hp_at_end": m["hp"], "loadout": m["loadout"], "dmg_dealt": m["dmg_dealt"], "dmg_taken": m["dmg_taken"],
                           "zone_dmg": m["zone_dmg"], "t_outside": m["t_outside"], "missed_replies": m["missed"], "late_replies": m["late"],
                           "lat_p50": m.get("lat_p50"), "lat_p99": m.get("lat_p99"), "lat_max": m.get("lat_max"), "ready_ms": m.get("ready_ms"),
                           "bad_lines": m.get("bad_lines"), "stale": m.get("stale"), "bot_stderr_tracebacks": m.get("stderr_tracebacks", 0),
                           "last_traceback": m.get("last_traceback"), "bot_stats": m.get("bot_stats"), "sim_t": r["sim_t"], "wall_s": r["wall_s"],
                           "alive_at_end": sum(1 for x in r["mechs"] if x["alive"])}
                    f.write(json.dumps(rec) + "\n")
    A = sorted([r for r in results if r.get("label") == "A"], key=lambda r: r["seed"])
    B = sorted([r for r in results if r.get("label") == "B"], key=lambda r: r["seed"])
    sim_s = sum(r.get("sim_t", 0) for r in results if "error" not in r)
    print(f"\n{len(results)} matches in {wall:.1f}s wall ({sim_s / max(wall, 1e-9):.1f} sim-s per wall-s across {jobs} jobs)")
    summarize(A, "A " + args.bot)
    if B:
        summarize(B, "B " + args.bot_b)
        a_by = {r["seed"]: statistics.fmean(tracked_points(r)) for r in A if "error" not in r and tracked_points(r)}
        b_by = {r["seed"]: statistics.fmean(tracked_points(r)) for r in B if "error" not in r and tracked_points(r)}
        common = sorted(set(a_by) & set(b_by))
        diffs = [a_by[s] - b_by[s] for s in common]
        if len(diffs) > 1:
            mu = statistics.fmean(diffs)
            sd = statistics.stdev(diffs)
            ci = t_crit(len(diffs)) * sd / math.sqrt(len(diffs))
            wins = sum(d > 0 for d in diffs)
            losses = sum(d < 0 for d in diffs)
            print(f"paired A-B over {len(diffs)} seeds: mean diff {mu:+.3f} ± {ci:.3f} (95% CI) [{mu - ci:+.3f}, {mu + ci:+.3f}]  A better on {wins}, B better on {losses}, ties {len(diffs) - wins - losses}")


if __name__ == "__main__":
    main()
