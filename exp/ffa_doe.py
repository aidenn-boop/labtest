"""Free-for-all screen: folded-over Plackett-Burman N=12 (24 runs, resolution IV) over 11 base (FFA-mode)
parameters, passed via MECH_P. Low level = shipped value for every factor, so the all-low run is exactly
the shipped bot (whose matches on the same seeds are shared with ffa_base.py through the cache).

  ffa_doe.py run --seeds 50000-50199 [--jobs 3]
  ffa_doe.py analyze [--seeds 50000-50199]
"""
import argparse, json, sys
from pathlib import Path

import numpy as np
from scipy import stats

import harness
import doe

import os
GO = os.environ.get("LAB_BOT", str(Path.home() / "mech/gg/inst/mechbot"))
# name: (low = shipped, high). LAB_DESIGN selects the factor set (1 = first screen, 2 = second screen).
DESIGNS = {
    "1": {
        "Vulture": (0, 0.4), "Odds": (0, 0.6), "OddsFlee": (0, 0.3), "FarmNPC": (True, False), "Shy": (0, 150),
        "EarlyHide": (0, 60), "Evade": (False, True), "CrateUntil": (150, 60), "EngageDist": (60, 80),
        "RetreatHP": (0.38, 0.28), "LootRadius": (90, 150),
    },
    # Second screen: mechanics the rules document that the shipped FFA mode leaves off (drops: full HP + 3 cubes;
    # mortar: lobs over cover, beats lightning), our measured FireGate, and the FFA-mode switches whose
    # alternative values the code itself uses in practice mode.
    "2": {
        "UseDrops": (False, True), "NoMortar": (True, False), "FireGate": (False, True), "Ghost": (True, False),
        "Peek": (True, False), "CrowdHide": (True, False), "HomeExpo": (120, 60), "Explore": (False, True),
        "LaserFirst": (True, False), "RecoverHP": (0.8, 0.6), "LaserRange": (85, 120),
    },
}
FACTORS = DESIGNS[os.environ.get("LAB_DESIGN", "1")]
F = list(FACTORS)


def pb12():
    gen = "++-+++---+-"
    rows = [[1 if c == "+" else -1 for c in (gen[-s:] + gen[:-s] if s else gen)] for s in range(11)]
    rows.append([-1] * 11)
    return rows


def design():
    base = pb12()
    D = base + [[-x for x in r] for r in base]
    assert doe.verify(D) == "ok", doe.verify(D)
    return D


def params(row):
    p = {f: FACTORS[f][0 if x < 0 else 1] for f, x in zip(F, row)}
    shipped = all(x < 0 for x in row)
    return None if shipped else p   # shipped run -> no override (same cache key as the baseline)


def run(a):
    D = design()
    seeds = list(range(*map(int, a.seeds.split("-"))))
    seeds.append(seeds[-1] + 1)
    for c0 in range(0, len(seeds), a.chunk):
        chunk = seeds[c0:c0 + a.chunk]
        for i, row in enumerate(D):
            R = harness.play(GO, chunk, params(row), None, "ffa=A", jobs=a.jobs)
            print(f"chunk {c0 // a.chunk} run {i:2d}: {len(R)} matches", flush=True)
    print("FFA-DOE-DONE", flush=True)


def analyze(a):
    D = design()
    seeds = list(range(*map(int, a.seeds.split("-")))); seeds.append(seeds[-1] + 1)
    rows = []
    for i, row in enumerate(D):
        R = harness.cached(GO, seeds, params(row), None, "ffa=A")
        rows += [(row, R[s]) for s in R]
    n_seeds = len({r["seed"] for _, r in rows})
    late = sum(r["late"] for _, r in rows)
    print(f"matches {len(rows)} over {n_seeds} seeds; late replies {late}; crashes {sum(r['tb'] for _, r in rows)}")
    by = {}
    for _, r in rows:
        by.setdefault(r["seed"], []).append(r["score"])
    sm = {s: np.mean(v) for s, v in by.items()}
    y = np.array([r["score"] - sm[r["seed"]] for _, r in rows])
    X = np.column_stack([np.ones(len(rows)), np.array([row for row, _ in rows], float)])
    XtXi = np.linalg.inv(X.T @ X); b = XtXi @ X.T @ y; e = y - X @ b
    h = np.einsum("ij,jk,ik->i", X, XtXi, X)
    V = XtXi @ (X.T * (e / (1 - h)) ** 2) @ X @ XtXi
    eff, se = 2 * b[1:], 2 * np.sqrt(np.diag(V))[1:]
    p = 2 * stats.norm.sf(np.abs(eff / se)); n = len(p); o = np.argsort(p)
    q = np.empty(n); q[o] = np.minimum.accumulate((p[o] * n / np.arange(1, n + 1))[::-1])[::-1]
    ship = [r["score"] for row, r in rows if all(x < 0 for x in row)]
    print(f"overall mean {np.mean([r['score'] for _, r in rows]):.3f}; shipped run mean {np.mean(ship):.3f} (n={len(ship)})")
    print(f"{'param':11s} {'ship':>6s} {'alt':>6s}  {'effect alt-ship':>17s}  {'q':>6s}  mechanism (ship -> alt)")
    for i in np.argsort(-eff):
        lo_r = [r for row, r in rows if row[i] < 0]; hi_r = [r for row, r in rows if row[i] > 0]
        mech = []
        for k in ["kills", "death_t"]:
            av = np.mean([(r[k] if r[k] is not None else 480) for r in lo_r]); bv = np.mean([(r[k] if r[k] is not None else 480) for r in hi_r])
            mech.append(f"{k} {av:.1f}->{bv:.1f}")
        tag = ("ADOPT" if eff[i] > 0 else "REJECT") if q[i] < 0.05 else ""
        print(f"{F[i]:11s} {str(FACTORS[F[i]][0]):>6s} {str(FACTORS[F[i]][1]):>6s}  {eff[i]:+7.3f} ± {1.96 * se[i]:.3f}  {q[i]:6.3f}  {tag:6s} {'; '.join(mech)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "analyze"])
    ap.add_argument("--seeds", default="50000-50199")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--chunk", type=int, default=30)
    a = ap.parse_args()
    run(a) if a.cmd == "run" else analyze(a)
