#!/usr/bin/env python3
"""Summary of an `acc-cored run --accswitch-shadow FILE` log: what the event-driven tick would have
done next to launchd's ticks.

    python3 -I tests/acc_cored/accswitch_shadow_summary.py FILE

Each line of FILE is one launchd tick, with whether acc-cored would have run it (`run`) and what
changed since the last one: `hard` (something the switcher acts on) and `soft` (only panel-side
readings: other accounts' usage, profiles). A skipped tick with a hard change is a miss; a run with
neither is a wasted run.
"""

import collections
import json
import sys

with open(sys.argv[1]) as f:
    rows = [json.loads(line) for line in f]
runs = [r for r in rows if r["run"]]
skips = [r for r in rows if not r["run"]]
viol = [r for r in skips if r["hard"]]
softskip = [r for r in skips if r["soft"] and not r["hard"]]
wasted = [r for r in runs if not r["hard"] and not r["soft"]]
span = (rows[-1]["at"] - rows[0]["at"]) / 3600 if rows else 0
print(
    f"ticks {len(rows)} over {span:.1f} h: would run {len(runs)}, would skip {len(skips)}"
)
print(f"skipped ticks that changed something the switcher acts on: {len(viol)}")
for r in viol[:10]:
    print("   ", r["at"], r["hard"], r["due"].get("why") if r["due"] else None)
print(
    f"skipped ticks with only panel-side changes (other accounts' readings, profiles): {len(softskip)}"
)
print(f"runs that changed nothing: {len(wasted)}")
print(
    "run reasons",
    collections.Counter(
        x.split(":")[0] for r in runs for x in r["reasons"]
    ).most_common(),
)
sys.exit(1 if viol else 0)
