# labtest

Test infrastructure for the chinatown Mech Royale bot: runs simulator experiments on GitHub Actions.
Not a submission.

- `sim/`: simulator forked from github.com/JuanQuenga/buildathon-mech-royale (MIT, see `sim/LICENSE.JuanQuenga`).
- `arena/`: the Mech Royale kit map.
- `bot/`: our bot (Go).
- `rivals/giddygiddyboy/`: giddygiddyboy's MIT bot at public commit 82d222f, used as an opponent (see its LICENSE).
- `exp/`: experiment scripts; `ci/build.sh` builds the bots; `.github/workflows/screen.yml` runs shards.

Push `jobs/run.json` to start a run; each shard commits its results to the `results` branch.
