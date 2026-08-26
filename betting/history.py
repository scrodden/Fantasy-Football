"""Historical games + closing lines for backtesting and calibration.

The live ESPN feed strips odds once a game is final, so to grade the model
against *real* closing lines over past seasons we need archival data:

  * **NFL** -- nflverse ``games.csv`` (Lee Sharpe's dataset): every game back to
    1999 with the closing spread, total, moneylines *and their prices*, plus
    weather (roof/temp/wind), rest days, and the QB who actually started.  No
    API key required.
  * **CFB** -- ESPN historical results give scores (for straight-up / margin /
    total accuracy).  Closing lines require a free CollegeFootballData key
    (``CFBD_API_KEY``); when present we pull ``/lines`` for full ATS/CLV.

Everything is normalized to one schema the backtester and the live Projector
both understand.  Records are cached aggressively -- finished history never
changes.
"""

from __future__ import annotations

import csv
import io

from fantasy import util
from betting import data as _data

NFL_GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"

# nflverse team abbreviations that differ from our ESPN-style codes, mapped so a
# franchise keeps one identity across relocations/renames.
_NFL_TEAM_FIX = {"LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR", "WAS": "WSH"}


def _fix_team(t: str) -> str:
    return _NFL_TEAM_FIX.get(t, t)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# NFL (nflverse)
# ---------------------------------------------------------------------------
def _load_nfl_rows(ttl: float = 7 * 24 * 3600) -> list[dict]:
    cache_key = "bet_hist_nfl_games"
    cached = util.cache_get(cache_key, ttl)
    if cached is not None:
        return cached
    text = util.fetch_text(NFL_GAMES_URL, timeout=90)
    if not text:
        stale = util.cache_get(cache_key, None)
        return stale or []
    rows = list(csv.DictReader(io.StringIO(text)))
    util.cache_put(cache_key, rows)
    return rows


def nfl_games(seasons: list[int] | None = None,
              completed_only: bool = True) -> list[dict]:
    """Normalized NFL games (regular + post) with closing lines & context."""
    rows = _load_nfl_rows()
    want = set(seasons) if seasons else None
    out = []
    for r in rows:
        season = _i(r.get("season"))
        if want is not None and season not in want:
            continue
        if r.get("game_type") == "" :
            continue
        hs, as_ = _i(r.get("home_score")), _i(r.get("away_score"))
        completed = hs is not None and as_ is not None
        if completed_only and not completed:
            continue
        home = _fix_team(r.get("home_team", ""))
        away = _fix_team(r.get("away_team", ""))
        if not home or not away:
            continue
        spread_line = _f(r.get("spread_line"))       # +ve = home favored (Lee Sharpe)
        rec = {
            "id": r.get("game_id"),
            "league": "nfl",
            "season": season,
            "week": _i(r.get("week")),
            "game_type": r.get("game_type"),        # REG / WC / DIV / CON / SB
            "date": r.get("gameday"),
            "neutral": (r.get("location", "Home") == "Neutral"),
            "home": {"id": home, "abbr": home, "name": home, "full": home},
            "away": {"id": away, "abbr": away, "name": away, "full": away},
            "home_score": hs, "away_score": as_,
            "completed": completed,
            "closing": {
                "home_spread": (-spread_line if spread_line is not None else None),
                "total": _f(r.get("total_line")),
                "home_ml": _i(r.get("home_moneyline")),
                "away_ml": _i(r.get("away_moneyline")),
                "home_spread_odds": _i(r.get("home_spread_odds")),
                "away_spread_odds": _i(r.get("away_spread_odds")),
                "over_odds": _i(r.get("over_odds")),
                "under_odds": _i(r.get("under_odds")),
            },
            "weather": {"roof": r.get("roof"), "temp": _f(r.get("temp")),
                        "wind": _f(r.get("wind"))},
            "rest": {"home": _i(r.get("home_rest")), "away": _i(r.get("away_rest"))},
            "qb": {"home": r.get("home_qb_name"), "away": r.get("away_qb_name"),
                   "home_id": r.get("home_qb_id"), "away_id": r.get("away_qb_id")},
            "div_game": r.get("div_game") == "1",
            "stadium_id": r.get("stadium_id"),
        }
        out.append(rec)
    out.sort(key=lambda g: (g["date"] or "", g["id"] or ""))
    return out


# ---------------------------------------------------------------------------
# CFB (ESPN results + optional CFBD lines)
# ---------------------------------------------------------------------------
import datetime as _dt

_CFB_WINDOW = ((8, 20), (1, 20))


def cfb_games(seasons: list[int], with_lines: bool = True) -> list[dict]:
    """Normalized CFB games from ESPN results; closing lines merged from CFBD
    when a key is configured (else ``closing`` is None -> accuracy-only)."""
    lines_by_id = _cfbd_lines(seasons) if with_lines else {}
    out = []
    for yr in seasons:
        (sm, sd), (em, ed) = _CFB_WINDOW
        start = _dt.date(yr, sm, sd)
        end = min(_dt.date(yr + 1, em, ed), _dt.date.today())
        for g in _data.historical_games("cfb", start, end):
            rec = {
                "id": g["id"], "league": "cfb", "season": yr,
                "week": None, "date": g["date"], "neutral": g["neutral"],
                "home": {"id": g["home"]["id"], "abbr": g["home"]["abbr"],
                         "name": g["home"]["name"], "full": g["home"]["full"]},
                "away": {"id": g["away"]["id"], "abbr": g["away"]["abbr"],
                         "name": g["away"]["name"], "full": g["away"]["full"]},
                "home_score": g["home"]["score"], "away_score": g["away"]["score"],
                "completed": g["completed"],
                "closing": lines_by_id.get(str(g["id"])),
                "weather": {}, "rest": {"home": None, "away": None},
                "qb": {}, "div_game": False,
            }
            out.append(rec)
    out.sort(key=lambda g: (g["date"] or "", g["id"] or ""))
    return out


def _cfbd_lines(seasons: list[int]) -> dict:
    """CFBD closing lines keyed by ESPN game id. Empty without a key."""
    from betting import cfbd
    if not cfbd.enabled():
        return {}
    out: dict = {}
    for yr in seasons:
        out.update(cfbd.lines(yr))
    return out


def games(league: str, seasons: list[int], **kw) -> list[dict]:
    return nfl_games(seasons, **kw) if league == "nfl" else cfb_games(seasons, **kw)
