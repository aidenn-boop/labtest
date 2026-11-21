# Builders - Mech Royale: track rules (definitive)

Source: the Mech Royale track page (joinbuilders.org/buildathon/tracks/builders), as pasted by the team on
2026-11-21. The full bot protocol is in `protocol.md` (identical to the kit's copy); the JSON Schema is
`mechfight.schema.json`.

## Bot interface and limits
- Speak mechfight/1 JSON lines on stdin and stdout (protocol.md).
- 50 ms to answer each observation. Late answers are skipped; the mech holds its last move and aim without
  firing for 1 second, then stands still. Answer times vary by a few ms between runs and machines, so a bot
  that answers near the limit can score differently on a rerun.
- Container: 1 CPU, 512 MB memory, read-only map at /arena.
- Runtime: no network, no root (uid 65534; install packages system-wide), no disk writes outside /tmp (64 MB).
- Builds: linux/amd64, network on, at most 10 minutes; pin versions because finalists are rebuilt.
- Repository at the submitted commit: at most 20 MB, Dockerfile at the top.

## Scoring
- Metric: placement points, higher is better. Baseline to beat: 1.15 (the kit's Python example bot).
- Formula: mean of (16 - place) over every match in a run. 15 for a win, 0 for dying first.
  Same-tick deaths share a place.
- Ties: equal scores rank the earlier submission first.
- Scorer version: builders-3.

## Rules
- Practice plays 48 matches against 15 house bots (4 easy, 4 medium, 4 hard, 3 expert) acting as one squad:
  they share vision, never fight each other, and hunt you weakest first (one early, more as you thin them
  out, all of them late).
- Practice seeds are private and fixed all event. Your bot never sees the seed; your slot and spawn vary
  each match.
- Every entry must build and pass check/check.py under the runtime limits. Failed builds or checks are
  rejected with the reason and never affect your best score.
- A bot that crashes or stops answering stays in any match as a sitting target and is scored, not rejected.
- A team may submit 4 times an hour; a submission usually takes a few minutes to score. The leaderboard ranks
  each team's best practice score alongside the starter bot.
- The repository must be public when you submit. Once a submission is scored you may make the repository
  private: the organizers keep a copy of every scored commit, and the final uses that copy. An entry that
  cannot be built for the final scores nothing.
- A rejected entry still uses one of the hour's submissions.

## The final
- At the cutoff, your chosen submission (your best unless you pick another) locks with its practice score.
- The top 32 teams enter a qualifying round: each plays 16 matches (16-mech free-for-alls) against other
  qualifiers on new seeds.
- The top 16 by qualifying mean (16 - place) advance and are announced without scores.
- All 16 finalists play a 64-match series (16-mech free-for-alls) together on new seeds; the last 4 play live
  at the venue and count the same. The series mean alone decides finalist rankings; earlier submissions break
  all ties.
- Organizers may adjust match counts before the final starts, never after.
- With fewer than 32 teams, everyone plays qualifying; with 16 or fewer, everyone plays the series.
  Expert house bots fill empty slots.
- If a qualifying entry fails to fetch or build, it sits out and the next practice-board team takes its seat.
- Non-finalists rank below finalists (eliminated qualifiers by qualifying score, then others in
  practice-board order, shown as negative scores). Standings stay hidden until the last match ends.
- Cutoff: Sun 2026-11-22 9:30 PM GMT+5:30 (16:00 UTC).

## Facts from protocol.md that the experiments rely on
- Matches are lockstep: the server sends all observations, waits for every bot (up to 50 ms), then simulates
  2 ticks. Answering fast never hurts. Identical code gives identical practice scores.
- hello.seed is per slot; seed your RNG with it for reproducibility.
- Hit events report mech, NPC and crate targets (crate hits since 2026-11-16).
- Trees do not stop shots; buildings, bridges and terrain do. Shots leave 3.0 m out along the actual aim.
