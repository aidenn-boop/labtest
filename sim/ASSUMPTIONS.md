# Simulator assumptions where the rulebook is silent or ambiguous

Switches marked `--sim key` can be flipped (see `config.py: SIM_DEFAULTS`). Evidence:
**CS** = the kit's recorded check stream (current balance), **RP** = the public sample replay (older
balance, so only ratios and event structure carry over), **RB** = the rulebook wording.

## Timing and order of operations

- Decision at every even tick. Actions apply for that tick and the next. The order within a tick is: weapon
  `take` resolution, torso slew, movement, mech push-apart, weapons, projectiles, NPCs, zone damage
  and regen, cube pickup and drops, offer expiry, deaths and placement.
- Firing (`fire_first_tick_only`, **calibrated on CS**): a held trigger fires the gun, mortar and lightning, or starts a
  laser charge, only on the **first tick of a decision**, and the cooldown resets to the full value with no carry.
  In CS the house bot's held gun fired at ticks 103, 109, 115, 121… (gaps exactly 6 ticks = 0.3 s with a 0.24 s
  cooldown). The cooldown seen in observations is only ever 0, 0.09 or 0.19, never 0.14, which a second-tick shot
  would show. The replay shows every fire event on the first tick too (docs/SIM_ISSUES.md #2). So the effective
  held-fire period is `ceil(cooldown / 0.1) × 0.1` s: gun 0.3, lightning 0.3, mortar 2.6.
  Laser charging and release are per tick.
- A missed reply keeps `move/boost/aim`. After `coast_s` without a valid reply the mech idles (RB).
- `end` is sent as soon as a mech dies (`send_end_on_death`), because its place is final then. The match
  stops once every tracked bot is dead, unless `--full` is set.
- Places: everyone who dies on a tick gets `alive count before that tick`. At the time limit, survivors
  are ranked by HP with ties sharing the better place.

## Movement

- Ground: accelerate toward `move × speed` at `ground_accel`. Boost needs `|move| > 0.05` and energy.
  Charging a laser multiplies speed by `charge_move_mult`.
- Air: accelerate at `air_accel` toward `move × max(walk_speed, current horizontal speed up to boost_speed)`,
  so a running jump keeps its momentum (`air_keeps_boost_speed`). RP air speeds reach boost speed.
- Jet: `vz = min(vz + jet_accel·dt, max_rise_speed)`, then gravity. This reproduces CS's steady 8.9 m/s
  climb exactly (10 − 22·0.05).
- Collision uses prisms rasterised to 0.5 m cells (int16 cm intervals). A move is blocked if any cell
  in the body disc has a solid interval overlapping `[feet + step_up, feet + height]`. On block, slide
  along the wall normal (the normal is estimated from the blocked cells' centroid), then try per-axis moves.
- Step-up: the support under the centre is the highest top ≤ `feet + step_up`. Terrain slope is measured from the
  current position to `slope_baseline` (2 m) ahead in the move direction. If that rise exceeds tan(`max_slope_deg`),
  the move is blocked. Replay: 0 of 10,990 grounded terrain steps exceed it. A per-tick measure would wrongly block
  LiDAR spikes the server allows (up to 1.7 m/m; nav's finding in docs/SIM_ISSUES.md). Walking down snaps to the
  surface when the drop is ≤ `ground_snap` (1.2 m), otherwise you fall.
- Jet ceiling: the lowest solid base above the head within the disc stops the climb.
- Roof edges: falling past a roof top the disc overlaps lands you on it. If your centre is off the roof
  and you are not moving toward it, you slide 0.6 m per tick away from it. This is a simplified version
  of RB's edge rule.
- Mechs overlapping horizontally and vertically are pushed apart symmetrically, unless the push would go into a wall.
- Wall collision uses the **base** radius (2.5 m) even with cubes (`collision_radius_scales=false`). Replay:
  14,390 grounded positions are all legal with 2.5 m, while 0.9% would be inside walls with the cube-scaled radius.
  Hit cylinders and eye/muzzle heights still scale.
- The arena edge clamps the position to `bounds ± radius`.

## Weapons

- Shot origin: the muzzle (`pos.z + muzzle_height`, mortar `height + 0.5`) plus `radius + 0.5` m along
  the **horizontal** aim (`shot_offset_horizontal`, fitted on RP shells). If a wall is closer, the shot
  stops there: a gun bullet is wasted, a mortar shell explodes, a laser has zero length.
- Gun spread: `spread_deg` is the Gaussian σ per axis (`spread_is_sigma`, RP σ ≈ 0.58°).
- Bullets are swept spheres (`projectile.gun_radius` 0.3) against mech, NPC and crate cylinders and the
  world raster. They do not inherit the shooter's velocity (RP).
- Mortar: a direct hit deals `damage` only, with full concussion (`mortar_direct_plus_splash=false`, RP: one
  hit event of 200·cubes·armor with drain 45 and knock 16/9). Splash goes to everyone else within
  `splash_radius` of the nearest point of their cylinder, with the falloff `1 − (1 − splash_edge)·d/R`, and needs
  a clear line from the blast to their centre. Knock: horizontal `knock·f` away from the blast, `vz = max(vz, 0) + knock_up·f`,
  energy `−drain·f`. Own splash ×`self_damage_mult` and you are thrown too. Shells explode at `ttl`.
- Laser: trigger held → charging (needs cooldown 0 and not shorted). Release fires if `charge ≥ min_charge`,
  otherwise it fires as soon as that much is reached. Full charge held for `hold_s` fires on its own.
  Damage `damage·(min_frac + (1 − min_frac)·charge/charge_max)`. The beam hits the first `1 + pierce`
  targets (mechs, NPCs, crates) up to the first wall. Cook-off ×(1 + `cookoff`) applies to any mech
  holding a mortar.
- Lightning: `cone_deg` is the half-angle around the aim yaw (`lightning_cone_half`). RP zaps hit up to 6.7°
  off aim, so this is not resolved. The target is the nearest within `range` (3-D, from the muzzle to the
  target centre) with a clear line. Chain targets are the nearest unhit within `chain_range` of the last
  target with a clear line, ×`chain_falloff`^k. A short cancels the charge and blocks charging for `short_s`.
  A shot with no target still costs energy.
- The rear check uses the direction from the target to the source point: the bullet's previous position,
  the beam origin, the previous arc point, or the blast point. Rear if outside ±`front_arc_deg/2` of the target's aim yaw.
- Taking a weapon sets its cooldown to `max(equip_s, its cooldown)`. A dropped gun keeps its ammo.
  Upgrades belong to the mech.

## Health, zone, regen

- Zone damage is applied per tick outside `radius` (strict `<` inside test), and reported as one
  `damaged` event per observation (weapon `zone`, bearing and by null).
- Regen `regen_per_s` after `regen_delay` without dealing or taking damage, only inside the zone. Zone
  damage resets the timer (RB).
- Next zone centre: uniform over the disc of radius `R_cur − R_next` around the current centre
  (`zone_center_uniform_area`). This matches 7 observed RP/CS offsets (fractions 0.19–1.0). It is not
  clamped to the map.

## Loot, cubes, upgrades, drops

- Spawns: open ground (no prism, no trunk, slope < 0.5) at least `arena_margin` from the bounds and within
  `spawn_zone_fraction × start_radius` of the start centre, with a minimum separation that relaxes if needed.
  NPCs and the ground items use open ground. A quarter of crates and floor weapons go on roofs,
  away from roof edges.
- Cubes from a kill or a crate scatter within 2.5 m (`cube_scatter`). Pickup goes to the closest mech
  within `pickup_radius` horizontally and 4 m vertically.
- Max HP = `stats.hp × (1 + hp_per_cube·min(cubes, cap))` (RP: 1400→1456→1512 per cube, ×1.15 plating).
  The increase heals. Damage multiplier: `1 + damage_per_cube·min(cubes, cap)` (RP amounts confirm). Size:
  radius, height, eye and muzzle ×(1 + 0.05·cubes).
- Offers: each threshold crossed queues one offer. Choices are 3 random distinct eligible upgrades (weapon
  upgrades only for held weapons). Expiry picks choice 0. Capacitor raises current energy by the same amount.
- Supply drops: at the call time, a random open-ground point within `spread × next_radius` of the next
  centre. Lands after `fall_s`. Capture: exactly one mech within `radius` horizontally (and ≤ 13 m vertically) for
  `capture_s`. Contested freezes the clock, empty resets it, a different sole holder restarts it.

## Vision and sound

- Visible if the eye has a clear line to the centre or top of the target, and either the 3-D distance is ≤ `near_range`
  (buildings and terrain block, trees ignored) or it is ≤ `range` and within the ±`fov/2` yaw wedge (buildings,
  terrain and trunks block, and canopy blocks when `exp(−canopyDensity·foliage_len) < canopyBlockT`).
  Rays are sampled every 0.75 m. Mechs and NPCs stay listed for `memory_s` after the last sighting.
- Built-in bots get a cheaper observation: items within 90 m without line-of-sight checks, no sounds or beams.
- Sounds: emitted at shots, explosions, laser charge and fire, lightning, jets (every 0.3 s while jetting),
  steps or boosts (every `step_every` while above `step_speed` on the ground), reloads, deaths, crates,
  NPC shots and drop landings. Heard within `range[kind]` (2-D), with the bearing σ = `bearing_noise_deg × hearing`.
- Beams are visible if any path point or segment midpoint passes the vision test.

## NPC drones

- They wander to random points within `leash` of home at `speed`. They aggro on an attacker or on the nearest mech
  within `aggro_range` with a clear line while within `leash` of home, checked every 0.5 s. They chase
  to 20 m, de-aggro beyond `deaggro_range` or when more than `chase_leash` from home, then walk home.
- Shots: aimed at the centre with no lead (`npc_lead`), `spread_deg` σ, `projectile_speed`, range 100 m
  (`npc_bullet_range`), every `cooldown` while the line is clear. NPCs are not damaged by the zone.

## House squad (practice mode)

See README *Squad director*. Tier parameters are in `builtin.py: TIERS`. Everything about the squad is a
reconstruction from the track page's one-paragraph description, the replay's expert, hard and medium bots,
and the example bot's official score.
