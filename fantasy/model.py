"""Merge player metadata + 5 seasons of RAW stats + RAW projections + injuries + byes.

Scoring is intentionally NOT done here. The server serves raw statistical
categories (yards, TDs, receptions, ...) for each season and for the upcoming
week / full-season projection. The browser computes fantasy points from those
raw numbers using the user's own scoring weights, so any league format can be
supported and toggled live without a round-trip.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from . import sources

REG_SEASON_GAMES = 17

# Raw Sleeper stat keys that any scoring system might use. Only those present
# for a given player/season are stored.
SCORING_RAW_FIELDS = [
    # passing
    "pass_yd", "pass_td", "pass_int", "pass_2pt", "pass_cmp", "pass_att", "pass_sack",
    # rushing
    "rush_yd", "rush_td", "rush_2pt", "rush_att",
    # receiving
    "rec", "rec_yd", "rec_td", "rec_2pt", "rec_tgt",
    # misc offense
    "fum_lost", "fum_rec_td",
    # kicking
    "xpm", "xpa", "fgm", "fga", "fgmiss",
    "fgm_0_19", "fgm_20_29", "fgm_30_39", "fgm_40_49", "fgm_50p",
    # team defense / special teams
    "sack", "int", "fum_rec", "def_td", "safe", "blk_kick", "def_2pt", "ff",
    "pts_allow", "def_st_td", "st_td", "def_st_ff", "def_st_fum_rec",
]

# Ordered (field, label) pairs used only for the season-by-season DISPLAY table.
CATEGORY_FIELDS = [
    ("pass_cmp", "Cmp"), ("pass_att", "Att"), ("pass_yd", "Pass Yd"),
    ("pass_td", "Pass TD"), ("pass_int", "INT"),
    ("rush_att", "Rush"), ("rush_yd", "Rush Yd"), ("rush_td", "Rush TD"),
    ("rec_tgt", "Tgt"), ("rec", "Rec"), ("rec_yd", "Rec Yd"), ("rec_td", "Rec TD"),
    ("fum_lost", "Fum Lost"),
    ("fgm", "FGM"), ("fga", "FGA"), ("xpm", "XPM"),
    ("sack", "Sack"), ("int", "Def INT"), ("ff", "FF"), ("def_td", "Def TD"),
    ("pts_allow", "Pts Allow"),
]


# ---------------------------------------------------------------------------
# Season context
# ---------------------------------------------------------------------------
def season_context(state: dict) -> dict:
    season = int(state.get("season", time.gmtime().tm_year))
    stype = state.get("season_type", "pre")
    week = int(state.get("week", 1) or 1)
    if stype == "regular":
        upcoming_week = week
        remaining = max(0, REG_SEASON_GAMES - (week - 1))
    elif stype == "post":
        upcoming_week = REG_SEASON_GAMES
        remaining = 0
    else:  # pre / off
        upcoming_week = 1
        remaining = REG_SEASON_GAMES
    last_complete = season if stype == "post" else season - 1
    history_years = list(range(last_complete - 4, last_complete + 1))
    return {
        "season": season,
        "season_type": stype,
        "week": week,
        "upcoming_week": upcoming_week,
        "remaining_games": remaining,
        "reg_season_games": REG_SEASON_GAMES,
        "history_years": history_years,
    }


def _num(d: dict, key: str, default=0.0):
    v = d.get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _round(x, n=2):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return 0.0


def _extract_cats(raw: dict) -> dict:
    """Pull the scoring-relevant raw fields that are present."""
    out = {}
    for f in SCORING_RAW_FIELDS:
        if f in raw and raw[f] is not None:
            out[f] = _round(_num(raw, f), 3)
    return out


# ---------------------------------------------------------------------------
# Per-player build
# ---------------------------------------------------------------------------
def _build_history(pid, history_years, stats_by_year):
    """Return list of season rows (most recent first) with raw category stats."""
    rows = []
    for year in sorted(history_years, reverse=True):
        raw = stats_by_year.get(year, {}).get(pid)
        if not raw:
            continue
        gp = _num(raw, "gp") or _num(raw, "gms_active")
        # Skip placeholder / rank-only entries (no games and no points scored).
        if gp <= 0 and _num(raw, "pts_ppr") == 0 and _num(raw, "pts_std") == 0:
            continue
        rows.append({"year": year, "gp": int(gp), "cats": _extract_cats(raw)})
    return rows


_RISK_KEYWORDS = ("out", "doubt", "question", "injured reserve", "reserve",
                  "pup", "susp", "dnr", "physically unable")


def _is_risk(status: str) -> bool:
    s = (status or "").strip().lower()
    if not s:
        return False
    if s in ("na", "ir", "cov"):
        return True
    return any(k in s for k in _RISK_KEYWORDS)


def _player_status(meta, espn_index):
    """Combine Sleeper injury_status (authoritative short code) with ESPN's
    detailed comment on expected availability."""
    status = meta.get("injury_status") or ""
    body = meta.get("injury_body_part") or ""
    name = meta.get("full_name") or f"{meta.get('first_name','')} {meta.get('last_name','')}"
    espn = espn_index.get(sources._norm_name(name), {})
    detail = espn.get("detail", "")
    espn_status = (espn.get("status", "") or "").strip()
    espn_type = espn.get("type", "") or body
    if not status and espn_status and espn_status.lower() not in ("active", "probable"):
        status = espn_status
    return {
        "status": status,
        "body_part": body or espn_type,
        "detail": detail,
        "is_risk": _is_risk(status),
    }


# ---------------------------------------------------------------------------
# Team offensive-line proxies + RB workload
# ---------------------------------------------------------------------------
def _percentile_grades(metric_by_team: dict, higher_is_better: bool) -> dict:
    """Rank teams on a metric and assign A–F grades + a 0-100 score."""
    items = sorted(metric_by_team.items(), key=lambda kv: kv[1], reverse=higher_is_better)
    n = len(items)
    grades = {}
    for rank, (team, _val) in enumerate(items):
        pct = 1.0 - (rank / (n - 1)) if n > 1 else 1.0
        frac = rank / n if n else 1.0
        letter = "A" if frac < 0.2 else "B" if frac < 0.4 else "C" if frac < 0.6 else "D" if frac < 0.8 else "F"
        grades[team] = {"grade": letter, "score": round(pct * 100), "rank": rank + 1, "of": n}
    return grades


def _workload_label(share):
    if share is None:
        return "Unknown"
    if share >= 0.60:
        return "Bellcow (primary back)"
    if share >= 0.45:
        return "Lead back (light committee)"
    if share >= 0.28:
        return "Committee / timeshare"
    return "Rotational / depth"


def _pick_starters(ol_list):
    """Approximate a projected starting five (2 T, 2 G, 1 C) from a team's active
    offensive linemen, preferring the most experienced at each spot. Depth-chart
    order isn't available on free data, so experience is the tie-breaker."""
    active = [p for p in ol_list if (p.get("status") or "").upper() in ("ACT", "")]
    pool = active or ol_list
    by = {"T": [], "G": [], "C": []}
    for p in pool:
        if p["depth"] in by:
            by[p["depth"]].append(p)
    for d in by:
        by[d].sort(key=lambda p: -p["exp"])
    starters = by["T"][:2] + by["G"][:2] + by["C"][:1]
    if len(starters) < 5:
        rest = [p for p in sorted(pool, key=lambda p: -p["exp"]) if p not in starters]
        starters += rest[: 5 - len(starters)]
    return starters[:5]


def _compute_team_context(players_meta, recent_stats, season_proj, season):
    """Return (team_oline, rb_workload_by_pid).

    O-line grades are built from the 2026 line composition: each projected
    starter contributes the pass/run-block percentile of the team he played for
    in 2025, so a rebuilt line reflects its new pieces rather than last year's
    unit. Continuity (how many starters return) is reported alongside.
    """
    t_sack, t_patt = defaultdict(float), defaultdict(float)
    t_rbyd, t_rbatt = defaultdict(float), defaultdict(float)
    t_rbproj = defaultdict(float)
    rb_pids = []
    for pid, meta in players_meta.items():
        tm = meta.get("team")
        if not tm:
            continue
        pos = meta.get("position")
        s = recent_stats.get(pid) or {}
        if pos == "QB":
            t_sack[tm] += _num(s, "pass_sack")
            t_patt[tm] += _num(s, "pass_att")
        elif pos == "RB":
            t_rbyd[tm] += _num(s, "rush_yd")
            t_rbatt[tm] += _num(s, "rush_att")
            t_rbproj[tm] += _num((season_proj.get(pid) or {}), "rush_att")
            rb_pids.append(pid)

    # 2025 team-level line performance -> percentile SCORES (0-100), by team.
    pb_metric = {tm: (t_sack[tm] / (t_patt[tm] + t_sack[tm]) if (t_patt[tm] + t_sack[tm]) else 0.10)
                 for tm in t_patt}
    rb_metric = {tm: (t_rbyd[tm] / t_rbatt[tm] if t_rbatt[tm] else 0.0) for tm in t_rbyd}
    pb_2025 = _percentile_grades(pb_metric, higher_is_better=False)
    rb_2025 = _percentile_grades(rb_metric, higher_is_better=True)

    def score_of(team, grades):
        g = grades.get(team)
        return g["score"] if g else 50  # unknown/rookie prior team -> league average

    # Pull 2026 & 2025 O-line rosters (nflverse). Degrade gracefully if offline.
    try:
        ol26 = sources.get_ol_roster(season)
        ol25 = sources.get_ol_roster(season - 1)
    except Exception:
        ol26, ol25 = {"by_team": {}}, {"team_by_gsis": {}}
    prior_team_by_gsis = ol25.get("team_by_gsis", {})

    # Build per-team projected line + personnel-based scores.
    starters_by_team = {}
    pb_personnel, rb_personnel = {}, {}
    for team, ol_list in ol26.get("by_team", {}).items():
        starters = _pick_starters(ol_list)
        if not starters:
            continue
        pb_vals, rb_vals, disp = [], [], []
        returning = 0
        for st in starters:
            prior = prior_team_by_gsis.get(st["gsis_id"])
            is_returning = prior == team
            if is_returning:
                returning += 1
            pb_vals.append(score_of(prior, pb_2025) if prior else 50)
            rb_vals.append(score_of(prior, rb_2025) if prior else 50)
            disp.append({
                "name": st["name"], "depth": st["depth"], "exp": st["exp"],
                "new": not is_returning,
                "prior_team": None if (is_returning or not prior) else prior,
                "rookie": prior is None and st["exp"] == 0,
            })
        pb_personnel[team] = sum(pb_vals) / len(pb_vals)
        rb_personnel[team] = sum(rb_vals) / len(rb_vals)
        starters_by_team[team] = {
            "starters": disp,
            "returning": returning,
            "continuity_pct": round(returning / len(starters) * 100),
        }

    # Re-rank the personnel scores across the league into A-F grades.
    pb_grades = _percentile_grades(pb_personnel, higher_is_better=True) if pb_personnel else {}
    rb_grades = _percentile_grades(rb_personnel, higher_is_better=True) if rb_personnel else {}

    team_oline = {}
    all_teams = set(list(pb_grades) + list(rb_grades) + list(pb_2025) + list(rb_2025))
    for tm in all_teams:
        info = starters_by_team.get(tm, {})
        # Fall back to the 2025 team grade for teams without roster data.
        team_oline[tm] = {
            "pass_block": pb_grades.get(tm) or pb_2025.get(tm),
            "run_block": rb_grades.get(tm) or rb_2025.get(tm),
            "sacks_allowed_pg": _round(t_sack.get(tm, 0) / REG_SEASON_GAMES, 1),
            "rb_ypc": _round(rb_metric.get(tm, 0), 2),
            "continuity_pct": info.get("continuity_pct"),
            "returning": info.get("returning"),
            "starters": info.get("starters", []),
            "personnel_based": tm in pb_grades,
        }

    # RB workload share = projected carries / team's projected RB carries
    # (fall back to last season's carry share when no projection exists).
    rb_workload = {}
    for pid in rb_pids:
        meta = players_meta[pid]
        tm = meta.get("team")
        proj = _num((season_proj.get(pid) or {}), "rush_att")
        team_proj = t_rbproj.get(tm, 0)
        share = None
        basis = "projected carries"
        if team_proj > 0 and proj > 0:
            share = proj / team_proj
        else:
            ls = _num(recent_stats.get(pid) or {}, "rush_att")
            team_ls = t_rbatt.get(tm, 0)
            if team_ls > 0 and ls > 0:
                share = ls / team_ls
                basis = "last-season carries"
        rb_workload[pid] = {
            "share_pct": round(share * 100) if share is not None else None,
            "label": _workload_label(share),
            "proj_carries": round(proj) if proj else None,
            "basis": basis,
        }
    return team_oline, rb_workload


# ---------------------------------------------------------------------------
# Opponent matchup (defense vs position) tables
# ---------------------------------------------------------------------------
def _matchup_tables(dvp):
    """From DvP data build mult[def][pos] (vs league avg, clamped) and
    rank[def][pos] where rank 1 = toughest defense against that position."""
    per_game = dvp.get("per_game", {})
    league = dvp.get("league", {})
    mult, rank = {}, {}
    for p in sources.MATCH_POSITIONS:
        vals = {d: per_game[d][p] for d in per_game if per_game[d].get(p, 0) > 0}
        if not vals:
            continue
        lg = league.get(p) or 0
        for d, v in vals.items():
            m = (v / lg) if lg else 1.0
            mult.setdefault(d, {})[p] = round(max(0.7, min(1.4, m)), 3)
        order = sorted(vals, key=lambda d: vals[d])  # fewest allowed first = toughest
        n = len(order)
        for i, d in enumerate(order):
            rank.setdefault(d, {})[p] = {"rank": i + 1, "of": n}
    return mult, rank


def _player_usage(raw, team_targets, team_carries, pos):
    """Opportunity/usage profile from last season's stats (snap %, target/carry
    share, red-zone touches, aDOT, air yards)."""
    if not raw:
        return None
    gp = _num(raw, "gp") or _num(raw, "gms_active") or 0
    if gp <= 0:
        return None
    off, tm_off = _num(raw, "off_snp"), _num(raw, "tm_off_snp")
    tgt, carries = _num(raw, "rec_tgt"), _num(raw, "rush_att")
    air = _num(raw, "rec_air_yd")
    rz = _num(raw, "rush_rz_att") + _num(raw, "rec_rz_tgt") + _num(raw, "pass_rz_att")
    u = {
        "gp": int(gp),
        "snap_pct": round(off / tm_off * 100) if tm_off else None,
        "targets": int(tgt) if tgt else 0,
        "tgt_per_g": round(tgt / gp, 1) if tgt else 0,
        "target_share": round(tgt / team_targets * 100, 1) if team_targets else None,
        "carries": int(carries) if carries else 0,
        "carry_per_g": round(carries / gp, 1) if carries else 0,
        "carry_share": round(carries / team_carries * 100, 1) if team_carries else None,
        "rz_touches": int(rz) if rz else 0,
        "rz_per_g": round(rz / gp, 1) if rz else 0,
        "adot": round(air / tgt, 1) if tgt else None,
        "air_yards": int(air) if air else 0,
    }
    return u


def _consistency(pts_list):
    """Floor/ceiling/boom-bust from a player's weekly PPR game log."""
    if not pts_list or len(pts_list) < 3:
        return None
    import statistics
    n = len(pts_list)
    s = sorted(pts_list)
    mean = statistics.fmean(pts_list)
    if mean <= 0:
        return None
    std = statistics.pstdev(pts_list)
    cv = std / mean

    def pct(q):
        return s[min(n - 1, max(0, int(round(q * (n - 1)))))]

    boom = sum(1 for x in pts_list if x >= 1.5 * mean) / n
    bust = sum(1 for x in pts_list if x <= 0.5 * mean) / n
    rating = "Steady" if cv < 0.5 else "Balanced" if cv < 0.85 else "Volatile"
    return {
        "games": n, "ppg": round(mean, 1),
        "floor": round(pct(0.2), 1), "ceiling": round(pct(0.85), 1),
        "cv": round(cv, 2), "rating": rating,
        "boom": round(boom * 100), "bust": round(bust * 100),
    }


def _blend_dvp(prev, cur, prior_weight=6.0):
    """Blend last season's DvP with the in-progress season, weighting the current
    season by how many games each defense has played (prior_weight = pseudo-games
    of last-season data). Early season leans on last year; later, on this year."""
    pg_prev, g_prev = prev.get("per_game", {}), prev.get("games", {})
    pg_cur, g_cur = cur.get("per_game", {}), cur.get("games", {})
    if not pg_cur:
        return prev if pg_prev else {"per_game": {}, "league": {}}
    blended = {}
    for t in set(pg_prev) | set(pg_cur):
        gc = g_cur.get(t, 0)
        row = {}
        for pos in sources.MATCH_POSITIONS:
            cv = pg_cur.get(t, {}).get(pos, 0.0)
            pv = pg_prev.get(t, {}).get(pos, 0.0)
            row[pos] = (gc * cv + prior_weight * pv) / (gc + prior_weight) if (gc > 0 and cv > 0) else pv
        blended[t] = row
    league = {}
    for pos in sources.MATCH_POSITIONS:
        vals = [blended[t][pos] for t in blended if blended[t][pos] > 0]
        league[pos] = sum(vals) / len(vals) if vals else 0.0
    return {"per_game": blended, "league": league}


def _player_matchup(pos, team, sched_season, upcoming_week, mult, rank):
    if pos not in sources.MATCH_POSITIONS:
        return None
    tsched = sched_season.get(team, {})
    # schedule weeks are ints; JSON round-trips dict keys to strings, so accept both.
    def wk(w):
        return tsched.get(w, tsched.get(str(w)))

    def m(o):
        return mult.get(o, {}).get(pos, 1.0) if o else 1.0

    nxt = wk(upcoming_week)
    rem_opps = [wk(w) for w in range(upcoming_week, 19) if wk(w)]
    rem_mults = [m(o) for o in rem_opps]
    ros_mult = round(sum(rem_mults) / len(rem_mults), 3) if rem_mults else 1.0
    # Fantasy-playoff weeks (15-17): schedule strength for the games that win titles.
    p_opps = [wk(w) for w in (15, 16, 17) if wk(w)]
    p_mults = [m(o) for o in p_opps]
    playoff = {"mult": round(sum(p_mults) / len(p_mults), 3) if p_mults else 1.0, "opps": p_opps}
    weeks = [{"week": w, "opp": wk(w), "mult": m(wk(w))} for w in range(upcoming_week, 19) if wk(w)]
    return {
        "pos": pos,
        "next": {"opp": nxt, "mult": m(nxt), "rank": rank.get(nxt, {}).get(pos)} if nxt else None,
        "ros": {"mult": ros_mult, "games": len(rem_opps)},
        "playoff": playoff,
        "weeks": weeks,
    }


# ---------------------------------------------------------------------------
# Dataset assembly (cached in memory)
# ---------------------------------------------------------------------------
_LOCK = threading.Lock()
_CACHE = {"built_at": 0.0, "data": None}
_MEM_TTL = 3 * 3600


def build_dataset(force: bool = False) -> dict:
    with _LOCK:
        fresh = _CACHE["data"] is not None and (time.time() - _CACHE["built_at"]) < _MEM_TTL
        if fresh and not force:
            return _CACHE["data"]
        data = _assemble()
        _CACHE["data"] = data
        _CACHE["built_at"] = time.time()
        return data


def _assemble() -> dict:
    state = sources.get_state()
    ctx = season_context(state)
    players = sources.get_players()
    espn_index = sources.get_injury_index()

    stats_by_year = {}
    for year in ctx["history_years"]:
        stats_by_year[year] = sources.get_season_stats(year, is_current=False)
    # Fold the in-progress season into the projection baseline so rest-of-season
    # updates weekly as games are played (recency weighting makes it dominate).
    hist_years = list(ctx["history_years"])
    if ctx["season_type"] == "regular":
        stats_by_year[ctx["season"]] = sources.get_season_stats(ctx["season"], is_current=True)
        hist_years = hist_years + [ctx["season"]]

    week_proj = sources.get_week_projections(ctx["season"], ctx["upcoming_week"])
    season_proj = sources.get_season_projections(ctx["season"])
    try:
        espn_proj = sources.get_espn_projections(ctx["season"])
    except Exception:
        espn_proj = {}
    try:
        byes = sources.get_bye_weeks(ctx["season"])
    except Exception:
        byes = {}

    recent_stats = stats_by_year.get(ctx["history_years"][-1], {})
    team_oline, rb_workload = _compute_team_context(players, recent_stats, season_proj, ctx["season"])

    # Team totals for usage shares (targets, carries) from last season.
    t_targets, t_carries = defaultdict(float), defaultdict(float)
    for _pid, _meta in players.items():
        _tm = _meta.get("team")
        if not _tm:
            continue
        _s = recent_stats.get(_pid) or {}
        t_targets[_tm] += _num(_s, "rec_tgt")
        t_carries[_tm] += _num(_s, "rush_att")

    is_reg = ctx["season_type"] == "regular"
    # Opponent matchup tables (defense vs position), blending last season with the
    # in-progress season as games are played; + this season's schedule.
    try:
        dvp_prev = sources.get_dvp(ctx["history_years"][-1])
        dvp_cur = sources.get_dvp(ctx["season"], is_current=True) if is_reg else {}
        dvp = _blend_dvp(dvp_prev, dvp_cur)
        schedule = sources.get_schedule()
        mult, rank = _matchup_tables(dvp)
        sched_season = schedule.get(str(ctx["season"]), {})
    except Exception:
        mult, rank, sched_season = {}, {}, {}

    # Consistency (weekly game logs, prior + current season), trending, ADP.
    try:
        idmap = sources.get_id_map([ctx["history_years"][-1], ctx["season"]])
        weekly_prev = sources.get_weekly_points(ctx["history_years"][-1])
        weekly_cur = sources.get_weekly_points(ctx["season"], is_current=True) if is_reg else {}
        sleeper_to_gsis = idmap.get("sleeper_to_gsis", {})
    except Exception:
        weekly_prev, weekly_cur, sleeper_to_gsis = {}, {}, {}
    try:
        trending = sources.get_trending()
    except Exception:
        trending = {}

    out_players = []
    for pid, meta in players.items():
        pos = meta.get("position")
        if pos not in sources.FANTASY_POSITIONS:
            continue
        status_str = (meta.get("status") or "").lower()
        if status_str == "retired":
            continue
        on_roster = bool(meta.get("team"))  # note: can be a stale team for retirees
        history = _build_history(pid, hist_years, stats_by_year)
        # "Recent" means the actual last two completed seasons (plus the current one
        # in progress), not merely the two most-recent seasons a player has data for
        # (which would keep old retirees like Brady, whose newest games are years old).
        recent_years = {ctx["history_years"][-1], ctx["history_years"][-1] - 1, ctx["season"]}
        has_recent = any(r["gp"] > 0 and r["year"] in recent_years for r in history)
        wk = week_proj.get(pid, {})
        sn = season_proj.get(pid, {})
        has_proj = (_num(sn, "pts_ppr") > 0) if sn else False
        newcomer = meta.get("years_exp") in (None, 0, 1)
        # Draftable = actually played in the last two seasons, OR on a roster and
        # either newly in the league or carrying a current-season projection.
        # This drops retirees (no recent games, no projection, veteran) even when
        # the source still lists a stale team for them.
        if not (has_recent or (on_roster and (has_proj or newcomer))):
            continue

        injury = _player_status(meta, espn_index)
        name = meta.get("full_name") or f"{meta.get('first_name','')} {meta.get('last_name','')}".strip()
        team_abbr = meta.get("team") or "FA"
        is_rookie = (meta.get("years_exp") == 0) and not has_recent
        oline = team_oline.get(team_abbr) if pos in ("QB", "RB") else None
        workload = rb_workload.get(pid) if pos == "RB" else None
        matchup = _player_matchup(pos, team_abbr, sched_season, ctx["upcoming_week"], mult, rank)
        _gsis = sleeper_to_gsis.get(pid)
        _logs = ((weekly_prev.get(_gsis) or []) + (weekly_cur.get(_gsis) or [])) if _gsis else None
        consistency = _consistency(_logs)
        usage = _player_usage(recent_stats.get(pid), t_targets.get(team_abbr), t_carries.get(team_abbr), pos)
        adp = {}
        for sc, fld in (("ppr", "adp_ppr"), ("half_ppr", "adp_half_ppr"), ("std", "adp_std")):
            v = _num(sn, fld) if sn else 0
            adp[sc] = v if 0 < v < 900 else None

        out_players.append({
            "id": pid,
            "name": name or pid,
            "position": pos,
            "team": team_abbr,
            "age": meta.get("age"),
            "years_exp": meta.get("years_exp"),
            "number": meta.get("number"),
            "search_rank": meta.get("search_rank") or 999999,
            "bye_week": byes.get(team_abbr),
            "is_rookie": is_rookie,
            "college": meta.get("college"),
            "oline": oline,
            "workload": workload,
            "matchup": matchup,
            "consistency": consistency,
            "usage": usage,
            "adp": adp,
            "trending": trending.get(pid, 0),
            "injury": injury,
            "history": history,
            "proj_week": _extract_cats(wk) if wk else {},
            "proj_season": _extract_cats(sn) if sn else {},
            "proj_season_espn": espn_proj.get(sources._norm_name(name), {}),
        })

    # Default order: by Sleeper search rank (client re-sorts by scored projection).
    out_players.sort(key=lambda p: p["search_rank"])

    return {
        "context": ctx,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "freshness": sources.data_freshness(),
        "count": len(out_players),
        "players": out_players,
    }


def get_player(pid: str):
    data = build_dataset()
    for p in data["players"]:
        if p["id"] == pid:
            return p
    return None
