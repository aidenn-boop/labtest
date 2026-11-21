"""One shard of the free-for-all screen (ffa_doe.py design), for parallel CI runners.

Every (run, seed) pair of the design over --seeds is numbered; this shard plays the pairs with
index % nshards == shard, batched per run. --validate instead plays the shipped run (no overrides)
on the given seeds, to compare against local results (cross-machine determinism check).
Stops starting new batches after --deadline-min minutes so results are uploaded before the job limit.

  ffa_shard.py --shard 3 --nshards 20 --seeds 50090-50199 --jobs 1
  ffa_shard.py --validate --seeds 50000-50005 --jobs 1
"""
import argparse, time
from collections import defaultdict

import harness
import ffa_doe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--deadline-min", type=float, default=320)
    ap.add_argument("--validate", action="store_true")
    a = ap.parse_args()
    lo, hi = map(int, a.seeds.split("-"))
    seeds = list(range(lo, hi + 1))
    t0 = time.time()
    if a.validate:
        R = harness.play(ffa_doe.GO, seeds, None, None, "ffa=A", jobs=a.jobs)
        print(f"validate: {len(R)} matches", {s: R[s]["score"] for s in sorted(R)}, flush=True)
        return
    D = ffa_doe.design()
    # All runs including the shipped one: every seed is played by every configuration in the same
    # environment, so per-seed demeaning in the analysis cancels any cloud-vs-local offset.
    pairs = [(i, s) for s in seeds for i in range(len(D))]
    mine = defaultdict(list)
    for k, (i, s) in enumerate(pairs):
        if k % a.nshards == a.shard:
            mine[i].append(s)
    print(f"shard {a.shard}/{a.nshards}: {sum(len(v) for v in mine.values())} matches over {len(mine)} runs", flush=True)
    for i, ss in sorted(mine.items()):
        for c in range(0, len(ss), 6):
            if (time.time() - t0) / 60 > a.deadline_min:
                print("deadline reached; stopping", flush=True)
                return
            R = harness.play(ffa_doe.GO, ss[c:c + 6], ffa_doe.params(D[i]), None, "ffa=A", jobs=a.jobs)
            print(f"run {i:2d}: +{len(R)}  ({(time.time() - t0) / 60:.0f} min)", flush=True)
    print("SHARD-DONE", flush=True)


if __name__ == "__main__":
    main()
