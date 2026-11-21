"""Screening experiment: folded-over Plackett-Burman design over the bot's two-level factors.

Design: the 24-run Plackett-Burman matrix (cyclic generator, Plackett & Burman 1946) gives 23
orthogonal +/-1 columns; we use the first k. Folding over (appending the negated runs) yields 48 runs of
resolution IV: main effects are not aliased with any two-factor interaction.

Every run plays the SAME seeds (common random numbers). Results are cached per (run, seed, squad
variant) in exp/doe_cache.jsonl, so the job resumes after interruption and can be extended with
more seeds. Run detached:  setsid nohup ~/mech/.venv/bin/python -u exp/doe.py run ... &

Commands:
  doe.py design                       print and verify the design matrix
  doe.py run --bot B --seeds 10000-10127 --variant jpl=0.75 [--jobs 7]
"""
import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path

LAB = Path.home() / "mech/lab"
PY = str(Path.home() / "mech/.venv/bin/python")
ARENA = str(Path.home() / "mech/kit/mechfight-kit/arena")
CACHE = LAB / "exp/doe_cache.jsonl"

FACTORS = ["los_gate", "los_seek", "lead_damp", "laser_hitscan", "close_with_lightning", "target_lock",
           "rear_pref", "strafe", "fight_boost", "laser_dodge", "mortar_use", "weapon_first", "farm_npc",
           "farm_crate", "drops", "upgrade_hp_first", "never_retreat", "heal_pause", "hunt", "fight_close",
           "lightning_first", "zone_tight"]

PB24 = "++++-+-++--++--+-+----"  # first 22 of the 23-symbol generator, extended below


def pb24():
    gen = "+++++-+-++--++--+-+----"           # Plackett-Burman N=24 generator (23 symbols)
    rows = []
    for s in range(23):
        r = gen[-s:] + gen[:-s] if s else gen
        rows.append([1 if c == "+" else -1 for c in r])
    rows.append([-1] * 23)
    return rows


def design(k):
    base = [r[:k] for r in pb24()]
    return base + [[-x for x in r] for r in base]


def verify(D):
    n, k = len(D), len(D[0])
    for a in range(k):
        if sum(r[a] for r in D) != 0:
            return f"column {a} unbalanced"
        for b in range(a + 1, k):
            if sum(r[a] * r[b] for r in D) != 0:
                return f"columns {a},{b} not orthogonal"
    # Resolution IV check: every main effect orthogonal to every two-factor interaction.
    for a in range(k):
        for b in range(k):
            for c in range(b + 1, k):
                if a not in (b, c) and sum(r[a] * r[b] * r[c] for r in D) != 0:
                    return f"main {a} aliased with interaction {b}x{c}"
    return "ok"


def parse_seeds(s):
    out = []
    for part in s.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b) + 1)) if b else [int(a)]
    return out


def run_key(params, variant, bot):
    return hashlib.sha1(json.dumps([params, variant, bot], sort_keys=True).encode()).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["design", "run"])
    ap.add_argument("--bot")
    ap.add_argument("--seeds", default="10000-10127")
    ap.add_argument("--variant", default="jpl=0.75")
    ap.add_argument("--jobs", type=int, default=7)
    ap.add_argument("--chunk", type=int, default=32)
    args = ap.parse_args()

    D = design(len(FACTORS))
    v = verify(D)
    if args.cmd == "design":
        for i, r in enumerate(D):
            print(f"{i:2d} " + "".join("+" if x > 0 else "-" for x in r))
        print(f"{len(D)} runs x {len(FACTORS)} factors; verification: {v}")
        return
    if v != "ok":
        sys.exit(f"design failed verification: {v}")

    squad = []
    if args.variant:
        k, _, val = args.variant.partition("=")
        squad = [f"join_per_loss={val}"] if k == "jpl" else [args.variant]
    seeds = parse_seeds(args.seeds)
    done = set()
    if CACHE.exists():
        for l in open(CACHE):
            r = json.loads(l)
            done.add((r["run_key"], r["seed"]))
    for chunk_start in range(0, len(seeds), args.chunk):
        chunk = seeds[chunk_start:chunk_start + args.chunk]
        for i, row in enumerate(D):
            params = {f: (1 if x > 0 else 0) for f, x in zip(FACTORS, row)}
            key = run_key(params, args.variant, args.bot)
            todo = [s for s in chunk if (key, s) not in done]
            if not todo:
                continue
            cmd = f"MECH_P='{json.dumps(params)}' {PY} -u {args.bot}"
            tmp = LAB / f"exp/.doe-{os.getpid()}.jsonl"
            tmp.unlink(missing_ok=True)
            argv = [PY, str(LAB / "sim/run_matches.py"), "--mode", "squad", "--arena", ARENA, "--bot", cmd,
                    "--seeds", ",".join(map(str, todo)), "--jobs", str(args.jobs), "--out", str(tmp)]
            for s in squad:
                argv += ["--squad", s]
            subprocess.run(argv, capture_output=True, text=True)
            rows = [json.loads(l) for l in open(tmp)] if tmp.exists() else []
            tmp.unlink(missing_ok=True)
            with open(CACHE, "a") as f:
                for r in rows:
                    rec = {"run_key": key, "run": i, "variant": args.variant, "bot": args.bot, "seed": r["seed"],
                           "params": params, "score": r["score"], "kills": r["kills"], "death_t": r["death_t"],
                           "dmg_dealt": r["dmg_dealt"], "dmg_taken": r["dmg_taken"], "cubes": r["cubes"],
                           "zone_dmg": r["zone_dmg"], "death_cause": r["death_cause"],
                           "tb": r.get("bot_stderr_tracebacks") or 0, "late": r.get("late_replies") or 0,
                           "lat_p99": r.get("lat_p99"), "bot_stats": r.get("bot_stats")}
                    f.write(json.dumps(rec) + "\n")
                    done.add((key, r["seed"]))
            print(f"chunk {chunk_start // args.chunk} run {i:2d}: +{len(rows)} matches", flush=True)
    print("DOE-DONE", flush=True)


if __name__ == "__main__":
    main()
