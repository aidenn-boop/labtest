"""One shard of the practice-mode screen (psq.py): every (run, seed) pair, numbered, index % nshards == shard."""
import argparse, os, time
from collections import defaultdict

import harness
import psq

GO = os.environ["LAB_BOT"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=20)
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--deadline-min", type=float, default=320)
    a = ap.parse_args()
    lo, hi = map(int, a.seeds.split("-"))
    seeds = list(range(lo, hi + 1))
    _, R = psq.runs()
    pairs = [(i, s) for s in seeds for i in range(len(R))]
    mine = defaultdict(list)
    for k, (i, s) in enumerate(pairs):
        if k % a.nshards == a.shard:
            mine[i].append(s)
    t0 = time.time()
    print(f"shard {a.shard}: {sum(len(v) for v in mine.values())} matches", flush=True)
    for i, ss in sorted(mine.items()):
        for c in range(0, len(ss), 12):
            if (time.time() - t0) / 60 > a.deadline_min:
                print("deadline reached", flush=True)
                return
            got = harness.play(GO, ss[c:c + 12], None, R[i], "jpl=0.5", jobs=a.jobs)
            print(f"run {i:2d}: +{len(got)} ({(time.time() - t0) / 60:.0f} min)", flush=True)
    print("SHARD-DONE", flush=True)


if __name__ == "__main__":
    main()
