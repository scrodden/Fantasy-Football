#!/usr/bin/env python3
"""Build the interactive cloud page (docs/) for GitHub Pages.

GitHub Pages can't run the Python backend, so the scheduled job pre-computes
every API response as a static JSON file under docs/data/ and ships the real
frontend (betting.js) in "static mode" -- it reads those files instead of
calling a server.  The result is the *full* interactive betting app (Top Edges,
All Games with click-through explanations, Strategy Tracker, Model Lab, Power
Ratings), refreshed every couple of hours, hosted free.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil

from betting import data, train, strategies, backtest, cfbd
from betting.model import Projector

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DDATA = os.path.join(DOCS, "data")


def _write(name: str, obj) -> None:
    with open(os.path.join(DDATA, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _status() -> dict:
    out = {"odds_api_configured": bool(data.odds_api_key()),
           "cfbd_configured": cfbd.enabled(), "leagues": {}}
    for lg in data.LEAGUES:
        proj = Projector(lg)
        out["leagues"][lg] = {
            "label": data.LEAGUE_LABEL[lg],
            "seeded": proj.elo.meta.get("n_games", 0) > 0,
            "games_learned": proj.elo.meta.get("n_games", 0),
            "teams_rated": len(proj.elo.teams),
            "season": proj.elo.meta.get("season"),
        }
    return out


def _rankings(lg: str) -> dict:
    proj = Projector(lg)
    return {"league": lg, "label": data.LEAGUE_LABEL[lg],
            "as_of": proj.elo.meta.get("last_game_date"),
            "teams": proj.elo.rankings()}


def _backtest(lg: str) -> dict:
    proj = Projector(lg)
    y = _dt.date.today().year
    end = y if _dt.date.today().month >= 9 else y - 1
    seasons = list(range(2021, end + 1))
    ov = {"elo": {"k": proj.elo.k, "hfa": proj.elo.hfa,
                  "points_per_elo": proj.elo.points_per_elo,
                  "revert": proj.elo.revert, "score_sd": proj.elo.score_sd},
          "pace": {"alpha": proj.pace.alpha, "home_pts": proj.pace.home_pts,
                   "form_w": proj.pace.form_w},
          "signals": proj.elo.meta.get("signals") or {}}
    r = backtest.run(lg, seasons, overrides=ov)
    r["signals"] = proj.elo.meta.get("signals") or {}
    r["calibrated_at"] = proj.elo.meta.get("calibrated_at")
    return r


def main() -> None:
    os.makedirs(DDATA, exist_ok=True)
    # Ship the real frontend + styles, and cache-bust them by content hash so a
    # browser always loads the current version after a deploy (no hard refresh).
    import hashlib
    ver = {}
    for fn in ("betting.js", "style.css"):
        src = os.path.join(ROOT, "web", fn)
        shutil.copy(src, os.path.join(DOCS, fn))
        with open(src, "rb") as fh:
            ver[fn] = hashlib.md5(fh.read()).hexdigest()[:8]
    html = (INDEX_HTML.replace("style.css", "style.css?v=" + ver["style.css"])
                      .replace("betting.js", "betting.js?v=" + ver["betting.js"]))
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)

    _write("status.json", _status())
    for lg in data.LEAGUES:
        board = train.build_board(lg)
        _write(f"board_{lg}.json", board)
        _write(f"strategies_{lg}.json", strategies.report(lg))
        _write(f"accuracy_{lg}.json", train.accuracy(lg))
        _write(f"rankings_{lg}.json", _rankings(lg))
        explain = {}
        for g in board["games"]:
            try:
                explain[str(g["id"])] = train.explain_game(lg, g["id"])
            except Exception as exc:
                print(f"[cloud] explain {lg} {g['id']}: {exc}")
        _write(f"explain_{lg}.json", explain)
        try:
            _write(f"backtest_{lg}.json", _backtest(lg))
        except Exception as exc:
            print(f"[cloud] backtest {lg}: {exc}")
    print("cloud snapshot written to docs/")


INDEX_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Football Betting Model</title>
<link rel="stylesheet" href="style.css">
<style>
body{background:var(--bg);color:var(--text);font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0}
.cloudbar{display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap;padding:1rem 1.2rem .4rem}
.cloudbar h1{margin:0;font-size:1.3rem}
.cloudbar .muted{font-size:.82rem}
#betting-panel{padding:0 1.2rem 3rem}
.cloud-note{margin:.2rem 1.2rem 1rem;color:var(--muted);font-size:.82rem}
</style></head><body>
<div class="cloudbar"><span style="font-size:1.5rem">🏈</span><h1>Football Betting Model</h1>
<span class="muted">auto-updating cloud view</span></div>
<div class="cloud-note">Projections, edges, and weekly strategy results — updated automatically every couple of hours. Click any game for the model's full reasoning.</div>
<div id="betting-panel"><p class="muted" style="padding:1.2rem">Loading…</p></div>
<script>window.BETTING_STATIC = true;</script>
<script src="betting.js"></script>
<script>document.addEventListener("DOMContentLoaded",function(){ if(window.Betting) window.Betting.show(); });</script>
</body></html>"""


if __name__ == "__main__":
    main()
