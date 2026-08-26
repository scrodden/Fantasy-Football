"""Seeding, the weekly learn-from-results loop, board building, and accuracy.

Public entry points (all used by the app's ``/api/betting/*`` routes):

  * ``seed(league, years)``        -- build ratings from scratch off past seasons
  * ``build_board(league)``        -- project this week's slate + betting edges
  * ``update_from_results(league)``-- ingest new finals, sharpen the model, grade
                                      our past predictions
  * ``accuracy(league)``           -- the model's honest track record so far

Every projection is written to a predictions log *before* the game, so ATS
records, calibration (Brier), and error stats are measured on genuinely
out-of-sample forecasts -- the same standard a real bettor is held to.
"""

from __future__ import annotations

import datetime as _dt
import json
import os

from betting import data, edges
from betting.model import Projector
from betting.elo import DATA_DIR

# Rough season windows (used only to chunk historical fetches for seeding).
_SEASON_WINDOW = {
    "nfl": ((9, 1), (2, 15)),     # Sep 1 -> Feb 15 (next year)
    "cfb": ((8, 20), (1, 20)),    # Aug 20 -> Jan 20 (next year)
}


def _pred_path(league: str) -> str:
    return os.path.join(DATA_DIR, f"predictions_{league}.json")


def _processed_path(league: str) -> str:
    return os.path.join(DATA_DIR, f"processed_{league}.json")


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def _processed_ids(league: str) -> set:
    return set(_load_json(_processed_path(league), []))


def _save_processed(league: str, ids: set):
    _save_json(_processed_path(league), sorted(ids))


# ---------------------------------------------------------------------------
# Seeding from history
# ---------------------------------------------------------------------------
def seed(league: str, years: list[int], reset: bool = True) -> dict:
    """Rebuild the model from completed games across ``years`` (chronological).

    Applies between-season mean reversion so early-season ratings aren't stale.
    Returns a small summary.  This is the cold-start; after it, weekly
    ``update_from_results`` keeps the model current.
    """
    # Start clean so re-seeding is idempotent.
    for p in (_pred_path(league), _processed_path(league)):
        if reset and os.path.exists(p):
            os.remove(p)
    for f in (f"elo_{league}.json", f"pace_{league}.json"):
        fp = os.path.join(DATA_DIR, f)
        if reset and os.path.exists(fp):
            os.remove(fp)

    proj = Projector(league)
    (sm, sd), (em, ed) = _SEASON_WINDOW[league]
    processed = set()
    total = 0
    for i, yr in enumerate(sorted(years)):
        if i > 0:
            proj.new_season(yr)
        start = _dt.date(yr, sm, sd)
        end = _dt.date(yr + 1, em, ed)
        end = min(end, _dt.date.today())
        games = data.historical_games(league, start, end)
        for g in games:
            proj.process_game(g)
            processed.add(g["id"])
            total += 1
    proj.save()
    _save_processed(league, processed)
    _board_cache_clear(league)
    return {"league": league, "years": years, "games_processed": total,
            "teams_rated": len(proj.elo.teams)}


# ---------------------------------------------------------------------------
# Choosing the slate to project
# ---------------------------------------------------------------------------
def _relevant_board(league: str) -> dict:
    """The upcoming slate worth betting.

    ESPN's 'current' board is preseason for the NFL right now; fall back to the
    regular-season week that actually carries odds.  For college the current
    board is already the live regular-season slate.
    """
    board = data.current_board(league)
    has_odds = any(g["espn_odds"] for g in board["games"])
    if league == "nfl" and (board.get("seasontype") == 1 or not has_odds):
        # Walk regular-season weeks until we find one with games + odds.
        for wk in range(1, 19):
            b = data.scoreboard(league, seasontype=2, week=wk)
            upcoming = [g for g in b["games"] if not g["completed"]]
            if upcoming and any(g["espn_odds"] for g in b["games"]):
                return b
    return board


# ---------------------------------------------------------------------------
# Building the board (projections + edges) and logging predictions
# ---------------------------------------------------------------------------
# Short in-process cache of the built board so repeated tab clicks / league
# toggles are instant. Busted by seed() and update_from_results().
_BOARD_CACHE: dict = {}
_BOARD_TTL = 180  # seconds


def _board_cache_clear(league: str | None = None):
    if league is None:
        _BOARD_CACHE.clear()
    else:
        _BOARD_CACHE.pop(league, None)


def _ensure_season(proj: Projector, year) -> bool:
    """Apply between-season mean reversion once when a new season begins.

    Idempotent: keyed on ``elo.meta['season']`` so repeated board builds don't
    keep reverting.  Returns True if a rollover happened."""
    if not year:
        return False
    last = proj.elo.meta.get("season")
    if last is not None and year > last:
        # CFB: seed the new season from CFBD preseason priors (returning
        # production + recruiting + SP+) instead of a flat regression.
        applied_priors = False
        if proj.league == "cfb":
            try:
                from betting import cfbd
                if cfbd.enabled():
                    cfbd.apply_season_priors(proj, year)
                    applied_priors = True
            except Exception as exc:
                print(f"[cfb priors] {exc}")
        if not applied_priors:
            proj.new_season(year)
        proj.save()
        return True
    if last is None:
        proj.elo.meta["season"] = year
    return False


def build_board(league: str, use_cache: bool = True) -> dict:
    if use_cache:
        hit = _BOARD_CACHE.get(league)
        if hit and (_dt.datetime.now().timestamp() - hit[0]) < _BOARD_TTL:
            return hit[1]
    proj = Projector(league)
    board = _relevant_board(league)
    _ensure_season(proj, board.get("year"))
    board = data.attach_multibook(board, league)   # layer Odds API if configured
    if league == "cfb":                             # layer CFBD's multi-book lines
        try:
            from betting import cfbd
            if cfbd.enabled():
                prov = cfbd.all_provider_lines(board.get("year"), board.get("week"))
                for g in board["games"]:
                    extra = prov.get(str(g["id"]))
                    if not extra:
                        continue
                    have = {b.get("book") for b in g["books"]}
                    for b in extra:
                        if b["book"] not in have and b.get("home_spread") is not None:
                            g["books"].append(b)
        except Exception as exc:
            print(f"[cfbd lines] {exc}")
    if proj.use_weather:                            # live game-time forecasts (NFL)
        try:
            from betting import weather as _weather
            _weather.attach_forecasts(board, league)
        except Exception as exc:
            print(f"[weather] {exc}")
    preds = _load_json(_pred_path(league), {})

    games_out = []
    all_edges = []
    for g in board["games"]:
        # Annotate games-played so edge confidence can temper on thin samples.
        g["home"]["_gp"] = (proj.elo.teams.get(str(g["home"]["id"]), {}) or {}).get("gp", 0)
        g["away"]["_gp"] = (proj.elo.teams.get(str(g["away"]["id"]), {}) or {}).get("gp", 0)
        p = proj.project(g)
        p["_spread_sd"] = proj.elo.score_sd
        game_edges = edges.evaluate(g, p, league) if not g["completed"] else []
        for e in game_edges:
            e["game"] = f"{g['away']['abbr']}@{g['home']['abbr']}"
            e["away_full"] = g["away"].get("full") or g["away"]["abbr"]
            e["home_full"] = g["home"].get("full") or g["home"]["abbr"]
            e["away_abbr"] = g["away"]["abbr"]
            e["home_abbr"] = g["home"]["abbr"]
            e["game_id"] = g["id"]
            e["kickoff"] = g["date"]
            e["neutral"] = g["neutral"]
        all_edges.extend(game_edges)

        row = {
            "id": g["id"], "date": g["date"], "name": g["name"],
            "neutral": g["neutral"], "state": g["state"],
            "completed": g["completed"], "status": g["status_detail"],
            "home": {k: g["home"].get(k) for k in ("id", "abbr", "name", "full", "record", "rank", "score", "logo")},
            "away": {k: g["away"].get(k) for k in ("id", "abbr", "name", "full", "record", "rank", "score", "logo")},
            "market": g["espn_odds"],
            "books": g["books"],
            "weather": g.get("weather"),
            "projection": p,
            "edges": game_edges,
        }
        games_out.append(row)

        # Log the pre-game prediction once (don't overwrite after kickoff).
        if not g["completed"] and g["id"] not in preds:
            preds[g["id"]] = {
                "id": g["id"], "league": league, "date": g["date"],
                "home": g["home"]["abbr"], "away": g["away"]["abbr"],
                "proj_margin": p["proj_margin"], "proj_total": p["proj_total"],
                "home_win_prob": p["home_win_prob"],
                "market": {"home_spread": (g["espn_odds"] or {}).get("home_spread"),
                           "total": (g["espn_odds"] or {}).get("total"),
                           "home_ml": (g["espn_odds"] or {}).get("home_ml"),
                           "away_ml": (g["espn_odds"] or {}).get("away_ml")},
                "bets": [{"market": e["market"], "side": e["side"],
                          "pick": e["pick"], "line": e.get("line"),
                          "price": e.get("price"), "edge": e.get("edge")}
                         for e in game_edges],
                "settled": False,
            }
    _save_json(_pred_path(league), preds)

    all_edges.sort(key=lambda e: e["ev"], reverse=True)
    result = {
        "league": league, "league_label": data.LEAGUE_LABEL[league],
        "year": board.get("year"), "week": board.get("week"),
        "seasontype": board.get("seasontype"),
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "multibook": bool(data.odds_api_key()) or any(len(g["books"]) > 1 for g in games_out),
        "n_games": len(games_out),
        "top_edges": all_edges[:25],
        "games": games_out,
        "model": {"teams_rated": len(proj.elo.teams),
                  "n_games_learned": proj.elo.meta.get("n_games", 0)},
    }
    _BOARD_CACHE[league] = (_dt.datetime.now().timestamp(), result)
    return result


def explain_game(league: str, game_id: str) -> dict:
    """Full model reasoning for one game (for the click-through detail view)."""
    board = build_board(league)               # cached
    row = next((g for g in board["games"] if str(g["id"]) == str(game_id)), None)
    if not row:
        return {"error": "game not found on the current board"}
    game = {
        "id": row["id"], "date": row["date"], "neutral": row.get("neutral", False),
        "home": {"id": row["home"].get("id"), "abbr": row["home"]["abbr"],
                 "name": row["home"].get("name"), "full": row["home"].get("full")},
        "away": {"id": row["away"].get("id"), "abbr": row["away"]["abbr"],
                 "name": row["away"].get("name"), "full": row["away"].get("full")},
        "espn_odds": row.get("market"),
        "books": row.get("books") or [],
        "weather": row.get("weather") or {},
    }
    proj = Projector(league)
    exp = proj.explain(game)
    exp["home"] = row["home"]
    exp["away"] = row["away"]
    exp["date"] = row["date"]
    exp["completed"] = row.get("completed", False)
    exp["status"] = row.get("status")
    return exp


# ---------------------------------------------------------------------------
# Weekly learning: ingest new finals, update model, grade predictions
# ---------------------------------------------------------------------------
def _grade_prediction(rec: dict, home_score: int, away_score: int) -> dict:
    actual_margin = home_score - away_score
    home_won = 1.0 if actual_margin > 0 else (0.0 if actual_margin < 0 else 0.5)
    su_correct = ((rec["proj_margin"] > 0) == (actual_margin > 0)) if actual_margin != 0 else None
    brier = (rec["home_win_prob"] - home_won) ** 2
    m = rec.get("market") or {}
    graded_bets = []
    for b in rec.get("bets", []):
        result = _grade_bet(b, home_score, away_score, m)
        graded_bets.append({**b, "result": result})
    rec.update({
        "settled": True, "home_score": home_score, "away_score": away_score,
        "actual_margin": actual_margin, "su_correct": su_correct,
        "brier": round(brier, 4),
        "margin_err": round(abs(rec["proj_margin"] - actual_margin), 1),
        "total_err": (round(abs(rec["proj_total"] - (home_score + away_score)), 1)
                      if rec.get("proj_total") is not None else None),
        "bets": graded_bets,
    })
    return rec


def _grade_bet(b: dict, hs: int, as_: int, market: dict) -> str:
    margin = hs - as_
    total = hs + as_
    line = b.get("line")
    if b["market"] == "spread":
        # line is the number for the bet side; reconstruct cover.
        if b["side"] == "home":
            diff = margin + line
        else:
            diff = -margin + line
        return "push" if abs(diff) < 1e-9 else ("win" if diff > 0 else "loss")
    if b["market"] == "total":
        if line is None:
            return "void"
        if abs(total - line) < 1e-9:
            return "push"
        return "win" if ((total > line) == (b["side"] == "over")) else "loss"
    if b["market"] == "moneyline":
        if margin == 0:
            return "push"
        return "win" if ((margin > 0) == (b["side"] == "home")) else "loss"
    return "void"


def update_from_results(league: str, since_days: int = 10) -> dict:
    """Fetch recent finals, update the model with any not-yet-seen games, and
    settle their logged predictions.  Safe to run repeatedly."""
    proj = Projector(league)
    today = _dt.date.today()
    # Football "season year" is the fall's calendar year (Jan finals belong to
    # the prior season).  Roll ratings over once if a new season has begun.
    _ensure_season(proj, today.year if today.month >= 8 else today.year - 1)
    processed = _processed_ids(league)
    preds = _load_json(_pred_path(league), {})

    start = today - _dt.timedelta(days=since_days)
    finals = data.historical_games(league, start, today)

    newly = 0
    for g in finals:
        gid = g["id"]
        # Settle the prediction log regardless of whether we trained on it.
        if gid in preds and not preds[gid].get("settled"):
            preds[gid] = _grade_prediction(preds[gid], g["home"]["score"], g["away"]["score"])
        if gid in processed:
            continue
        proj.process_game(g)
        processed.add(gid)
        newly += 1

    proj.save()
    _save_processed(league, processed)
    _save_json(_pred_path(league), preds)
    _board_cache_clear(league)
    # Rebuild NFL EPA ratings from the latest play-by-play.
    epa_info = None
    try:
        from betting import epa as _epa
        if league == "nfl":
            epa_info = _epa.sync()
            _epa.sync_qb()
        else:
            from betting import cfbd
            if cfbd.enabled():
                epa_info = _epa.sync_cfb()
    except Exception as exc:
        print(f"[epa sync] {exc}")
    return {"league": league, "new_finals_learned": newly,
            "total_learned": proj.elo.meta.get("n_games", 0),
            "epa": epa_info,
            "predictions_settled": sum(1 for r in preds.values() if r.get("settled"))}


# ---------------------------------------------------------------------------
# Accuracy report
# ---------------------------------------------------------------------------
def accuracy(league: str) -> dict:
    preds = _load_json(_pred_path(league), {})
    settled = [r for r in preds.values() if r.get("settled")]
    n = len(settled)
    if not n:
        return {"league": league, "n": 0}

    su = [r for r in settled if r.get("su_correct") is not None]
    su_pct = 100.0 * sum(1 for r in su if r["su_correct"]) / len(su) if su else None
    brier = sum(r["brier"] for r in settled) / n
    margin_mae = sum(r["margin_err"] for r in settled) / n
    tot = [r for r in settled if r.get("total_err") is not None]
    total_mae = (sum(r["total_err"] for r in tot) / len(tot)) if tot else None

    # Bet track record by market.
    by_market: dict = {}
    for r in settled:
        for b in r.get("bets", []):
            m = by_market.setdefault(b["market"], {"win": 0, "loss": 0, "push": 0, "units": 0.0})
            res = b.get("result")
            if res in ("win", "loss", "push"):
                m[res] += 1
                if res == "win":
                    price = b.get("price") or -110
                    m["units"] += (edges.american_to_decimal(price) - 1)
                elif res == "loss":
                    m["units"] -= 1
    for m in by_market.values():
        decided = m["win"] + m["loss"]
        m["ats_pct"] = round(100.0 * m["win"] / decided, 1) if decided else None
        m["units"] = round(m["units"], 2)
        m["roi_pct"] = round(100.0 * m["units"] / decided, 1) if decided else None

    return {
        "league": league, "n": n,
        "su_pct": round(su_pct, 1) if su_pct is not None else None,
        "brier": round(brier, 4),
        "margin_mae": round(margin_mae, 2),
        "total_mae": round(total_mae, 2) if total_mae is not None else None,
        "by_market": by_market,
    }
