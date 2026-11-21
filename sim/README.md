# Local Mech Royale simulator (`sim/`)

A headless re-implementation of the `mechfight/1` rules in `protocol.md`. It plays full 16-mech
matches locally, so bot versions can be compared over hundreds of seeds instead of 4 official
practice runs per hour. Bots run as subprocesses over pipes, exactly like production: hello, ready,
one obs per decision, a reply echoing the tick, then end. Timing and protocol bugs show up here too.

> **Sim scores are a proxy, not the official score.** The engine follows the rulebook closely. Movement,
> energy and jet numbers were checked against the recorded check stream, and mortar and splash numbers
> against the public replay. The house bots are our own reconstruction: the official house AI is
> unpublished. See *What is calibrated vs guessed* below, `ASSUMPTIONS.md` and `CALIBRATION.md`.
> Use the sim to *rank* bot versions (paired seeds). Confirm big decisions with an official practice run.

## Quick start

Everything runs through `sim/py`. It runs Python 3.13 with numpy, using the system Python when it
already has numpy, and otherwise `uv run --with numpy`. Nothing is installed system-wide.
The first run rasterises the map in about 1 s and caches it in `sim/.cache/`.

```sh
# 48 practice-style matches (our bot vs the 15-bot house squad), 4 parallel jobs
sim/py sim/run_matches.py --mode squad --bot "sim/py -u bot/main.py" --seeds 0-47 --jobs 4

# paired comparison: current tree vs an older commit (check it out somewhere first)
git worktree add /tmp/old <sha>
sim/py sim/run_matches.py --mode squad --bot "sim/py -u bot/main.py" --bot-b "sim/py -u /tmp/old/bot/main.py" --seeds 0-95 --jobs 4

# free-for-all: 2 copies of our bot, the kit example bot, and built-in opponents (16 entries in total)
sim/py sim/run_matches.py --mode ffa --bot "sim/py -u bot/main.py" \
  --ffa "bot*2,cmd:python3 -u ../../mech-royale/starter/mechfight-kit/python/example_bot.py*2,builtin:expert*4,builtin:hard*4,builtin:brawler*2,builtin:sniper*2" --seeds 0-31 --jobs 4

# one match with a trace, then the viewer (open the HTML in a browser)
sim/py sim/run_match.py --bot "sim/py -u bot/main.py" --seed 7 --trace out/s7.jsonl.gz --logs out/logs
python3 sim/render.py out/s7.jsonl.gz -o out/s7.html
python3 sim/render.py ../../mech-royale/starter/mechanics-v2-16.jsonl.gz -o out/official.html   # the official sample replay works too
```

The bot command runs with the repository root as its working directory (change it with `--cwd`).
`hello.map.dir` points at the arena folder. The arena is looked up in this order: `--arena`, then
`$MECH_ARENA`, then `<repo>/arena`, then the kit folder.

## Output

`run_matches.py` prints one line per match for the tracked bot (place, points, kills, time alive,
cause of death and killer, loadout, cubes, zone damage, latency p99, missed and late replies,
`TRACEBACKS n` if its stderr had Python tracebacks). It then prints:

- `score mean(16-place) ± 95% CI`, kills per match, mean time alive, and the causes of death
- with `--bot-b`: the paired difference A − B over common seeds, with a 95% CI and win/loss counts
- throughput (sim-seconds per wall-second)

`--out results.jsonl` writes one JSON line per tracked bot per match with these fields: `seed, mode, label, bot, slot,
place, score, death_t, death_cause (zone|gun|mortar|laser|lightning|npc|survived), killed_by, killed_by_name,
kills, cubes, hp_at_end, loadout, dmg_dealt, dmg_taken, zone_dmg, t_outside, missed_replies, late_replies,
lat_p50/p99/max, ready_ms, bad_lines, stale, bot_stderr_tracebacks, last_traceback, bot_stats, sim_t, wall_s`.

Each external bot process gets `MECH_STATS=<tmpfile>` and `MECH_SIM=1` in its environment. If the bot writes
JSON there at exit, it is returned as `bot_stats`. `--logs DIR` keeps every bot's stderr.

## Options (both scripts)

| Flag | Meaning |
| --- | --- |
| `--mode squad` | Practice format: entrant + 4 easy, 4 medium, 4 hard and 3 expert house bots. The entrant's slot is `seed % 16`. |
| `--mode ffa --ffa SPEC` | 16 entries such as `bot*2,bot2*1,cmd:<command>*3,builtin:<tier>*10`. `bot` is `--bot` (tracked), `bot2` is `--bot-b` (tracked), `cmd:` is an untracked external process. Slots are shuffled per seed. |
| `--enforce` | Enforce the 50 ms budget: late replies are dropped and the mech coasts, as on the server. By default the sim waits for every reply (deterministic) and only counts `late` (> 50 ms). |
| `--budget-ms N` | Change the budget. |
| `--validate` | Schema-check every obs we send and every action received (`data/mechfight.schema.json`). |
| `--full` | Keep simulating after every tracked bot is dead. By default the match stops there, because its place is final. |
| `--set a.b.c=v` | Override a rule in `hello.config`, e.g. `--set weapons.gun.damage=50`. |
| `--sim key=v` | Simulator switches for ambiguous rules (`config.py: SIM_DEFAULTS`, documented in ASSUMPTIONS.md). |
| `--squad key=v` | House-squad director: `early` (1), `join_per_loss` (1), `all_engage_t` (300 s), `retaliate_s` (0), `defend_range` (0), `forget_s` (8). |
| `--traces DIR` | (`run_matches.py`) Write a trace for every match. |
| `--seed N --trace F` | (`run_match.py`) Run one match and write its trace. |

Matches are deterministic for a given seed and lineup, as long as the bots are deterministic and
`--enforce` is off.

## Built-in bots (`builtin.py`)

`builtin:<tier>` with tiers `easy`, `medium`, `hard`, `expert` (house squad) and the FFA archetypes
`brawler`, `sniper`, `camper` and `idle`. They read the same observation format, with cheaper sensing
(items within 90 m without line-of-sight checks). They aim with lead and per-tier noise, strafe,
take weapons, use laser, lightning and mortar, retreat to heal, follow the zone, and steer around
obstacles with probes and jump/jet recovery. They do no global path planning.

**Squad director (practice).** House bots share vision of the entrant and never shoot each other.
Engagement is weakest first: one bot (an easy one) hunts from the start. Each house loss lets another
join (`join_per_loss`), and all engage after `all_engage_t`. Non-engaged bots loot and play the zone,
and by default **do not fight back** (`retaliate_s=0`). That default reproduces the kit example bot's
official 1.15: the sim gives 0.90 ± 0.34 over 48 seeds. With 4 s of retaliation it gives 0.22 instead.

## What is calibrated vs guessed

| Area | Status |
| --- | --- |
| Movement speeds, accelerations, gravity, jump, jet (cap then gravity, rises at 8.9 m/s), energy drain and regen | **Calibrated** against the recorded check stream (current config) |
| Held-fire cadence: first tick of a decision only, so the gun fires every 0.3 s, not 0.24 | **Calibrated** against the check stream |
| Slope limit over a 2 m baseline; wall collision radius 2.5 m regardless of cubes | **Calibrated**: 0 violations over the replay's grounded positions |
| Mortar: direct hit = `damage` only; splash and knock share one falloff; drain | **Calibrated** against the public replay's events |
| Shot origin (horizontal 3 m offset) | **Calibrated** against replay mortar shells (`docs/SIM_ISSUES.md`) |
| Zone schedule, DPS, shrink timing; crate, loot and NPC counts; roof fractions; spawn separation | **Matches** the config and the replay. The next-centre distribution is a guess: uniform in the allowed disc |
| Gun, laser and lightning damage, rear ×1.5, cube scaling, armor | From the rulebook. Replay hit amounts are consistent (×1.04 per cube, ×1.5 rear, ×0.92 armor) |
| Collision (walls slide, 1.2 m step, 50° slope, bridges and overhangs, roof-edge slide) | From the rulebook, rasterised at 0.5 m. Not checked against the server |
| Vision (near 45 m, 200 m / 120° wedge, canopy transmittance `exp(-0.35·len) < 0.25`, 5 s memory), sounds | From the rulebook. The canopy formula is a guess |
| NPC drones (wander, aggro, chase, shoot) | From the rulebook. The movement details are guessed |
| House squad behaviour and tier skills | **Guessed**. Aim noise, lead and ranges follow the replay's expert, hard and medium bots. Squad aggression is fitted to the example bot's official score |
| Upgrade offers, supply drops, weapon take and drop | From the rulebook |

## Files

`engine.py` (rules), `sense.py` (vision, sound and observations), `world.py` (map raster, collision,
LOS), `builtin.py` (built-in bots and squad director), `runner.py` (processes, lockstep, trace),
`run_match.py`, `run_matches.py`, `render.py` (HTML viewer), `calibrate.py` (replay and trace
statistics), `schema.py` (validator), `data/` (recorded `hello.config` and schema).
