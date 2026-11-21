# Calibration: sim vs reality

There are two sources of ground truth:

- **CS**: the kit's recorded check stream (`check/stream.jsonl.gz`). It holds 300 observations at 10 Hz of one mech
  (slot 3, a house bot in a real match) under the **current** balance (the same `hello.config` the sim uses).
- **RP**: the public sample replay `mechanics-v2-16.jsonl.gz`, with 16 house bots and 2,098 frames at 10 Hz. It was
  recorded under an **older balance**: walk 9 m/s, boost 15, bullets 140 m/s, boost drain about 30/s, jet drain
  about 20/s, regen 20/s. So RP is used for structure, ratios, geometry and behaviour, not for absolute speeds.

Reproduce: `sim/py sim/calibrate.py <trace> --world` (works on RP and on any sim trace).

## Physics residuals

| Quantity | Reality | Sim | Residual / note |
| --- | --- | --- | --- |
| Walk speed (ground) | CS 11.69 m/s | 11.69–11.70 | < 0.01 |
| Boost speed | CS 19.50 | 19.45–19.55 | < 0.05 |
| Boost drain | CS 17.0 /s (6.8 per 0.4 s) | 17.0 | 0 |
| Jet climb speed | CS 8.9 m/s (steady) | 8.9 | 0. Requires *jet + cap, then gravity* (10 − 22·0.05). The naive order gives 10 |
| Jet drain | CS 15.0 /s | 15.0 | 0 |
| Energy regen | CS 18 /s after 0.6 s | 18 | 0 |
| Free-fall accel | CS −22 (vz −13.1 → −21.9 over 0.4 s); RP −21…−23 | −22 | 0 |
| HP regen | CS 45 /s (545 → 556.25 in 0.25 s of regen); HP is fractional | 45 | 0 (obs HP has 2 decimals, as in CS) |
| Gun bullet speed | config 252 (RP 140, old balance) | 252.0 | 0 vs config |
| Mortar gravity | RP −22 | −22 | 0 |
| Mortar shell origin | RP: horizontal 3 m offset (residual < 0.1 m over 3.7 s of flight) | horizontal offset | from teammate analysis (docs/SIM_ISSUES.md #1) |
| Surface height under a standing mech | RP feet z | `support(x, y)` | p1–p99 within **0.01 m** over 14,390 grounded samples (terrain + roof tops are exact) |
| Wall collision legality | RP grounded positions | 0 / 14,390 positions in a wall with the base radius 2.5 m | With the cube-scaled radius 0.9% would be "illegal", so collision radius does not scale (sim default) |
| Mortar direct hit | RP: one hit of 200 × cubes × armor, drain 45, knock 16 / 9 (f = 1) | same | The earlier "direct + splash" guess was wrong and is fixed |
| Mortar splash | RP: damage / 160 = drain / 45 = knock / 16 = the same falloff f | same | 0 (e.g. 67.82 = 160 × 0.4076 × 1.04) |
| Cube scaling | RP: gun hits of 45, 46.8, 48.6, 50.4; max HP 1400 → 1456 → 1512; plating ×1.15 | same formula | 0 |
| Rear multiplier | RP and CS: 45 → 67.5 | ×1.5 | 0 |
| Armor | RP: 200 × 1.04 × 0.92 = 191.36 | same | 0 |
| Laser full charge | CS: 315 per hit | 315 | 0 |
| Lightning target offset from aim | RP: max 6.7° (47 zaps, all single-target, ≤ 42.6 m) | half-angle 30° | not resolved (see ASSUMPTIONS) |
| Observation format | CS key set, types, rounding | `sim/py /tmp/obsdiff.py`-style diff: no missing or extra keys; integral numbers serialised as ints like the JS server | the beams/drops shapes come from the schema (not present in CS) |

| Held-fire cadence | CS: inferred gun fire ticks 103, 109, 115, 121 …, gaps exactly 6 ticks (0.3 s) at a 0.24 s cooldown; observed cooldowns only 0 / 0.09 / 0.19 | first tick of each decision only, no carry: 6-tick gaps | 0 (the earlier accumulate model gave 4–5 tick gaps, which is 25% more gun DPS than the server) |
| Terrain slope limit | RP: 10,990 grounded terrain steps, max rise over a 2 m look-ahead 1.12 m/m (< tan 50° = 1.19). Per-tick rises reach 1.7 on LiDAR spikes | 2 m baseline | 0 violations (the per-tick rule blocked 0.04% of real steps and could pin bots on spikes) |

## World layout

| Quantity | Reality | Sim |
| --- | --- | --- |
| Crates / floor weapons / NPCs | RP 36 / 40 / 12 | 36 / 40 / 12 |
| Crates on roofs / weapons on roofs | RP 9 / 10 (25%) | 9 / 10 |
| Spawn min separation | RP 140 m (config 130) | ≥ 130 (relaxes if needed) |
| Spawn max distance from start centre | RP 442 (limit 0.8 × 560 = 448) | ≤ 448 |
| Zone schedule (radius at 0, 60, 110, 155, 195 s) | RP 560, 560, 380, 380, 240 | 560, 560, 380, 380, 240 |
| Next-centre offset as a fraction of the allowed (R − r) | RP 0.51, 0.54, 0.66, 0.99, 0.44, 0.19; CS 0.64 | uniform over the disc (mean 0.67) |
| Supply drop distance from the target centre / next radius | RP 0.38, 0.32, 0.17 | uniform within 0.7 R (`drops.spread`) |

## House-bot behaviour (from RP and CS)

These numbers come from RP (`calibrate.py` "behaviour by bot type") and drive `builtin.py: TIERS`.

| RP bot | Gun hit rate | Fire distance p10/50/90 (m) | Aim error at fire, median | Airborne fraction | Shots fired airborne | Jet hops per min | First hit on a mech |
| --- | --- | --- | --- | --- | --- | --- | --- |
| expert | 0.68 | 32 / 70 / 273 | 2.4° | 0.26 | 0.41 | 5.6 | 17 s |
| hard | 0.49 | 52 / 100 / 183 | 2.3° | 0.23 | 0.28 | 5.3 | 11 s |
| medium | 0.80 | 38 / 53 / 256 | 2.4° | 0.21 | 0.33 | 3.5 | 33 s |
| brawler | 0.54 | 56 / 107 / 172 | 2.1° | 0.26 | 0.18 | 7.0 | 91 s |
| sniper | 0.77 | 39 / 89 / 183 | 1.8° | 0.21 | 0.24 | 6.7 | 12 s |

The aim error includes the lead angle (bullets at 140 m/s, so lead is about 2–4° at 100 m). The hit rate counts
crate and NPC hits too.

Behaviours seen:

- **Jet hops.** About 6 per minute, each about 3 s airborne, rising about 14 m (many episodes exactly 14.0 m), and
  18–53% of all shots are fired mid-hop. These are a scripted dodge, not navigation: only 2–19% of hops land on a roof.
- **Looting first.** Most bots take a second weapon within 2–10 s of spawn (`first_weapon_t`), and the laser is common.
- **Kiting and jinking** (CS, current balance). In a gun-vs-laser duel at 140–150 m the house bot holds range by
  backing off: its velocity is about 160–180° from the enemy bearing, with lateral jinks. Its movement direction
  changes by more than 60° every **0.6 s** (median) while an enemy is visible, and every 3 s otherwise. Aim stays
  within about 2° of the enemy bearing, with occasional ~20° excursions. It reloads only at an empty magazine and
  keeps moving while reloading. It boosts in bursts.
- **Engagement ranges** are mostly 50–150 m with the gun. Mortar shots go out at 50–110 m.
- **Zone play.** In RP the 2nd-place brawler died to the zone at 209 s, 1-on-1 with the winner, so house bots do
  not always respect the zone late.

Sim built-in tiers measured the same way, in an all-built-in FFA (seed 1, current balance, bullets at 252 m/s):

| Sim tier | Gun hit rate | Fire distance p50 | Aim error p50 | Airborne fraction | Shots airborne | Hops per min |
| --- | --- | --- | --- | --- | --- | --- |
| expert | 0.62 | 140 | 1.8° | 0.30 | 0.34 | 5.4 |
| hard | 0.52 | 100 | 1.1° | 0.36 | 0.40 | 5.0 |
| medium | 0.57 | 91 | 1.5° | 0.48 | 0.50 | 5.8 |
| easy | 0.43 | 69 | 1.2° | 0.29 | 0.31 | 6.7 |

## Practice score proxy

The only official squad-mode number we have: **kit example bot = 1.15** (official practice, 48 matches).

| Squad setting | Example bot in the sim (mean of 16 − place) |
| --- | --- |
| Non-engaged house bots retaliate for 4 s after being hit | 0.22 ± 0.18 (n = 32) |
| **No retaliation (default)** | **1.00 ± 0.34 (n = 32)**; after the kiting/jink tier retune **0.92 ± 0.31 (n = 48)**: deaths 31× easy (the early engager), 6× medium, 3× hard, 2× expert, 5× NPC, 1× zone; a cluster at 300 s when all engage |
| Same, after first-tick-only firing + slope fix (current default) | **0.90 ± 0.34 (n = 48)**: killers 27× easy, 6× expert, 5× medium, 4× hard, 6× NPC/zone |

So in the sim the example bot dies mostly to the one or two engaged bots. Who killed it and when is in `--out`.

## docs/SIM_ISSUES.md items and their status

- Combat #1 (shot origin horizontal only): fixed (`shot_offset_horizontal`, default on).
- Combat #2 (fire tick parity): resolved from CS. A held trigger fires on the first tick of a decision only (`fire_first_tick_only`).
- Combat #3 (replay balance differs): noted. The sim reads the live config, and RP is used only for ratios.
- Nav #1 (slope rule too strict): fixed. The slope is measured over a 2 m baseline (`slope_baseline`).
- Nav #2 (radius 2.5 m, jet 8.9, support height): agrees. Collision radius no longer scales with cubes.
- "Built-in bots never fire mortars": they did, but rarely. The alignment gate is loosened, and a mortar carrier now
  fires about every 2.6–3 s at 25–220 m (7 shells in 20 s in a duel). Mortar loot is random, so in a given match
  only some bots carry one.

## Known fidelity gaps

- House AI internals: target choice, how engagement escalates, whether non-engaged bots defend themselves, and
  their pathing are all guessed. The built-in bots steer locally and jet over obstacles; they have no global
  path planner, so they may get stuck behind large buildings more than real ones do.
- Collision is a 0.5 m raster, not the server's exact polygons, so very narrow gaps may differ.
  Roof-edge sliding is simplified.
- Canopy vision rule (`exp(−0.35 · foliage length) < 0.25`) and sound occlusion (none) are guesses.
- Lightning cone width and chain target rules are not confirmed (the replay shows no chains).
- Fire-tick parity (docs/SIM_ISSUES.md #2) is open.
- NPC movement and aggro details beyond the rulebook text are guessed.
