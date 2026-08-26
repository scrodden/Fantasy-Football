"""Data source clients: Sleeper (stats/projections/players) and ESPN (news/injuries).

All endpoints are free and require no authentication. Results are cached to disk.
Historical (completed) seasons are cached forever; anything that changes during
the current season uses a short TTL so a manual refresh picks up new data.
"""

from __future__ import annotations

import csv
import io
import os
import re
import time

from . import util

SLEEPER = "https://api.sleeper.app/v1"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
CFBD = "https://api.collegefootballdata.com"

# ESPN team abbreviation -> Sleeper team abbreviation.
TEAM_ALIASES = {"WSH": "WAS"}

TTL_BYES = 7 * 86400  # bye weeks are fixed once the schedule is out
TTL_COLLEGE = 30 * 86400  # a rookie's college stats never change

# TTLs (seconds)
TTL_STATE = 6 * 3600
TTL_PLAYERS = 12 * 3600
TTL_CURRENT_STATS = 3 * 3600
TTL_PROJECTIONS = 3 * 3600
TTL_NEWS = 1 * 3600
TTL_INJURIES = 1 * 3600

# Positions Yahoo treats as draftable in a standard league.
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}


# ---------------------------------------------------------------------------
# Sleeper
# ---------------------------------------------------------------------------
def get_state() -> dict:
    """Current NFL season/week context."""
    data = util.cached_fetch("state", f"{SLEEPER}/state/nfl", TTL_STATE)
    return data or {}


def get_players() -> dict:
    """All NFL players keyed by Sleeper player_id (~11k entries, ~15 MB)."""
    data = util.cached_fetch("players", f"{SLEEPER}/players/nfl", TTL_PLAYERS, timeout=90)
    return data or {}


def get_season_stats(year: int, is_current: bool) -> dict:
    """Season-total stats for every player for a given year, keyed by player_id."""
    ttl = TTL_CURRENT_STATS if is_current else None  # completed seasons never change
    data = util.cached_fetch(
        f"stats_{year}", f"{SLEEPER}/stats/nfl/regular/{year}", ttl, timeout=60
    )
    return data or {}


def get_week_stats(year: int, week: int) -> dict:
    """Actual per-player stats for a single week (Sleeper), keyed by player_id."""
    data = util.cached_fetch(
        f"wstats_{year}_wk{week}", f"{SLEEPER}/stats/nfl/regular/{year}/{week}",
        TTL_CURRENT_STATS, timeout=45,
    )
    return data or {}


def get_week_projections(year: int, week: int) -> dict:
    """Sleeper's own projections for a given week, keyed by player_id."""
    data = util.cached_fetch(
        f"proj_{year}_wk{week}",
        f"{SLEEPER}/projections/nfl/regular/{year}/{week}",
        TTL_PROJECTIONS,
        timeout=60,
    )
    return data or {}


ESPN_FF = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leaguedefaults/3?view=kona_player_info"
ESPN_STAT_MAP = {
    "3": "pass_yd", "4": "pass_td", "20": "pass_int",
    "23": "rush_att", "24": "rush_yd", "25": "rush_td",
    "53": "rec", "42": "rec_yd", "43": "rec_td", "58": "rec_tgt", "72": "fum_lost",
}


def get_espn_projections(year: int) -> dict:
    """ESPN's season projections as raw stat lines, keyed by normalized name.
    A second independent source for ensembling with Sleeper's projections."""
    import json as _json
    key = f"espn_proj_{year}"
    cached = util.cache_get(key, TTL_PROJECTIONS)
    if cached is not None:
        return cached
    hdr = {"X-Fantasy-Filter": _json.dumps(
        {"players": {"limit": 500, "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}})}
    data = util.fetch_json(ESPN_FF.format(year=year), timeout=45, headers=hdr)
    out = {}
    for pw in (data or {}).get("players", []):
        p = pw.get("player", {}) or {}
        nm = p.get("fullName")
        if not nm:
            continue
        season = [s for s in p.get("stats", []) if s.get("statSourceId") == 1 and s.get("scoringPeriodId") == 0]
        if not season:
            continue
        st = season[0].get("stats", {}) or {}
        cats = {}
        for sid, cat in ESPN_STAT_MAP.items():
            v = st.get(sid)
            if v:
                cats[cat] = round(float(v), 2)
        if cats:
            out[_norm_name(nm)] = cats
    if out:
        util.cache_put(key, out)
    return out


def get_season_projections(year: int) -> dict:
    """Sleeper full-season projections (used for rest-of-season), keyed by player_id."""
    data = util.cached_fetch(
        f"proj_season_{year}",
        f"{SLEEPER}/projections/nfl/regular/{year}",
        TTL_PROJECTIONS,
        timeout=60,
    )
    return data or {}


# ---------------------------------------------------------------------------
# ESPN -- news + detailed injury reports (the "scour the internet" layer)
# ---------------------------------------------------------------------------
def get_news() -> list:
    """Latest NFL headlines from ESPN."""
    data = util.cached_fetch("espn_news", f"{ESPN}/news?limit=50", TTL_NEWS)
    items = []
    for art in (data or {}).get("articles", []):
        links = art.get("links", {}) or {}
        web = (links.get("web") or {}).get("href", "")
        items.append(
            {
                "headline": art.get("headline", ""),
                "description": art.get("description", ""),
                "published": art.get("published", ""),
                "url": web,
            }
        )
    return items


def _norm_name(name: str) -> str:
    """Normalize a player name for fuzzy matching across sources."""
    name = (name or "").lower()
    name = re.sub(r"[^a-z ]", "", name)  # drop punctuation (O'Dell, Jr., etc.)
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def get_injury_index() -> dict:
    """Map normalized player name -> detailed injury report from ESPN.

    Each value: {status, type, detail, date}. This complements Sleeper's short
    injury_status code with a human-readable comment on expected availability.
    """
    data = util.cached_fetch("espn_injuries", f"{ESPN}/injuries", TTL_INJURIES, timeout=60)
    index: dict = {}
    for team in (data or {}).get("injuries", []):
        for inj in team.get("injuries", []):
            athlete = inj.get("athlete", {}) or {}
            name = athlete.get("displayName") or ""
            key = _norm_name(name)
            if not key:
                continue
            detail = inj.get("longComment") or inj.get("shortComment") or ""
            typ = ""
            details = inj.get("details") or {}
            if isinstance(details, dict):
                typ = details.get("type") or details.get("detail") or ""
            index[key] = {
                "status": inj.get("status", ""),
                "type": typ,
                "detail": detail.strip(),
                "date": inj.get("date", ""),
            }
    return index


def news_for_player(full_name: str, team: str | None = None, limit: int = 6) -> list:
    """Filter the general NFL news feed for items that mention this player/team."""
    news = get_news()
    key_parts = _norm_name(full_name).split()
    last = key_parts[-1] if key_parts else ""
    first = key_parts[0] if key_parts else ""
    out = []
    for item in news:
        blob = _norm_name(item["headline"] + " " + item["description"])
        if last and last in blob and (not first or first[:3] in blob or last != first):
            out.append(item)
        elif last and f" {last} " in f" {blob} ":
            out.append(item)
        if len(out) >= limit:
            break
    return out


def research_links(full_name: str) -> list:
    """Live external search links so the user can dig deeper for any player."""
    q = full_name.replace(" ", "+")
    q_nfl = (full_name + " NFL fantasy").replace(" ", "+")
    return [
        {"label": "Google News", "url": f"https://news.google.com/search?q={q_nfl}"},
        {"label": "ESPN", "url": f"https://www.espn.com/search/_/q/{q}"},
        {"label": "FantasyPros", "url": f"https://www.fantasypros.com/nfl/search/?q={q}"},
        {"label": "Rotowire", "url": f"https://www.rotowire.com/search.php?sport=NFL&term={q}"},
        {"label": "Reddit r/fantasyfootball", "url": f"https://www.reddit.com/r/fantasyfootball/search/?q={q}&sort=new"},
    ]


# ---------------------------------------------------------------------------
# Bye weeks (ESPN) -- keyed by Sleeper team abbreviation
# ---------------------------------------------------------------------------
def get_bye_weeks(season: int) -> dict:
    cached = util.cache_get(f"byes_{season}", TTL_BYES)
    if cached is not None:
        return cached
    teams = util.fetch_json(f"{ESPN}/teams", timeout=30)
    abbrs = []
    try:
        for sport in (teams or {}).get("sports", []):
            for lg in sport.get("leagues", []):
                for t in lg.get("teams", []):
                    ab = t.get("team", {}).get("abbreviation")
                    if ab:
                        abbrs.append(ab)
    except (AttributeError, TypeError):
        abbrs = []
    byes: dict = {}
    for ab in abbrs:
        sch = util.fetch_json(
            f"{ESPN}/teams/{ab.lower()}/schedule?season={season}&seasontype=2", timeout=30
        )
        if not sch:
            continue
        bw = sch.get("byeWeek")
        if bw:
            key = TEAM_ALIASES.get(ab.upper(), ab.upper())
            byes[key] = int(bw)
    if byes:
        util.cache_put(f"byes_{season}", byes)
    return byes


# ---------------------------------------------------------------------------
# Offensive-line rosters (nflverse, free) -- to grade the 2026 line composition
# ---------------------------------------------------------------------------
NFLVERSE_ROSTER = "https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{year}.csv"
NFLVERSE_WEEKLY = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{year}.csv"
NFLVERSE_GAMES = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
# nflverse team code -> Sleeper team code (only the ones that differ)
NFLVERSE_TO_SLEEPER = {"AZ": "ARI", "LA": "LAR"}
TTL_ROSTER = 3 * 86400
TTL_DVP = 30 * 86400      # completed-season defense-vs-position data is static
TTL_CURRENT_DVP = 12 * 3600  # in-progress season refreshes ~twice a day
TTL_SCHEDULE = 1 * 86400
MATCH_POSITIONS = ("QB", "RB", "WR", "TE", "K")


def _roster_rows(year: int) -> list:
    """Minimal roster rows for a season from nflverse (cached)."""
    key = f"roster_rows_{year}"
    cached = util.cache_get(key, TTL_ROSTER)
    if cached is not None:
        return cached
    text = util.fetch_text(NFLVERSE_ROSTER.format(year=year), timeout=60)
    rows = []
    if text:
        for r in csv.DictReader(io.StringIO(text)):
            try:
                exp = int(float(r.get("years_exp") or 0))
            except ValueError:
                exp = 0
            rows.append({
                "gsis_id": r.get("gsis_id") or "",
                "sleeper_id": r.get("sleeper_id") or "",
                "position": r.get("position") or "",
                "team": r.get("team") or "",
                "name": r.get("full_name") or r.get("football_name") or "",
                "depth": (r.get("depth_chart_position") or "").upper(),
                "status": r.get("status") or "",
                "exp": exp,
            })
    if rows:
        util.cache_put(key, rows)
    return rows


def get_ol_roster(year: int) -> dict:
    """Offensive linemen for a season from nflverse.

    Returns {"by_team": {sleeper_team: [ {gsis_id,name,depth,exp,status} ]},
             "team_by_gsis": {gsis_id: sleeper_team}}.
    """
    by_team: dict = {}
    team_by_gsis: dict = {}
    for row in _roster_rows(year):
        if row["position"] != "OL":
            continue
        gsis = row["gsis_id"]
        team = NFLVERSE_TO_SLEEPER.get(row["team"], row["team"])
        by_team.setdefault(team, []).append({
            "gsis_id": gsis, "name": row["name"], "depth": row["depth"],
            "exp": row["exp"], "status": row["status"],
        })
        if gsis:
            team_by_gsis[gsis] = team
    return {"by_team": by_team, "team_by_gsis": team_by_gsis}


def get_id_map(years) -> dict:
    """Map Sleeper player_id <-> nflverse gsis_id, built from rosters.
    Later years override earlier ones."""
    key = "id_map"
    cached = util.cache_get(key, TTL_ROSTER)
    if cached is not None:
        return cached
    s2g, g2s = {}, {}
    for yr in years:
        for r in _roster_rows(yr):
            sid, gid = r["sleeper_id"], r["gsis_id"]
            if sid and gid:
                s2g[sid] = gid
                g2s[gid] = sid
    out = {"sleeper_to_gsis": s2g, "gsis_to_sleeper": g2s}
    if s2g:
        util.cache_put(key, out)
    return out


def get_weekly_points(year: int, is_current: bool = False) -> dict:
    """Per-player weekly PPR fantasy points for a season: {gsis_id: [pts, ...]}.
    Used for consistency (floor/ceiling/boom-bust)."""
    key = f"weekly_pts_{year}"
    cached = util.cache_get(key, TTL_CURRENT_DVP if is_current else TTL_DVP)
    if cached is not None:
        return cached
    text = util.fetch_text(NFLVERSE_WEEKLY.format(year=year), timeout=90)
    pts: dict = {}
    if text:
        for r in csv.DictReader(io.StringIO(text)):
            if (r.get("season_type") or "REG") != "REG":
                continue
            gid = r.get("player_id")
            if not gid:
                continue
            try:
                p = float(r.get("fantasy_points_ppr") or 0)
            except ValueError:
                p = 0.0
            pts.setdefault(gid, []).append(round(p, 2))
    if pts:
        util.cache_put(key, pts)
    return pts


def get_trending() -> dict:
    """Most-added players in the last 24h across Sleeper: {sleeper_id: count}."""
    data = util.cached_fetch(
        "trending_add", f"{SLEEPER}/players/nfl/trending/add?lookback_hours=24&limit=200",
        TTL_NEWS,
    )
    return {d["player_id"]: d["count"] for d in (data or []) if d.get("player_id")}


# ---------------------------------------------------------------------------
# Defense-vs-position + schedule -> opponent-adjusted projections
# ---------------------------------------------------------------------------
def get_dvp(year: int, is_current: bool = False) -> dict:
    """Fantasy points allowed per game by each defense to each position, from
    nflverse weekly stats. Returns {"per_game": {team: {pos: ppg_allowed}},
    "league": {pos: league_avg_ppg}, "games": {team: n}}.

    Current-season data uses a short TTL so it refreshes as games are played."""
    key = f"dvp_{year}"
    ttl = TTL_CURRENT_DVP if is_current else TTL_DVP
    cached = util.cache_get(key, ttl)
    if cached is not None:
        return cached
    text = util.fetch_text(NFLVERSE_WEEKLY.format(year=year), timeout=90)
    allowed: dict = {}
    weeks: dict = {}
    if text:
        for row in csv.DictReader(io.StringIO(text)):
            if (row.get("season_type") or "REG") != "REG":
                continue
            pos = row.get("position") or row.get("position_group")
            opp = row.get("opponent_team")
            if not opp or pos not in MATCH_POSITIONS:
                continue
            opp = NFLVERSE_TO_SLEEPER.get(opp, opp)
            try:
                pts = float(row.get("fantasy_points_ppr") or 0)
            except ValueError:
                pts = 0.0
            allowed.setdefault(opp, {}).setdefault(pos, 0.0)
            allowed[opp][pos] += pts
            weeks.setdefault(opp, set()).add(row.get("week"))
    games = {d: len(weeks.get(d, set())) for d in allowed}
    per_game = {
        d: {p: allowed[d].get(p, 0.0) / max(1, games.get(d, 1)) for p in MATCH_POSITIONS}
        for d in allowed
    }
    league = {}
    for p in MATCH_POSITIONS:
        vals = [per_game[d][p] for d in per_game if per_game[d][p] > 0]
        league[p] = (sum(vals) / len(vals)) if vals else 0.0
    out = {"per_game": per_game, "league": league, "games": games}
    if per_game:
        util.cache_put(key, out)
    return out


def get_schedule() -> dict:
    """Regular-season opponent map from nflverse: {season: {team: {week: opp}}}."""
    key = "schedule"
    cached = util.cache_get(key, TTL_SCHEDULE)
    if cached is not None:
        return cached
    text = util.fetch_text(NFLVERSE_GAMES, timeout=60)
    sched: dict = {}
    if text:
        for row in csv.DictReader(io.StringIO(text)):
            if row.get("game_type") != "REG":
                continue
            season = str(row.get("season"))
            try:
                wk = int(row.get("week"))
            except (TypeError, ValueError):
                continue
            home = NFLVERSE_TO_SLEEPER.get(row.get("home_team"), row.get("home_team"))
            away = NFLVERSE_TO_SLEEPER.get(row.get("away_team"), row.get("away_team"))
            if not home or not away:
                continue
            sched.setdefault(season, {}).setdefault(home, {})[wk] = away
            sched.setdefault(season, {}).setdefault(away, {})[wk] = home
    if sched:
        util.cache_put(key, sched)
    return sched


# ---------------------------------------------------------------------------
# College stats for rookies (CollegeFootballData API -- optional free key)
# ---------------------------------------------------------------------------
def cfbd_enabled() -> bool:
    return bool(os.environ.get("CFBD_API_KEY", "").strip())


def _cfbd_get(path: str):
    key = os.environ.get("CFBD_API_KEY", "").strip()
    if not key:
        return None
    return util.fetch_json(
        CFBD + path, timeout=30, headers={"Authorization": f"Bearer {key}"}
    )


# CFBD stat categories worth showing, grouped for a readable table.
_CFBD_LABELS = {
    "passing": ["YDS", "TD", "INT", "COMPLETIONS", "ATT", "PCT"],
    "rushing": ["YDS", "TD", "CAR", "YPC"],
    "receiving": ["YDS", "TD", "REC"],
}


def get_college_stats(name: str, seasons: int = 3) -> dict | None:
    """Return {'seasons': [...], 'source': ...} for a rookie, or None.

    Requires a free CFBD API key in the CFBD_API_KEY environment variable.
    Exception-safe: any failure returns None so the UI falls back to links.
    """
    cache_key = f"college_{_norm_name(name).replace(' ', '_')}"
    cached = util.cache_get(cache_key, TTL_COLLEGE)
    if cached is not None:
        return cached or None  # cached empty dict means "looked up, found nothing"
    if not cfbd_enabled():
        return None
    try:
        found = _cfbd_get(f"/player/search?searchTerm={name.replace(' ', '%20')}")
        if not found:
            util.cache_put(cache_key, {})
            return None
        player = found[0]
        pid = player.get("id")
        team = player.get("team")
        # Pull per-season stat rows for the player's team and keep this player's.
        by_year: dict = {}
        for yr in range(player.get("lastSeason", 2025), player.get("lastSeason", 2025) - seasons, -1):
            rows = _cfbd_get(
                f"/stats/player/season?year={yr}&team={str(team).replace(' ', '%20')}"
            )
            if not rows:
                continue
            for r in rows:
                if str(r.get("playerId")) != str(pid):
                    continue
                cat = (r.get("category") or "").lower()
                if cat not in _CFBD_LABELS:
                    continue
                stat_type = (r.get("statType") or "").upper()
                by_year.setdefault(yr, {"year": yr, "team": team})
                by_year[yr][f"{cat}_{stat_type}"] = r.get("stat")
        out = {
            "player": player.get("name", name),
            "position": player.get("position"),
            "seasons": [by_year[y] for y in sorted(by_year, reverse=True)],
            "labels": _CFBD_LABELS,
            "source": "CollegeFootballData.com",
        }
        util.cache_put(cache_key, out if out["seasons"] else {})
        return out if out["seasons"] else None
    except Exception as exc:  # never let college lookup break a player page
        print(f"[college] lookup failed for {name}: {exc}")
        return None


def college_links(name: str, college: str | None) -> list:
    q = name.replace(" ", "+")
    links = [
        {"label": "Sports-Reference (CFB)", "url": f"https://www.sports-reference.com/cfb/search/search.fcgi?search={q}"},
        {"label": "ESPN College", "url": f"https://www.espn.com/search/_/q/{q}"},
    ]
    if college:
        links.append({"label": f"{college} football", "url": f"https://news.google.com/search?q={college.replace(' ', '+')}+football+{q}"})
    return links


# ---------------------------------------------------------------------------
# Team pages: names, records, coach/division, and a news feed
# ---------------------------------------------------------------------------
TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}

TEAM_DIVISION = {
    "BUF": "AFC East", "MIA": "AFC East", "NE": "AFC East", "NYJ": "AFC East",
    "BAL": "AFC North", "CIN": "AFC North", "CLE": "AFC North", "PIT": "AFC North",
    "HOU": "AFC South", "IND": "AFC South", "JAX": "AFC South", "TEN": "AFC South",
    "DEN": "AFC West", "KC": "AFC West", "LAC": "AFC West", "LV": "AFC West",
    "DAL": "NFC East", "NYG": "NFC East", "PHI": "NFC East", "WAS": "NFC East",
    "CHI": "NFC North", "DET": "NFC North", "GB": "NFC North", "MIN": "NFC North",
    "ATL": "NFC South", "CAR": "NFC South", "NO": "NFC South", "TB": "NFC South",
    "ARI": "NFC West", "LAR": "NFC West", "SEA": "NFC West", "SF": "NFC West",
}


def get_team_records(year: int) -> dict:
    """Regular-season W-L-T + points for/against per team, from game scores."""
    key = f"records_{year}"
    cached = util.cache_get(key, 12 * 3600)
    if cached is not None:
        return cached
    text = util.fetch_text(NFLVERSE_GAMES, timeout=60)
    rec: dict = {}
    if text:
        for r in csv.DictReader(io.StringIO(text)):
            if r.get("game_type") != "REG" or str(r.get("season")) != str(year):
                continue
            hs, as_ = r.get("home_score"), r.get("away_score")
            if not hs or not as_:
                continue
            try:
                hs, as_ = int(float(hs)), int(float(as_))
            except ValueError:
                continue
            h = NFLVERSE_TO_SLEEPER.get(r["home_team"], r["home_team"])
            a = NFLVERSE_TO_SLEEPER.get(r["away_team"], r["away_team"])
            for tm in (h, a):
                rec.setdefault(tm, {"w": 0, "l": 0, "t": 0, "pf": 0, "pa": 0, "g": 0})
            rec[h]["pf"] += hs; rec[h]["pa"] += as_; rec[h]["g"] += 1
            rec[a]["pf"] += as_; rec[a]["pa"] += hs; rec[a]["g"] += 1
            if hs > as_: rec[h]["w"] += 1; rec[a]["l"] += 1
            elif as_ > hs: rec[a]["w"] += 1; rec[h]["l"] += 1
            else: rec[h]["t"] += 1; rec[a]["t"] += 1
    if rec:
        util.cache_put(key, rec)
    return rec


def get_team_detail(abbr: str) -> dict:
    """Coach, division, and current record for a team (ESPN)."""
    key = f"teamdetail_{abbr}"
    cached = util.cache_get(key, 12 * 3600)
    if cached is not None:
        return cached
    out = {"coach": None, "coach_exp": None, "division": None, "record": None}
    det = util.fetch_json(f"{ESPN}/teams/{abbr.lower()}", timeout=20)
    if det:
        t = det.get("team", {}) or {}
        items = (t.get("record", {}) or {}).get("items", [])
        tot = next((i for i in items if i.get("type") == "total"), None)
        if tot:
            out["record"] = tot.get("summary")
        grp = t.get("groups", {}) or {}
        out["division"] = grp.get("name") or (grp.get("parent", {}) or {}).get("name")
    ros = util.fetch_json(f"{ESPN}/teams/{abbr.lower()}/roster", timeout=30)
    if ros:
        c = ros.get("coach")
        if isinstance(c, list):
            c = c[0] if c else None
        if isinstance(c, dict):
            nm = (c.get("firstName", "") + " " + c.get("lastName", "")).strip()
            out["coach"] = nm or c.get("displayName")
            out["coach_exp"] = c.get("experience")
    util.cache_put(key, out)
    return out


def get_team_news(query: str, limit: int = 20) -> list:
    """Local + national coverage via Google News RSS (free, no auth)."""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote
    key = "teamnews_" + re.sub(r"[^a-z0-9]+", "_", query.lower())
    cached = util.cache_get(key, 3600)
    if cached is not None:
        return cached
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
    text = util.fetch_text(url, timeout=20)
    items = []
    if text:
        try:
            root = ET.fromstring(text)
            for it in root.findall(".//item")[:limit]:
                title = it.findtext("title") or ""
                src = it.findtext("source") or ""
                if not src and " - " in title:
                    src = title.rsplit(" - ", 1)[-1]
                items.append({
                    "title": title,
                    "link": it.findtext("link") or "",
                    "pub": it.findtext("pubDate") or "",
                    "source": src,
                })
        except ET.ParseError:
            pass
    if items:
        util.cache_put(key, items)
    return items


# ---------------------------------------------------------------------------
# Insider chatter: Bluesky (free public API) + Reddit search (free JSON).
# Monitors reporters' social feeds for injury / roster / depth-chart news and
# folds it into the team + player news panels. No paid feeds, no auth.
# ---------------------------------------------------------------------------
INSIDER_KEYWORDS = (
    "injur", "questionable", "doubtful", "ruled out", " out ", "inactive",
    "injured reserve", " ir ", "designated to return", "pup", "activ", "waiv",
    "sign", "release", "cut", "trade", "acquir", "claim", "suspend",
    "promot", "demot", "starter", "starting", "benched", "depth chart",
    "snap count", "workload", "carries", "targets", "return", "practice",
    "dnp", "limited", "full go", "concussion", "hamstring", "ankle", "knee",
    "acl", "achilles", "groin", "quad", "calf", "shoulder", "back spasms",
)


def _is_insider_signal(text: str) -> bool:
    t = " " + (text or "").lower() + " "
    return any(k in t for k in INSIDER_KEYWORDS)


def _load_insiders() -> list:
    """Read data/insiders.txt → [(handle, team_tag_or_None)]. Falls back to a
    built-in default if the file is missing."""
    path = os.path.join(util.BASE_DIR, "data", "insiders.txt")
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "," in line:
                    handle, tag = line.split(",", 1)
                    out.append((handle.strip().lstrip("@"), tag.strip().upper() or None))
                else:
                    out.append((line.lstrip("@"), None))
    except OSError:
        pass
    if not out:
        out = [("adamschefter.bsky.social", None), ("rapsheet.bsky.social", None),
               ("tompelissero.bsky.social", None), ("mikegarafolo.bsky.social", None)]
    return out


def _bsky_post_url(handle: str, uri: str) -> str:
    # at://did/app.bsky.feed.post/<rkey>  ->  https://bsky.app/profile/<handle>/post/<rkey>
    rkey = uri.rsplit("/", 1)[-1] if uri else ""
    return f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else f"https://bsky.app/profile/{handle}"


def get_bluesky_posts(limit_per: int = 12) -> list:
    """Recent keyword-matching posts from the curated insider handles.
    Cached 20 min. Returns [{platform, source, handle, team_tag, text, link, pub}]."""
    cached = util.cache_get("bsky_insiders", 1200)
    if cached is not None:
        return cached
    items = []
    for handle, tag in _load_insiders():
        url = (f"https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
               f"?actor={handle}&limit={limit_per}&filter=posts_no_replies")
        data = util.fetch_json(url, timeout=15, retries=2)
        if not data or "feed" not in data:
            continue  # handle missing / renamed -> skip silently
        for fp in data.get("feed", []):
            post = fp.get("post") or {}
            rec = post.get("record") or {}
            text = rec.get("text") or ""
            if not _is_insider_signal(text):
                continue
            author = post.get("author") or {}
            items.append({
                "platform": "Bluesky",
                "source": author.get("displayName") or handle,
                "handle": handle,
                "team_tag": tag,
                "text": text,
                "link": _bsky_post_url(handle, post.get("uri") or ""),
                "pub": rec.get("createdAt") or post.get("indexedAt") or "",
            })
    items.sort(key=lambda x: x.get("pub") or "", reverse=True)
    util.cache_put("bsky_insiders", items)
    return items


def get_insider_feed(player: str | None = None, team: str | None = None, limit: int = 15) -> list:
    """Beat-reporter chatter (Bluesky) filtered to a player or team.
    `team` may be a full name or abbreviation. Reddit was dropped — it 403-blocks
    all unauthenticated requests, which the free-data-only constraint can't clear."""
    bsky = get_bluesky_posts()
    items = []
    if player:
        parts = _norm_name(player).split()
        last = parts[-1] if parts else ""
        for it in bsky:
            if last and last in _norm_name(it["text"]):
                items.append(it)
    elif team:
        abbr = TEAM_ALIASES.get(str(team).upper(), str(team).upper())
        full = TEAM_NAMES.get(abbr, team)
        low_full = (full or "").lower()
        nick = low_full.split()[-1] if low_full else ""
        for it in bsky:
            tag = it.get("team_tag")
            if tag:                                    # beat writer tied to one team
                if TEAM_ALIASES.get(tag, tag) == abbr:
                    items.append(it)
            elif nick and nick in it["text"].lower():  # insider mentioning the team
                items.append(it)
    else:
        items = list(bsky)
    # de-dupe newest-first. Also collapse aggregator mirrors of a first-party
    # post: the bot reposts "<Reporter>: <same text>", so compare the news body
    # (strip a leading "Name:" and keep the last chunk of normalized text).
    def _body_key(text):
        t = text.split(":", 1)[-1] if ":" in text[:40] else text
        t = re.sub(r"[^a-z0-9 ]", "", t.lower())
        t = re.sub(r"\s+", " ", t).strip()
        return t[:90]  # prefix: stable across mirrors (which vary the tail: @handles vs names)

    def _rank(x):
        h = (x.get("handle") or "").lower()
        first_party = 0 if any(k in h for k in ("bot", "mirror", "reposter")) else 1
        return (x.get("pub") or "", first_party)  # newest first, then real reporter over bot

    seen_link, seen_body, uniq = set(), set(), []
    for it in sorted(items, key=_rank, reverse=True):
        bk = _body_key(it["text"])
        if it["link"] in seen_link or (bk and bk in seen_body):
            continue
        seen_link.add(it["link"])
        seen_body.add(bk)
        uniq.append(it)
        if len(uniq) >= limit:
            break
    return uniq


def data_freshness() -> dict:
    """Return how old the key cached datasets are, in human terms."""
    def human(key):
        age = util.cache_age(key)
        if age is None:
            return "not loaded"
        if age < 90:
            return "just now"
        if age < 3600:
            return f"{int(age // 60)} min ago"
        if age < 86400:
            return f"{int(age // 3600)} hr ago"
        return f"{int(age // 86400)} day(s) ago"

    return {
        "players": human("players"),
        "injuries": human("espn_injuries"),
        "news": human("espn_news"),
    }
