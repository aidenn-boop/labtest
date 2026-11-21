"""Shared match runner for the Go bot: plays (bot, MECH_P, MECH_SQ, squad variant) on seeds, with a
persistent per-match cache so every configuration/seed pair is played at most once across all tools."""
import hashlib, json, os, subprocess
from pathlib import Path

LAB = Path(os.environ.get("LAB", Path.home() / "mech/lab"))
PY = os.environ.get("LAB_PY", str(Path.home() / "mech/.venv/bin/python"))
ARENA = os.environ.get("LAB_ARENA", str(Path.home() / "mech/kit/mechfight-kit/arena"))
CACHE = Path(os.environ.get("LAB_CACHE", LAB / "exp/match_cache.jsonl"))
FIELD = Path(os.environ.get("LAB_FIELD", Path.home() / "mech/field"))
_cache = None


def cfg_key(bot, p, sq, variant):
    return hashlib.sha1(json.dumps([bot, p or {}, sq or {}, variant], sort_keys=True).encode()).hexdigest()[:16]


def _load():
    global _cache
    if _cache is None:
        _cache = {}
        if CACHE.exists():
            for l in open(CACHE):
                r = json.loads(l)
                _cache[(r["key"], r["seed"])] = r
    return _cache


def cached(bot, seeds, p=None, sq=None, variant="jpl=0.5"):
    """Read-only: cached results only (never starts matches; safe beside a running experiment)."""
    c = _load()
    k = cfg_key(bot, p, sq, variant)
    return {s: c[(k, s)] for s in seeds if (k, s) in c}


def play(bot, seeds, p=None, sq=None, variant="jpl=0.5", jobs=None):
    """Returns {seed: record}. Plays only seeds not already cached."""
    jobs = jobs or int(os.environ.get("HARNESS_JOBS", "7"))
    c = _load()
    k = cfg_key(bot, p, sq, variant)
    todo = [s for s in seeds if (k, s) not in c]
    if todo:
        env = (f"MECH_P='{json.dumps(p)}' " if p else "") + (f"MECH_SQ='{json.dumps(sq)}' " if sq else "")
        tmp = LAB / f"exp/.h-{os.getpid()}.jsonl"
        tmp.unlink(missing_ok=True)
        argv = [PY, str(LAB / "sim/run_matches.py"), "--arena", ARENA, "--bot", env + bot,
                "--seeds", ",".join(map(str, todo)), "--jobs", str(jobs), "--out", str(tmp)]
        key, _, val = variant.partition("=")
        if key == "ffa":   # variant "ffa=<name>": lineup read from ~/mech/field/lineup_<name>
            argv += ["--mode", "ffa", "--ffa", open(FIELD / f"lineup_{val}").read().strip()]
        else:
            argv += ["--mode", "squad", "--squad", f"join_per_loss={val}" if key == "jpl" else variant]
        subprocess.run(argv, capture_output=True, text=True)
        rows = [json.loads(l) for l in open(tmp)] if tmp.exists() else []
        tmp.unlink(missing_ok=True)
        with open(CACHE, "a") as f:
            for r in rows:
                rec = {"key": k, "seed": r["seed"], "bot": bot, "p": p, "sq": sq, "variant": variant,
                       "score": r["score"], "kills": r["kills"], "death_t": r["death_t"],
                       "death_cause": r.get("death_cause"), "dmg_dealt": r.get("dmg_dealt"), "dmg_taken": r.get("dmg_taken"),
                       "killed_by": r.get("killed_by"), "zone_dmg": r.get("zone_dmg"), "loadout": r.get("loadout"),
                       "tb": r.get("bot_stderr_tracebacks") or 0, "late": r.get("late_replies") or 0,
                       "lat_p99": r.get("lat_p99"), "stats": r.get("bot_stats") if isinstance(r.get("bot_stats"), dict) else None}
                f.write(json.dumps(rec) + "\n")
                c[(k, r["seed"])] = rec
    return {s: c[(k, s)] for s in seeds if (k, s) in c}
