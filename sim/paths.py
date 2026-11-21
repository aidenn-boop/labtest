"""Locate the arena folder (no numpy needed)."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def find_arena(explicit=None):
    cands = [explicit, os.environ.get("MECH_ARENA"), os.path.join(REPO, "arena"), os.path.join(HERE, "arena"),
             os.path.join(REPO, "..", "..", "mech-royale", "starter", "mechfight-kit", "arena")]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "v2", "world.json")):
            return os.path.abspath(c)
    raise FileNotFoundError("arena not found: pass --arena or set MECH_ARENA to the kit's arena/ folder")
