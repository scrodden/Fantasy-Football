"""Game, result, and betting-line data for the football betting model.

All primary data comes from ESPN's free, no-auth scoreboard API, which returns
schedules, live/final scores, team records, AND embedded sportsbook odds
(spread, total, moneyline) in a single call -- for both the NFL and FBS college
football.  When a free The Odds API key is configured, we additionally pull
multi-book lines so the user can line-shop for the best number.

Zero third-party dependencies -- everything rides on ``fantasy.util``.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Optional

from fantasy import util

# ESPN scoreboard endpoints ------------------------------------------------
_ESPN = "https://site.api.espn.com/apis/site/v2/sports/football"
_LEAGUE_PATH = {"nfl": "nfl", "cfb": "college-football"}
# groups=80 restricts college football to FBS (the ~136 top-division teams).
_CFB_FBS_GROUP = "80"

# The Odds API (free tier ~500 req/mo). Optional -- only used if a key is set.
_ODDS_API = "https://api.the-odds-api.com/v4/sports"
_ODDS_SPORT = {"nfl": "americanfootball_nfl", "cfb": "americanfootball_ncaaf"}

LEAGUES = ("nfl", "cfb")
LEAGUE_LABEL = {"nfl": "NFL", "cfb": "College (FBS)"}
# All-star / conference pseudo-teams from the Pro Bowl — never a real matchup.
_PSEUDO_TEAMS = {"AFC", "NFC", "NFL"}


# ---------------------------------------------------------------------------
# Config: The Odds API key
# ---------------------------------------------------------------------------
_CONFIG_PATH = os.path.join(util.BASE_DIR, "betting_config.json")


def load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            import json
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    import json
    with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def odds_api_key() -> Optional[str]:
    """Key from env var wins, then the on-disk config file."""
    return os.environ.get("ODDS_API_KEY") or load_config().get("odds_api_key") or None


# ---------------------------------------------------------------------------
# ESPN scoreboard
# ---------------------------------------------------------------------------
def _scoreboard_url(league: str, *, seasontype: int | None = None,
                    week: int | None = None, dates: str | None = None) -> str:
    path = _LEAGUE_PATH[league]
    params = ["limit=200"]
    if league == "cfb":
        params.append(f"groups={_CFB_FBS_GROUP}")
    if seasontype is not None:
        params.append(f"seasontype={seasontype}")
    if week is not None:
        params.append(f"week={week}")
    if dates is not None:
        params.append(f"dates={dates}")
    return f"{_ESPN}/{path}/scoreboard?" + "&".join(params)


def _team_record(competitor: dict) -> str:
    for rec in competitor.get("records", []) or []:
        if rec.get("type") in (None, "total") or rec.get("name") in (None, "overall"):
            return rec.get("summary", "")
    recs = competitor.get("records") or []
    return recs[0].get("summary", "") if recs else ""


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_moneyline(odds: dict):
    """Return (home_ml, away_ml) as ints, or (None, None)."""
    ml = odds.get("moneyline") or {}

    def _pick(side):
        node = ml.get(side) or {}
        val = (node.get("close") or node.get("current") or node.get("open") or {}).get("odds")
        if val is None:
            # older shape: homeTeamOdds.moneyLine
            return None
        try:
            return int(str(val).replace("+", ""))
        except ValueError:
            return None

    home = _pick("home")
    away = _pick("away")
    if home is None:
        home = _num((odds.get("homeTeamOdds") or {}).get("moneyLine"))
        home = int(home) if home is not None else None
    if away is None:
        away = _num((odds.get("awayTeamOdds") or {}).get("moneyLine"))
        away = int(away) if away is not None else None
    return home, away


def _parse_espn_odds(competition: dict, home_abbr: str) -> Optional[dict]:
    """Extract the best (priority) sportsbook line from an ESPN competition.

    ESPN's ``spread`` is quoted from the home team's perspective for most
    events, but be robust: prefer the signed home spread inferred from the
    favorite flags + the ``details`` string when present.
    """
    odds_list = competition.get("odds") or []
    if not odds_list:
        return None
    o = odds_list[0]  # provider priority 1 (e.g. DraftKings / ESPN BET consensus)
    spread = _num(o.get("spread"))
    home_spread = spread
    # Cross-check against favorite flags; ESPN "spread" is the home line.
    home_odds = o.get("homeTeamOdds") or {}
    away_odds = o.get("awayTeamOdds") or {}
    if spread is not None:
        # If away team is favored but spread is negative-for-home, flip sign.
        if away_odds.get("favorite") and not home_odds.get("favorite") and spread < 0:
            home_spread = abs(spread)
        elif home_odds.get("favorite") and not away_odds.get("favorite") and spread > 0:
            home_spread = -abs(spread)
    home_ml, away_ml = _parse_moneyline(o)
    return {
        "book": (o.get("provider") or {}).get("name", "ESPN"),
        "home_spread": home_spread,          # points; negative = home favored
        "total": _num(o.get("overUnder")),
        "home_ml": home_ml,
        "away_ml": away_ml,
        "details": o.get("details"),
    }


def _parse_event(event: dict, league: str) -> Optional[dict]:
    comps = event.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None

    def _team(c):
        t = c.get("team") or {}
        return {
            "id": t.get("id"),
            "abbr": t.get("abbreviation") or t.get("shortDisplayName"),
            "name": t.get("shortDisplayName") or t.get("name"),
            "full": t.get("displayName"),
            "logo": t.get("logo"),
            "record": _team_record(c),
            "score": int(c["score"]) if str(c.get("score", "")).strip().isdigit() else None,
            "rank": c.get("curatedRank", {}).get("current") if c.get("curatedRank", {}).get("current", 99) <= 25 else None,
        }

    status = (comp.get("status") or {}).get("type") or {}
    state = status.get("state")            # "pre" | "in" | "post"
    completed = bool(status.get("completed"))
    home_t = _team(home)
    away_t = _team(away)
    odds = _parse_espn_odds(comp, home_t["abbr"])

    return {
        "id": event.get("id"),
        "league": league,
        "date": event.get("date"),
        "name": event.get("name"),
        "neutral": bool(comp.get("neutralSite")),
        "state": state,
        "completed": completed,
        # season type: 1=preseason, 2=regular, 3=postseason (per-event).
        "seasontype": (event.get("season") or {}).get("type"),
        "status_detail": status.get("shortDetail") or status.get("description"),
        "home": home_t,
        "away": away_t,
        "espn_odds": odds,
        "books": [odds] if odds else [],   # extended by Odds API layer if configured
    }


def scoreboard(league: str, *, seasontype: int | None = None,
               week: int | None = None, dates: str | None = None,
               ttl: float = 900) -> dict:
    """Fetch and normalize a league scoreboard.

    Returns ``{"season": {...}, "week": int, "seasontype": int, "games": [...]}``.
    Cached for ``ttl`` seconds (default 15 min) to stay light on the API.
    """
    url = _scoreboard_url(league, seasontype=seasontype, week=week, dates=dates)
    tag = dates or f"st{seasontype}_wk{week}" if (dates or seasontype or week) else "current"
    key = f"bet_sb_{league}_{tag}"
    raw = util.cached_fetch(key, url, ttl_seconds=ttl)
    if not raw:
        return {"season": {}, "week": None, "seasontype": None, "games": []}
    games = []
    for ev in raw.get("events", []) or []:
        g = _parse_event(ev, league)
        if g:
            games.append(g)
    season = raw.get("season") or {}
    wk = (raw.get("week") or {}).get("number")
    return {
        "season": season,
        "year": season.get("year"),
        "week": wk,
        "seasontype": season.get("type"),
        "games": games,
    }


def current_board(league: str, ttl: float = 900) -> dict:
    """The scoreboard ESPN considers 'current' (this week's slate)."""
    return scoreboard(league, ttl=ttl)


def historical_games(league: str, start: _dt.date, end: _dt.date,
                     ttl: float = 30 * 24 * 3600) -> list[dict]:
    """Every completed game in a date range, for training/backtesting.

    ESPN accepts ``dates=YYYYMMDD-YYYYMMDD``.  We chunk by ~2-week windows to
    stay within the ~200-event response cap (college has ~100+ games/week).
    Long TTL because finished results never change.
    """
    out: list[dict] = []
    span = 14 if league == "cfb" else 21
    cur = start
    while cur <= end:
        stop = min(end, cur + _dt.timedelta(days=span - 1))
        dates = f"{cur.strftime('%Y%m%d')}-{stop.strftime('%Y%m%d')}"
        board = scoreboard(league, dates=dates, ttl=ttl)
        for g in board["games"]:
            # Train only on games that count: skip preseason (backups play, so
            # results are poor signal for team strength) and unfinished games.
            if g.get("seasontype") == 1:
                continue
            # Skip the Pro Bowl / all-star exhibition (AFC vs NFC squads).
            if g["home"]["abbr"] in _PSEUDO_TEAMS or g["away"]["abbr"] in _PSEUDO_TEAMS:
                continue
            if g["completed"] and g["home"]["score"] is not None:
                out.append(g)
        cur = stop + _dt.timedelta(days=1)
    # De-dup by event id (chunk boundaries can overlap a game's listing).
    seen, uniq = set(), []
    for g in out:
        if g["id"] not in seen:
            seen.add(g["id"])
            uniq.append(g)
    uniq.sort(key=lambda g: g["date"] or "")
    return uniq


# ---------------------------------------------------------------------------
# The Odds API (optional multi-book layer)
# ---------------------------------------------------------------------------
def _american_from_price(price) -> Optional[int]:
    try:
        return int(round(float(price)))
    except (TypeError, ValueError):
        return None


def odds_api_lines(league: str, ttl: float = 900) -> list[dict]:
    """Multi-book lines from The Odds API, or [] if no key / unavailable.

    Each entry: {home, away, commence, books:[{book, home_spread, total,
    home_ml, away_ml}]}.  Team names are ESPN-independent, matched later by
    name + kickoff time.
    """
    key = odds_api_key()
    if not key:
        return []
    sport = _ODDS_SPORT[league]
    url = (f"{_ODDS_API}/{sport}/odds?apiKey={key}&regions=us"
           f"&markets=spreads,totals,h2h&oddsFormat=american&dateFormat=iso")
    ckey = "bet_oddsapi_" + league
    raw = util.cached_fetch(ckey, url, ttl_seconds=ttl)
    if not isinstance(raw, list):
        return []
    out = []
    for ev in raw:
        home = ev.get("home_team")
        away = ev.get("away_team")
        books = []
        for bk in ev.get("bookmakers", []) or []:
            entry = {"book": bk.get("title") or bk.get("key"),
                     "home_spread": None, "total": None,
                     "home_ml": None, "away_ml": None}
            for mk in bk.get("markets", []) or []:
                outcomes = mk.get("outcomes", []) or []
                if mk.get("key") == "spreads":
                    for oc in outcomes:
                        if oc.get("name") == home:
                            entry["home_spread"] = _num(oc.get("point"))
                elif mk.get("key") == "totals":
                    if outcomes:
                        entry["total"] = _num(outcomes[0].get("point"))
                elif mk.get("key") == "h2h":
                    for oc in outcomes:
                        if oc.get("name") == home:
                            entry["home_ml"] = _american_from_price(oc.get("price"))
                        elif oc.get("name") == away:
                            entry["away_ml"] = _american_from_price(oc.get("price"))
            books.append(entry)
        out.append({"home": home, "away": away,
                    "commence": ev.get("commence_time"), "books": books})
    return out


def _name_key(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def attach_multibook(board: dict, league: str) -> dict:
    """If an Odds API key is set, merge its multi-book lines into a board's
    games (matched by team full-name).  No-op otherwise."""
    lines = odds_api_lines(league)
    if not lines:
        return board
    index = {}
    for ln in lines:
        index[(_name_key(ln["home"]), _name_key(ln["away"]))] = ln
    for g in board["games"]:
        hk, ak = _name_key(g["home"]["full"]), _name_key(g["away"]["full"])
        match = index.get((hk, ak))
        if not match:
            # try loose contains-match on last word (nickname)
            for (h2, a2), ln in index.items():
                if hk in h2 or h2 in hk:
                    if ak in a2 or a2 in ak:
                        match = ln
                        break
        if match:
            existing_books = {b["book"] for b in g["books"]}
            for b in match["books"]:
                if b["book"] not in existing_books:
                    g["books"].append(b)
    return board
