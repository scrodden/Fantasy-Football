#!/usr/bin/env python3
"""Headless scheduled maintenance for the betting model.

Runs on GitHub Actions every couple of hours (see .github/workflows/betting.yml)
so you never have to keep the app open:

  * lock each game's bets ~24h before kickoff (freezing the projection + line),
  * settle games that have finished and grade the strategy bets,
  * fold new results into the ratings (and re-sync EPA).

This is exactly the work the in-app background autopilot does -- just triggered
by the cloud on a schedule instead of by a running app. State is saved to the
JSON files under data/, which the workflow commits back to the repo.
"""

from __future__ import annotations

import sys
import traceback

from betting import data, train, strategies


def main() -> int:
    ok = True
    for lg in data.LEAGUES:
        try:
            upd = train.update_from_results(lg)
            m = strategies.maintain(lg)
            print(f"[{lg}] learned {upd.get('new_finals_learned', 0)} new finals · "
                  f"locked {m['lock'].get('locked', 0)} · "
                  f"settled {m['settle'].get('settled', 0)} strategy bets "
                  f"(EPA: {upd.get('epa')})")
        except Exception:
            ok = False
            print(f"[{lg}] ERROR:\n{traceback.format_exc()}", file=sys.stderr)
    print("done." if ok else "done with errors.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
