"""Practice-mode (squad) screen: folded Plackett-Burman N=24 (48 runs, resolution IV) over 23 practice parameters,
passed through MECH_SQ, plus run 48 = the shipped bot. Practice matches replay exactly per seed (house bots run
inside the simulator), so every seed is a perfectly paired block.

Why the simulator is trusted here: it scored this bot 5.52 on squad variant jpl=0.5; the official scorer gave
four re-seeded copies 5.29-5.46 (mean 5.39) and the original 6.00.

  psq_shard.py --shard k --nshards 20 --seeds A-B --jobs 3     (CI)
  psq.py analyze <results.jsonl...>                            (local)
"""
import json, sys

import numpy as np
from scipy import stats

import doe

# name: (low, high, shipped). Odds/Vulture from the first practice screen were replaced: they only act when
# Ghost is on, and practice mode turns Ghost off.
FACTORS = {
    "Stick": (120, 300, 200), "HPw": (0.05, 0.2, 0.1), "LowBonus": (100, 300, 200), "MaxTarget": (180, 300, 240),
    "LoseRule": (False, True, True), "FightLoot": (40, 100, 70), "FarmHP": (0.5, 0.9, 0.5),
    "LaserSeek": (160, 320, 240), "Finish": (14, 26, 20), "Backstab": (0, 60, 40), "LaserRange": (110, 150, 120),
    "LootRadius": (100, 200, 150), "GunRange": (70, 100, 85), "RetreatHP": (0.28, 0.48, 0.38),
    "FightBoost": (0, 40, 0), "DodgeBoost": (0, 25, 0), "ThreatBoost": (0, 30, 0), "Pursue": (0, 0.6, 0),
    "AllFoes": (False, True, False), "FinishRush": (False, True, False), "LethalK": (0.7, 0.9, 0.8),
    "FinishHP": (0.3, 0.46, 0.38), "EngageDist": (45, 75, 60),
}
F = list(FACTORS)


def runs():
    D = doe.design(len(F))
    assert doe.verify(D) == "ok"
    out = [{f: FACTORS[f][0 if x < 0 else 1] for f, x in zip(F, row)} for row in D]
    out.append(None)  # control: shipped
    return D, out


def analyze(files):
    rows = [json.loads(l) for f in files for l in open(f)]
    rows = [r for r in rows if r["variant"] == "jpl=0.5"]
    D, R = runs()
    key = lambda p: json.dumps(p, sort_keys=True)
    idx = {key(p): i for i, p in enumerate(R)}
    for r in rows:
        r["run"] = idx.get(key(r["sq"] or None), -1)
    des = [r for r in rows if 0 <= r["run"] < len(D)]
    ctrl = [r for r in rows if r["run"] == len(D)]
    print(f"design matches {len(des)}, control {len(ctrl)}, late {sum(r['late'] for r in rows)}, crashes {sum(r['tb'] for r in rows)}")
    sm = {}
    for r in des:
        sm.setdefault(r["seed"], []).append(r["score"])
    sm = {s: np.mean(v) for s, v in sm.items()}
    y = np.array([r["score"] - sm[r["seed"]] for r in des])
    X = np.column_stack([np.ones(len(des)), np.array([D[r["run"]] for r in des], float)])
    XtXi = np.linalg.inv(X.T @ X); b = XtXi @ X.T @ y; e = y - X @ b
    h = np.einsum("ij,jk,ik->i", X, XtXi, X)
    V = XtXi @ (X.T * (e / (1 - h)) ** 2) @ X @ XtXi
    eff, se = 2 * b[1:], 2 * np.sqrt(np.diag(V))[1:]
    p = 2 * stats.norm.sf(np.abs(eff / se)); n = len(p); o = np.argsort(p)
    q = np.empty(n); q[o] = np.minimum.accumulate((p[o] * n / np.arange(1, n + 1))[::-1])[::-1]
    print(f"design mean {np.mean([r['score'] for r in des]):.3f}; shipped mean {np.mean([r['score'] for r in ctrl]) if ctrl else float('nan'):.3f}")
    for i in np.argsort(-eff):
        lo, hi, sh = FACTORS[F[i]]
        tag = ("HIGH better" if eff[i] > 0 else "LOW better") if q[i] < 0.05 else ""
        print(f"{F[i]:12s} low {str(lo):>6s} high {str(hi):>6s} ship {str(sh):>6s}  {eff[i]:+7.3f} ± {1.96 * se[i]:.3f}  q {q[i]:6.3f}  {tag}")


if __name__ == "__main__":
    if sys.argv[1] == "analyze":
        analyze(sys.argv[2:])
