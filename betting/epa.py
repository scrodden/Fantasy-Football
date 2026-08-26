"""EPA-based team ratings (NFL) -- opponent-adjusted efficiency.

Expected Points Added per play is far more stable and predictive than raw
points: it strips out garbage-time, defensive/special-teams scores, and the
noise of a few big plays.  We aggregate nflverse play-by-play to each team's
offensive EPA/play per game, keep an opponent-adjusted rolling rating for every
team's offense and defense, and convert the matchup into a point spread.

The value is measured in the backtest (``backtest.epa_ablation``) before it's
trusted; live, ``sync(season)`` rebuilds ratings from the current season's
play-by-play so the model can lean on efficiency as games accumulate.

Play-by-play files are ~20 MB each; downloads are cached and the compact
per-game aggregate is cached separately so we parse each season only once.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import urllib.request

from fantasy import util
from betting.elo import DATA_DIR

PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{year}.csv.gz"

# nflverse relocation codes -> our canonical codes (match history/signals).
_FIX = {"LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR", "WAS": "WSH"}

# Per-league EPA tuning, from the backtest ablations.
#   NFL: a 20% blend lowers margin MAE ~0.03 (EPA-from-PBP is a small supplement).
#   CFB: a 50% blend lowers margin MAE ~0.26 (CFBD PPA adds real signal in the
#        talent-gap-heavy college game).
PLAYS_PER_GAME = 55.0     # NFL EPA/play x this ~ points
DEFAULT_HFA = 1.6
DEFAULT_ALPHA = 0.10
DEFAULT_BLEND = 0.20
CFB_PLAYS = 85.0
CFB_HFA = 2.4
CFB_ALPHA = 0.12
CFB_BLEND = 0.50
BLEND_BY_LEAGUE = {"nfl": DEFAULT_BLEND, "cfb": CFB_BLEND}


def _fix(t):
    return _FIX.get(t, t)


def team_game_epa(year: int) -> dict:
    """Per-game offensive EPA/play for each team. Returns
    ``{game_id: {season, week, home, away, off: {team: epa/play}, plays: {team: n}}}``.
    Cached so each season's PBP is parsed at most once."""
    ckey = f"bet_epa_agg_{year}"
    cached = util.cache_get(ckey, None)      # completed seasons never change
    if cached is not None and year < _current_season():
        return {k: v for k, v in cached.items()}
    raw = _download_pbp(year)
    if not raw:
        return cached or {}
    games: dict = {}
    for r in raw:
        pt = r.get("play_type")
        if pt not in ("pass", "run"):
            continue
        pos, def_ = r.get("posteam"), r.get("defteam")
        epa = r.get("epa")
        gid = r.get("game_id")
        if not pos or not def_ or not gid or epa in (None, "", "NA"):
            continue
        try:
            e = float(epa)
        except ValueError:
            continue
        pos, def_ = _fix(pos), _fix(def_)
        g = games.get(gid)
        if g is None:
            g = games[gid] = {"season": _int(r.get("season")), "week": _int(r.get("week")),
                              "home": _fix(r.get("home_team")), "away": _fix(r.get("away_team")),
                              "sum": {}, "plays": {}}
        g["sum"][pos] = g["sum"].get(pos, 0.0) + e
        g["plays"][pos] = g["plays"].get(pos, 0) + 1
    out = {}
    for gid, g in games.items():
        off = {t: g["sum"][t] / g["plays"][t] for t in g["sum"] if g["plays"][t] >= 10}
        if len(off) < 2:
            continue
        out[gid] = {"season": g["season"], "week": g["week"], "home": g["home"],
                    "away": g["away"], "off": off, "plays": g["plays"]}
    util.cache_put(ckey, out)
    return out


_PBP_COLS = ("game_id", "season", "week", "season_type", "posteam", "defteam",
             "home_team", "away_team", "epa", "play_type",
             "passer_player_id", "passer_player_name", "qb_epa", "qb_dropback")


def _download_pbp(year: int):
    ckey = f"bet_pbp_raw2_{year}"    # v2 key: now also keeps passer/qb columns
    cached = util.cache_get(ckey, None)
    if cached is not None:
        return cached
    url = PBP_URL.format(year=year)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            blob = resp.read()
        text = gzip.decompress(blob).decode("utf-8", "replace")
    except Exception as exc:
        print(f"[epa] download {year} failed: {exc}")
        return None
    rows = [{k: r.get(k) for k in _PBP_COLS} for r in csv.DictReader(io.StringIO(text))]
    util.cache_put(ckey, rows)
    return rows


# ---------------------------------------------------------------------------
# QB value from passing EPA
# ---------------------------------------------------------------------------
def qb_game_starters(year: int) -> dict:
    """Per game, each team's starting QB (most dropbacks) with their EPA/dropback.
    Returns ``{game_id: {week, home, away, teams: {team: {id, name, epa_db, db}}}}``."""
    ckey = f"bet_qb_agg_{year}"
    cached = util.cache_get(ckey, None)
    if cached is not None and year < _current_season():
        return cached
    raw = _download_pbp(year)
    if not raw:
        return cached or {}
    # accumulate per (game, team, passer)
    acc: dict = {}
    for r in raw:
        if r.get("qb_dropback") != "1":
            continue
        pid = r.get("passer_player_id")
        gid = r.get("game_id")
        team = r.get("posteam")
        qe = r.get("qb_epa")
        if not pid or not gid or not team or qe in (None, "", "NA"):
            continue
        try:
            qe = float(qe)
        except ValueError:
            continue
        key = (gid, team, pid)
        a = acc.get(key)
        if a is None:
            a = acc[key] = {"name": r.get("passer_player_name"), "sum": 0.0, "db": 0,
                            "week": _int(r.get("week")),
                            "home": _fix(r.get("home_team")), "away": _fix(r.get("away_team"))}
        a["sum"] += qe
        a["db"] += 1
    # pick each team's primary passer (most dropbacks)
    games: dict = {}
    for (gid, team, pid), a in acc.items():
        g = games.setdefault(gid, {"week": a["week"], "home": a["home"],
                                    "away": a["away"], "teams": {}})
        cur = g["teams"].get(_fix(team))
        if cur is None or a["db"] > cur["db"]:
            g["teams"][_fix(team)] = {"id": pid, "name": a["name"],
                                      "epa_db": a["sum"] / a["db"] if a["db"] else 0.0,
                                      "db": a["db"]}
    util.cache_put(ckey, games)
    return games


class QBModel:
    """Rolling per-QB passing value (EPA/dropback), shrunk toward replacement.

    The team's power rating implicitly assumes its established starter; when a
    *different* QB starts, we shift the team by the value gap in points."""

    REPLACEMENT = -0.10       # EPA/dropback of a replacement-level backup
    SHRINK_DB = 60            # dropbacks of prior weight toward replacement
    DB_PER_GAME = 34.0        # dropbacks/game -> converts EPA/db to points
    MAX_DELTA = 7.0

    def __init__(self, alpha: float = 0.25):
        self.alpha = alpha
        self.value: dict = {}     # qb_id -> smoothed EPA/dropback
        self.db: dict = {}        # qb_id -> total dropbacks seen
        self.team_qb: dict = {}   # team -> established starter id

    def _eff(self, qb_id) -> float:
        """Reliability-shrunk value toward replacement level."""
        if not qb_id:
            return self.REPLACEMENT
        v = self.value.get(qb_id, self.REPLACEMENT)
        n = self.db.get(qb_id, 0)
        return (n * v + self.SHRINK_DB * self.REPLACEMENT) / (n + self.SHRINK_DB)

    def _side_delta(self, team, starter_id) -> float:
        est = self.team_qb.get(team)
        if not starter_id or not est or starter_id == est:
            return 0.0
        gap = (self._eff(starter_id) - self._eff(est)) * self.DB_PER_GAME
        return max(-self.MAX_DELTA, min(self.MAX_DELTA, gap))

    def adjustment(self, home_team, away_team, home_id, away_id) -> float:
        return round(self._side_delta(home_team, home_id) - self._side_delta(away_team, away_id), 2)

    def update(self, rec: dict) -> None:
        """Feed one game's starters (from qb_game_starters)."""
        for team, s in rec.get("teams", {}).items():
            pid = s["id"]
            if not pid:
                continue
            self.value[pid] = ((1 - self.alpha) * self.value.get(pid, s["epa_db"])
                               + self.alpha * s["epa_db"])
            self.db[pid] = self.db.get(pid, 0) + s["db"]
            self.team_qb[team] = pid


def _int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _current_season():
    import datetime as dt
    t = dt.date.today()
    return t.year if t.month >= 9 else t.year - 1


class EPAModel:
    """Opponent-adjusted rolling offense/defense EPA ratings (points/play)."""

    def __init__(self, state: dict | None = None, alpha: float = DEFAULT_ALPHA,
                 hfa: float = DEFAULT_HFA, plays: float = PLAYS_PER_GAME):
        state = state or {}
        self.alpha = state.get("alpha", alpha)
        self.hfa = state.get("hfa", hfa)
        self.plays = state.get("plays", plays)
        # team -> {off, def} in EPA/play above league average (0-centered)
        self.teams: dict = state.get("teams", {})

    def _t(self, team):
        return self.teams.setdefault(team, {"off": 0.0, "def": 0.0, "gp": 0})

    def net(self, team) -> float:
        t = self.teams.get(team)
        return (t["off"] - t["def"]) if t else 0.0

    def margin(self, home, away, neutral=False) -> float:
        h, a = self.teams.get(home), self.teams.get(away)
        if not h or not a:
            return 0.0
        # home offense vs away defense, and vice-versa
        home_epa = h["off"] + a["def"]
        away_epa = a["off"] + h["def"]
        pts = (home_epa - away_epa) * self.plays
        return pts + (0.0 if neutral else self.hfa)

    def update(self, rec: dict) -> None:
        """Update from one game's per-team offensive EPA aggregate."""
        home, away = rec["home"], rec["away"]
        off = rec["off"]
        if home not in off or away not in off:
            return
        h, a = self._t(home), self._t(away)
        # home offense produced off[home] vs away defense; expected = h.off + a.def
        for team, opp, actual in ((h, a, off[home]), (a, h, off[away])):
            exp = team["off"] + opp["def"]
            resid = actual - exp
            team["off"] += self.alpha * resid
            opp["def"] += self.alpha * resid
        h["gp"] += 1
        a["gp"] += 1

    def new_season(self, revert: float = 0.5):
        for t in self.teams.values():
            t["off"] *= (1 - revert)
            t["def"] *= (1 - revert)
            t["gp"] = 0

    def to_state(self):
        return {"alpha": self.alpha, "hfa": self.hfa, "plays": self.plays,
                "teams": self.teams}


# ---------------------------------------------------------------------------
# Live persistence
# ---------------------------------------------------------------------------
def _path(league: str = "nfl"):
    return os.path.join(DATA_DIR, f"epa_{league}.json")


def load(league: str = "nfl") -> EPAModel:
    """Load an EPA model. For CFB, ratings are keyed by CFBD team name and an
    ``id_to_name`` map (ESPN id -> CFBD name) is attached for live lookups."""
    try:
        with open(_path(league), "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        m = EPAModel(); m.id_to_name = {}
        return m
    m = EPAModel(state.get("model", state))       # tolerate old flat format
    m.id_to_name = state.get("id_to_name", {})
    return m


def _save(league: str, m: EPAModel, id_to_name: dict | None = None):
    tmp = _path(league) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"model": m.to_state(), "id_to_name": id_to_name or {}}, fh, indent=2)
    os.replace(tmp, _path(league))


def _qb_path():
    return os.path.join(DATA_DIR, "qb_nfl.json")


def sync_qb(season: int | None = None, back: int = 2) -> dict:
    """Build per-QB passing-EPA values from the last few seasons of PBP and save.

    QB skill carries across seasons, so we fold in ``back`` prior seasons too.
    The values are *descriptive* (shown in the game explanation) -- the backtest
    showed a change-based QB margin adjustment does not improve accuracy."""
    season = season or _current_season()
    qb = QBModel()
    names = {}
    for yr in range(season - back, season + 1):
        st = qb_game_starters(yr)
        for gid in sorted(st, key=lambda k: (st[k]["week"] or 0)):
            qb.update(st[gid])
            for t, s in st[gid]["teams"].items():
                if s.get("id"):
                    names[s["id"]] = s["name"]
    payload = {"value": qb.value, "db": qb.db, "team_qb": qb.team_qb, "names": names}
    tmp = _qb_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, _qb_path())
    return {"season": season, "qbs_rated": len(qb.value)}


def load_qb() -> dict | None:
    try:
        with open(_qb_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def qb_points(qb_state: dict, qb_id: str) -> float:
    """Descriptive: a QB's passing value in points/game (reliability-shrunk)."""
    if not qb_state or not qb_id:
        return 0.0
    v = qb_state.get("value", {}).get(qb_id, QBModel.REPLACEMENT)
    n = qb_state.get("db", {}).get(qb_id, 0)
    eff = (n * v + QBModel.SHRINK_DB * QBModel.REPLACEMENT) / (n + QBModel.SHRINK_DB)
    return round(eff * QBModel.DB_PER_GAME, 1)


def sync(season: int | None = None) -> dict:
    """Rebuild NFL EPA ratings from a season's play-by-play and save them."""
    season = season or _current_season()
    agg = team_game_epa(season)
    m = EPAModel()
    for gid in sorted(agg, key=lambda k: (agg[k]["week"] or 0)):
        m.update(agg[gid])
    _save("nfl", m, {})       # NFL is keyed by abbr; no id map needed
    return {"season": season, "games": len(agg), "teams_rated": len(m.teams)}


def sync_cfb(season: int | None = None, back: int = 1) -> dict:
    """Rebuild CFB EPA ratings from CFBD PPA, keyed by CFBD team name, plus an
    ESPN-id -> CFBD-name map (learned by joining to ESPN games on game id)."""
    from betting import cfbd, history
    if not cfbd.enabled():
        return {"error": "no CFBD key"}
    import datetime as _d
    y = _d.date.today().year
    season = season or (y if _d.date.today().month >= 8 else y - 1)
    m = EPAModel(alpha=CFB_ALPHA, hfa=CFB_HFA, plays=CFB_PLAYS)
    name_to_id: dict = {}
    for i, yr in enumerate(range(season - back, season + 1)):
        if i > 0:
            m.new_season()
        cfbd_games = cfbd.team_game_epa(yr)
        espn = {str(g["id"]): g for g in history.cfb_games([yr], with_lines=False)}
        for gid in sorted(cfbd_games, key=lambda k: (cfbd_games[k]["week"] or 0)):
            rec = cfbd_games[gid]
            m.update(rec)
            eg = espn.get(str(gid))
            if eg:
                name_to_id[rec["home"]] = str(eg["home"]["id"])
                name_to_id[rec["away"]] = str(eg["away"]["id"])
    id_to_name = {v: k for k, v in name_to_id.items()}
    _save("cfb", m, id_to_name)
    return {"season": season, "teams_rated": len(m.teams), "mapped": len(id_to_name)}
