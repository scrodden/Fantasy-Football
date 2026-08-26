"""CollegeFootballData (CFBD) access -- the college data source.

Requires a free API key (collegefootballdata.com/key), read from the
``CFBD_API_KEY`` env var or the app's ``betting_config.json``.  Provides:

  * ``lines(year)``   -- historical closing spreads/totals/moneylines, keyed by
    ESPN game id (so they join cleanly to our ESPN-sourced CFB games).
  * ``ppa_games(year)`` -- per-team, per-game offensive/defensive PPA (CFBD's
    expected-points-added), for the college EPA rating model.

All calls are cached aggressively; finished seasons never change.
"""

from __future__ import annotations

import json
import os
import urllib.request

from fantasy import util
from betting import data as _data

_BASE = "https://api.collegefootballdata.com"


def key() -> str | None:
    return os.environ.get("CFBD_API_KEY") or _data.load_config().get("cfbd_api_key") or None


def enabled() -> bool:
    return bool(key())


def _get(path: str):
    k = key()
    if not k:
        return None
    req = urllib.request.Request(_BASE + path,
                                 headers={"Authorization": "Bearer " + k,
                                          "User-Agent": "FFAssistant/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        print(f"[cfbd] {path.split('?')[0]} failed: {exc}")
        return None


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


_PROVIDER_PREF = ("DraftKings", "Bovada", "ESPN Bet", "consensus")


def lines(year: int) -> dict:
    """Closing lines keyed by ESPN game id: {id: {home_spread,total,home_ml,away_ml,book}}.

    CFBD ``spread`` is already home-relative (negative = home favored), matching
    our internal convention."""
    ckey = f"bet_cfbd_lines_{year}"
    cached = util.cache_get(ckey, None)
    if cached is None:
        raw = _get(f"/lines?year={year}&seasonType=both")
        if raw is None:
            return {}
        cached = raw
        util.cache_put(ckey, raw)
    out = {}
    for g in cached:
        gid = g.get("id")
        provs = g.get("lines") or []
        if not gid or not provs:
            continue
        chosen = None
        for pref in _PROVIDER_PREF:
            chosen = next((l for l in provs if l.get("provider") == pref), None)
            if chosen:
                break
        chosen = chosen or provs[0]
        out[str(gid)] = {
            "home_spread": _f(chosen.get("spread")),      # neg = home favored
            "total": _f(chosen.get("overUnder")),
            "home_ml": _i(chosen.get("homeMoneyline")),
            "away_ml": _i(chosen.get("awayMoneyline")),
            "book": chosen.get("provider"),
        }
    return out


def all_provider_lines(year: int, week: int | None = None, ttl: float = 900) -> dict:
    """Every book's line per game (for live multi-book shopping), keyed by ESPN
    game id: {id: [{book, home_spread, total, home_ml, away_ml}, ...]}."""
    wk = f"&week={week}" if week else ""
    ckey = f"bet_cfbd_curlines_{year}_{week or 'all'}"
    raw = util.cache_get(ckey, ttl)
    if raw is None:
        raw = _get(f"/lines?year={year}&seasonType=regular{wk}")
        if raw is None:
            return {}
        util.cache_put(ckey, raw)
    out = {}
    for g in raw:
        gid = g.get("id")
        if not gid:
            continue
        books = []
        for l in g.get("lines") or []:
            if l.get("spread") is None and l.get("overUnder") is None:
                continue
            books.append({"book": l.get("provider"),
                          "home_spread": _f(l.get("spread")),
                          "total": _f(l.get("overUnder")),
                          "home_ml": _i(l.get("homeMoneyline")),
                          "away_ml": _i(l.get("awayMoneyline"))})
        if books:
            out[str(gid)] = books
    return out


def preseason(year: int) -> dict:
    """Preseason prior signals per CFBD team name:
    ``{name: {ret_pct, recruit_points, sp_rating}}``. Any piece may be missing.
    All are legitimately available *before* the season (no leakage)."""
    ckey = f"bet_cfbd_preseason_{year}"
    cached = util.cache_get(ckey, None)
    if cached is not None:
        return cached
    out: dict = {}
    for row in (_get(f"/player/returning?year={year}") or []):
        t = row.get("team")
        if t:
            out.setdefault(t, {})["ret_pct"] = _f(row.get("percentPPA"))
    for row in (_get(f"/recruiting/teams?year={year}") or []):
        t = row.get("team")
        if t:
            out.setdefault(t, {})["recruit_points"] = _f(row.get("points"))
    for row in (_get(f"/ratings/sp?year={year}") or []):
        t = row.get("team")
        if t and t != "nationalAverages":
            out.setdefault(t, {})["sp_rating"] = _f(row.get("rating"))
    if out:
        util.cache_put(ckey, out)
    return out


def apply_season_priors(proj, year: int, ret_slope: float = 0.5,
                        rec_coef: float = 15.0, sp_weight: float = 0.35) -> dict:
    """Prior-aware CFB season rollover on a live Projector.

    Returning production + recruiting (backtest-validated: ~0.17 early-season MAE
    improvement) reshape the mean reversion; SP+ (CFBD's published preseason
    projection, a strong external prior) is then blended in for live use."""
    from betting import epa
    if not enabled():
        proj.new_season(year)
        return {"priors": False}
    priors = preseason(year)
    id_to_name = epa.load("cfb").id_to_name or {}
    rp = [v.get("recruit_points") for v in priors.values() if v.get("recruit_points")]
    import statistics
    rmean = statistics.mean(rp) if rp else 0.0
    rsd = statistics.pstdev(rp) if len(rp) > 1 else 1.0
    mean = proj.elo.mean
    revert = proj.elo.revert
    ppe = proj.elo.points_per_elo
    applied = 0
    for tid, node in proj.elo.teams.items():
        name = id_to_name.get(tid)
        pri = priors.get(name) if name else None
        keep = 1 - revert
        bump = 0.0
        if pri:
            pct = pri.get("ret_pct")
            if pct is not None:
                keep = min(0.85, max(0.45, (1 - revert) + ret_slope * (pct - 0.55)))
            rpts = pri.get("recruit_points")
            if rpts is not None and rsd:
                bump = rec_coef * ((rpts - rmean) / rsd)
        r = mean + keep * (node["rating"] - mean) + bump
        if pri and pri.get("sp_rating") is not None:
            r = (1 - sp_weight) * r + sp_weight * (mean + pri["sp_rating"] * ppe)
            applied += 1
        node["rating"] = r
        node["gp"] = 0
    proj.pace.new_season()
    proj.elo.meta["season"] = year
    return {"priors": True, "sp_applied": applied, "teams": len(proj.elo.teams)}


def _lines_meta(year: int) -> dict:
    """{game_id: (homeTeam, awayTeam)} from the cached lines payload."""
    ckey = f"bet_cfbd_lines_{year}"
    cached = util.cache_get(ckey, None)
    if cached is None:
        lines(year)  # populates the cache
        cached = util.cache_get(ckey, None) or []
    return {str(g.get("id")): (g.get("homeTeam"), g.get("awayTeam")) for g in cached
            if g.get("id")}


def team_game_epa(year: int) -> dict:
    """Per-game team PPA joined to home/away, for the CFB EPA model:
    ``{game_id: {week, home, away, off: {team_name: ppa}}}`` (names are CFBD's)."""
    ppa = ppa_games(year)
    meta = _lines_meta(year)
    out = {}
    for gid, g in ppa.items():
        ha = meta.get(gid)
        if not ha or ha[0] not in g["off"] or ha[1] not in g["off"]:
            continue
        out[gid] = {"week": g["week"], "home": ha[0], "away": ha[1], "off": g["off"]}
    return out


def ppa_games(year: int) -> dict:
    """Per-game team PPA (CFBD's EPA). Returns
    ``{game_id: {week, off: {team_name: ppa/play}}}`` -- both teams' offensive
    PPA, so a rating model can opponent-adjust."""
    ckey = f"bet_cfbd_ppa_{year}"
    cached = util.cache_get(ckey, None)
    if cached is None:
        rows = []
        # CFBD paginates by week for PPA; pull the regular season + postseason.
        for st in ("regular", "postseason"):
            r = _get(f"/ppa/games?year={year}&seasonType={st}&excludeGarbageTime=true")
            if r:
                rows.extend(r)
        if not rows:
            return {}
        cached = rows
        util.cache_put(ckey, rows)
    games: dict = {}
    for row in cached:
        gid = row.get("gameId")
        team = row.get("team")
        off = (row.get("offense") or {}).get("overall")
        if gid is None or not team or off is None:
            continue
        g = games.setdefault(str(gid), {"week": row.get("week"), "off": {}})
        g["off"][team] = _f(off)
    return games
