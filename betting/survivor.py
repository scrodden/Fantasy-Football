"""Survivor / Eliminator pool planner.

A survivor pool: each week you pick ONE team to win its game; if it loses you're
out; and you may use each team AT MOST ONCE all season.  Picking this week's
biggest favorite is a trap -- you'll want that team for a week when your options
are thin.  The right question is which *assignment of teams to weeks* keeps you
alive the longest.

We reuse the betting ``Projector`` for a market-anchored win probability on every
remaining game, then solve the season as a max-weight bipartite assignment
(weeks x teams, each team used once) that maximizes total survival probability
-- equivalently, sum of log(win_prob).  That plan tells you what to pick *now*
while reserving strong teams for the weeks you'll need them.  Recompute weekly as
results and lines move.

Free data only: win probabilities come from the same ESPN-odds-anchored model as
the Betting tab; no paid feeds.
"""

from __future__ import annotations

import math

from betting import data
from betting.model import Projector

LAST_REG_WEEK = 18
_NEG = 1e9  # cost for an impossible (team-not-playing) cell


# ---------------------------------------------------------------------------
# Rectangular Hungarian algorithm (Kuhn-Munkres), minimizes total cost.
# rows <= cols.  Returns row -> col assignment (0-indexed).  O(rows^2 * cols).
# ---------------------------------------------------------------------------
def _hungarian(cost: list[list[float]]) -> list[int]:
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)          # p[j] = row assigned to column j (1-indexed)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = -1
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(0, m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    ans = [-1] * n
    for j in range(1, m + 1):
        if p[j] > 0:
            ans[p[j] - 1] = j - 1
    return ans


def _optimal(weeks: list[int], week_prob: dict, teams: list[str]) -> tuple[float, dict]:
    """Best assignment of one distinct team per week. Returns (total_log_prob,
    {week: team}).  `week_prob[w]` is {team_abbr: win_prob} for that week."""
    if not weeks:
        return 0.0, {}
    cost = []
    for w in weeks:
        tp = week_prob[w]
        row = []
        for t in teams:
            wp = tp.get(t)
            row.append(-math.log(max(wp, 1e-6)) if wp is not None else _NEG)
        cost.append(row)
    assign = _hungarian(cost)
    plan, total = {}, 0.0
    for i, w in enumerate(weeks):
        col = assign[i]
        t = teams[col] if 0 <= col < len(teams) else None
        wp = week_prob[w].get(t) if t else None
        if wp is None:                       # infeasible cell -> best free team
            free = {x: week_prob[w][x] for x in week_prob[w] if x in teams and x not in plan.values()}
            if free:
                t = max(free, key=free.get)
                wp = free[t]
        plan[w] = t
        if wp:
            total += math.log(max(wp, 1e-6))
    return total, plan


# ---------------------------------------------------------------------------
# Board build
# ---------------------------------------------------------------------------
def _collect_weeks(proj: Projector):
    """Project every remaining regular-season week once. Returns
    (week_prob, week_games, teams_meta, detected_start)."""
    week_prob: dict = {}
    week_games: dict = {}
    teams_meta: dict = {}
    detected_start = None
    for wk in range(1, LAST_REG_WEEK + 1):
        b = data.scoreboard("nfl", seasontype=2, week=wk)
        tp: dict = {}
        games = []
        any_upcoming = False
        for g in b["games"]:
            ha, aa = g["home"]["abbr"], g["away"]["abbr"]
            for side in ("home", "away"):
                t = g[side]
                teams_meta.setdefault(t["abbr"], {"abbr": t["abbr"], "name": t.get("name"),
                                                  "full": t.get("full"), "logo": t.get("logo")})
            if g["completed"]:
                continue
            any_upcoming = True
            pr = proj.project(g)
            hp, ap = pr["home_win_prob"], pr["away_win_prob"]
            tp[ha], tp[aa] = hp, ap
            games.append({
                "away": aa, "home": ha,
                "away_name": g["away"].get("name"), "home_name": g["home"].get("name"),
                "away_logo": g["away"].get("logo"), "home_logo": g["home"].get("logo"),
                "away_wp": round(ap, 4), "home_wp": round(hp, 4),
                "fav": ha if hp >= ap else aa, "fav_wp": round(max(hp, ap), 4),
                "has_odds": bool(g["espn_odds"]), "date": g["date"],
            })
        week_prob[wk] = tp
        week_games[wk] = games
        if any_upcoming and detected_start is None:
            detected_start = wk
    if detected_start is None:
        detected_start = 1
    return week_prob, week_games, teams_meta, detected_start


def build_survivor(used=None, start_week=None, horizon=None) -> dict:
    used = set((a or "").upper() for a in (used or []))
    proj = Projector("nfl")
    week_prob, week_games, teams_meta, detected_start = _collect_weeks(proj)

    start = int(start_week) if start_week else detected_start
    start = max(1, min(start, LAST_REG_WEEK))
    end = LAST_REG_WEEK
    if horizon:
        end = min(LAST_REG_WEEK, start + int(horizon) - 1)
    weeks = [w for w in range(start, end + 1) if week_prob.get(w)]

    # candidate teams = everything playing in the horizon, minus already-used
    teamset = sorted({t for w in weeks for t in week_prob[w] if t not in used})

    # optimal full-horizon plan
    total_logp, plan_map = _optimal(weeks, week_prob, teamset)
    plan = []
    cum = 1.0
    for w in weeks:
        t = plan_map.get(w)
        wp = week_prob[w].get(t) if t else None
        cum *= (wp or 1.0)
        opp = _opponent(week_games[w], t)
        plan.append({"week": w, "team": t, "win_prob": round(wp, 4) if wp else None,
                     "opp": opp, "cum_survival": round(cum, 4)})

    # this-week options, each scored by season-long survival if forced now
    this_week = weeks[0] if weeks else None
    options = []
    if this_week is not None:
        rest = weeks[1:]
        for t in week_prob[this_week]:
            if t in used or week_prob[this_week][t] is None:
                continue
            sub_teams = [x for x in teamset if x != t]
            sub_total, _ = _optimal(rest, week_prob, sub_teams)
            season_logp = math.log(max(week_prob[this_week][t], 1e-6)) + sub_total
            options.append({
                "team": t, "win_prob": round(week_prob[this_week][t], 4),
                "opp": _opponent(week_games[this_week], t),
                "season_survival": math.exp(season_logp),
            })
        options.sort(key=lambda o: o["season_survival"], reverse=True)
        best = options[0]["season_survival"] if options else 0.0
        for o in options:
            o["cost_vs_best"] = round(best - o["season_survival"], 4)
            o["season_survival"] = round(o["season_survival"], 4)

    return {
        "league": "nfl",
        "detected_week": detected_start,
        "start_week": start,
        "end_week": end,
        "weeks_planned": len(weeks),
        "used": sorted(used),
        "teams": [teams_meta[a] for a in sorted(teams_meta)],
        "weeks": [{"week": w, "games": week_games[w]} for w in weeks],
        "plan": plan,
        "plan_survival": round(math.exp(total_logp), 4) if weeks else None,
        "recommended": options[0] if options else None,
        "this_week": {"week": this_week, "options": options[:14]},
        "model": {"teams_rated": len(proj.elo.teams),
                  "n_games_learned": proj.elo.meta.get("n_games", 0)},
    }


def _opponent(games: list, team: str | None) -> str | None:
    if not team:
        return None
    for g in games:
        if g["home"] == team:
            return "vs " + g["away"]
        if g["away"] == team:
            return "@ " + g["home"]
    return None
