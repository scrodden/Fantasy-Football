#!/usr/bin/env python3
"""Auction-draft roster optimizer.

Builds season-long projections (same model as the web app), estimates auction
values via value-based drafting, applies a cost rule, and finds the roster that
maximizes total starting-lineup points under a standard Yahoo auction budget.

NOTE ON DATA: Yahoo's actual average auction values (AAV) are not available from
any free/no-auth source, so auction prices here are ESTIMATED with the standard
VBD method (12 teams x $200 = $2,400 distributed by value over replacement).
Swap in real Yahoo AAV by editing load_auction_values() if you can export it.
"""

from __future__ import annotations

import csv
import os
import re
import sys

sys.path.insert(0, ".")
from fantasy import model  # noqa: E402

YAHOO_AAV_FILE = "yahoo_aav.csv"

# ---------------------------------------------------------------------------
# League / draft settings
# ---------------------------------------------------------------------------
TEAMS = 12
BUDGET = 200
# Standard Yahoo starting lineup + bench
STARTERS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}  # + 1 FLEX(RB/WR/TE)
FLEX_ELIGIBLE = ("RB", "WR", "TE")
ROSTER_SIZE = 15  # 9 starters (incl FLEX) + 6 bench
BENCH_SPOTS = ROSTER_SIZE - (sum(STARTERS.values()) + 1)

# Scoring: Yahoo default is close to Half-PPR.
SCORING = {
    "pass_yds_per_pt": 25, "pass_td": 4, "pass_int": -2, "pass_2pt": 2,
    "rush_yds_per_pt": 10, "rush_td": 6, "rush_2pt": 2,
    "ppr": 0.5, "rec_yds_per_pt": 10, "rec_td": 6, "rec_2pt": 2,
    "fum_lost": -2, "misc_td": 6,
    "pat": 1, "fg_0_39": 3, "fg_40_49": 4, "fg_50p": 5, "fg_miss": 0,
    "d_sack": 1, "d_int": 2, "d_fum_rec": 2, "d_td": 6, "d_safe": 2, "d_block": 2, "d_2pt": 2,
}

# Value-over-replacement baselines (rank of the last "startable" player).
REPLACEMENT_RANK = {"QB": 14, "RB": 30, "WR": 36, "TE": 14, "K": 12, "DEF": 12}

MATCH_DAMP = 0.7
NEXT_GAME_MULT = {"Out": 0, "IR": 0, "PUP": 0, "Sus": 0, "DNR": 0, "NA": 0,
                  "Doubtful": 0.25, "Questionable": 0.9, "COV": 0.5}
AVAIL = {"IR": 0.12, "PUP": 0.35, "Sus": 0.55, "DNR": 0.30, "NA": 0.40,
         "Out": 0.92, "Doubtful": 0.95, "Questionable": 1.0, "COV": 0.9}


# ---------------------------------------------------------------------------
# Scoring + projection (mirrors the web app's client-side model)
# ---------------------------------------------------------------------------
def pts_allow_tier(avg):
    if avg <= 0: return 10
    if avg <= 6: return 7
    if avg <= 13: return 4
    if avg <= 20: return 1
    if avg <= 27: return 0
    if avg <= 34: return -1
    return -4


def score_points(cats, games):
    if not cats:
        return 0.0
    s = SCORING
    g = lambda k: cats.get(k, 0) or 0
    pts = 0.0
    pts += (g("pass_yd") / s["pass_yds_per_pt"]) + g("pass_td") * s["pass_td"] + g("pass_int") * s["pass_int"] + g("pass_2pt") * s["pass_2pt"]
    pts += (g("rush_yd") / s["rush_yds_per_pt"]) + g("rush_td") * s["rush_td"] + g("rush_2pt") * s["rush_2pt"]
    pts += g("rec") * s["ppr"] + (g("rec_yd") / s["rec_yds_per_pt"]) + g("rec_td") * s["rec_td"] + g("rec_2pt") * s["rec_2pt"]
    pts += g("fum_lost") * s["fum_lost"] + g("fum_rec_td") * s["misc_td"]
    fgb = g("fgm_0_19") + g("fgm_20_29") + g("fgm_30_39") + g("fgm_40_49") + g("fgm_50p")
    if fgb > 0:
        pts += (g("fgm_0_19") + g("fgm_20_29") + g("fgm_30_39")) * s["fg_0_39"] + g("fgm_40_49") * s["fg_40_49"] + g("fgm_50p") * s["fg_50p"]
    else:
        pts += g("fgm") * s["fg_0_39"]
    pts += g("xpm") * s["pat"] + g("fgmiss") * s["fg_miss"]
    pts += g("sack") * s["d_sack"] + g("int") * s["d_int"] + g("fum_rec") * s["d_fum_rec"]
    pts += (g("def_td") + g("st_td") + g("def_st_td")) * s["d_td"] + g("safe") * s["d_safe"] + g("blk_kick") * s["d_block"] + g("def_2pt") * s["d_2pt"]
    if "pts_allow" in cats and games > 0:
        pts += pts_allow_tier(g("pts_allow") / games) * games
    return pts


def season_points(p, ctx):
    games = ctx.get("reg_season_games", 17)
    played = [(h["gp"], score_points(h["cats"], h["gp"])) for h in p["history"] if h["gp"] > 0]
    baseline = 0.0
    if played:
        wts = list(range(len(played), 0, -1))
        tw = sum(wts)
        baseline = sum(w * (pt / gp) for w, (gp, pt) in zip(wts, played)) / tw
    status = (p.get("injury") or {}).get("status", "") or ""
    sn = score_points(p.get("proj_season") or {}, games)
    mu = p.get("matchup") or {}
    m_ros = (mu.get("ros") or {}).get("mult", 1.0)
    adj_ros = 1 + MATCH_DAMP * (m_ros - 1)
    ros_ppg = (0.5 * (sn / games) + 0.5 * baseline if sn > 0 else baseline) * adj_ros
    remaining = ctx.get("remaining_games", games)
    return ros_ppg * remaining * AVAIL.get(status, 1.0)


# ---------------------------------------------------------------------------
# Auction values (VBD) + cost rule
# ---------------------------------------------------------------------------
def build_pool():
    data = model.build_dataset()
    ctx = data["context"]
    players = []
    for p in data["players"]:
        pos = p["position"]
        if pos not in REPLACEMENT_RANK:
            continue
        pts = season_points(p, ctx)
        if pts <= 0:
            continue
        players.append({"id": p["id"], "name": p["name"], "pos": pos,
                        "team": p["team"], "bye": p.get("bye_week"), "pts": pts})
    return players, ctx


def _norm(name):
    name = (name or "").lower()
    name = re.sub(r"[^a-z ]", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _num_or_none(s):
    raw = re.sub(r"[^0-9.]", "", str(s or ""))
    if not raw or raw == ".":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def load_yahoo_aav():
    """If yahoo_aav.csv exists, return {normalized_name: cost}.

    Handles two layouts: (1) simple -- name and cost on the same row; and
    (2) Yahoo's copy-paste "stacked" export, where the Avg $ sits on a rank
    row and the player name is on the following row.
    """
    if not os.path.exists(YAHOO_AAV_FILE):
        return None
    try:
        with open(YAHOO_AAV_FILE, encoding="utf-8-sig", errors="replace", newline="") as fh:
            rows = list(csv.reader(fh))
    except OSError as exc:
        print(f"[yahoo_aav] could not read {YAHOO_AAV_FILE}: {exc}")
        return None
    if not rows:
        return None

    header = rows[0]
    name_i = next((i for i, c in enumerate(header) if re.search(r"name|player", c or "", re.I)), 0)
    # Prefer an "avg" cost column; otherwise any $/cost/value column.
    cost_i = next((i for i, c in enumerate(header) if re.search(r"avg.*\$|avg.*cost|aav", c or "", re.I)), None)
    if cost_i is None:
        cost_i = next((i for i, c in enumerate(header) if re.search(r"cost|value|price|\$", c or "", re.I)), None)
    if cost_i is None:
        print(f"[yahoo_aav] no cost column found in header: {header}")
        return None

    out = {}
    pending = None
    for row in rows[1:]:
        nm_cell = row[name_i] if len(row) > name_i else ""
        cost_cell = row[cost_i] if len(row) > cost_i else ""
        costval = _num_or_none(cost_cell)
        nm = (nm_cell or "").strip()
        # Ignore "Team - POS" continuation lines.
        looks_team_pos = bool(re.match(r"^[A-Za-z]{2,3}\s*-", nm))
        if nm and costval is not None and not looks_team_pos:
            out[_norm(nm)] = costval          # simple same-row layout
            pending = None
        elif costval is not None:
            pending = costval                  # stacked: stats row carries the $
        elif nm and pending is not None and not looks_team_pos:
            out[_norm(nm)] = pending           # stacked: name row follows
            pending = None
    return out or None


def add_auction_values(players):
    by_pos = {}
    for p in players:
        by_pos.setdefault(p["pos"], []).append(p)
    repl_pts = {}
    for pos, arr in by_pos.items():
        arr.sort(key=lambda x: -x["pts"])
        r = REPLACEMENT_RANK[pos]
        repl_pts[pos] = arr[r - 1]["pts"] if len(arr) >= r else (arr[-1]["pts"] if arr else 0)
    for p in players:
        p["vor"] = max(0.0, p["pts"] - repl_pts[p["pos"]])
    total_vor = sum(p["vor"] for p in players if p["vor"] > 0) or 1.0
    discretionary = TEAMS * BUDGET - TEAMS * ROSTER_SIZE  # each slot min $1
    yahoo = load_yahoo_aav()
    matched = 0
    for p in players:
        est = 1 + (p["vor"] / total_vor) * discretionary if p["vor"] > 0 else 1.0
        p["aav_est"] = max(1, round(est))
        real = yahoo.get(_norm(p["name"])) if yahoo else None
        if real is not None:
            p["aav"] = max(1, round(real))
            p["aav_source"] = "yahoo"
            matched += 1
        else:
            p["aav"] = p["aav_est"]
            p["aav_source"] = "est"
        # Cost rule: +$5 if AAV above $8, else pay AAV.
        p["cost"] = p["aav"] + 5 if p["aav"] > 8 else p["aav"]
    if yahoo:
        print(f"Using REAL Yahoo AAV from {YAHOO_AAV_FILE} ({matched} players matched; "
              f"unmatched fall back to estimate).")
    else:
        print(f"No {YAHOO_AAV_FILE} found -- using ESTIMATED auction values (VBD).")
    return players


# ---------------------------------------------------------------------------
# Optimizer: maximize starters' points under budget (exact via DP per group)
# ---------------------------------------------------------------------------
def _group_curve(cands, count, budget):
    """For choosing EXACTLY `count` players from cands, return
    curve[cost] = (best_pts, chosen_ids) for total cost <= cost."""
    NEG = -1e9
    # dp[j][c] = (pts, picks) best using exactly j players at cost <= c
    dp = [[(NEG, None)] * (budget + 1) for _ in range(count + 1)]
    for c in range(budget + 1):
        dp[0][c] = (0.0, ())
    for item in cands:
        cost, pts, pid = item["cost"], item["pts"], item["id"]
        for j in range(count, 0, -1):
            for c in range(budget, cost - 1, -1):
                prev_pts, prev_picks = dp[j - 1][c - cost]
                if prev_pts > NEG and prev_pts + pts > dp[j][c][0]:
                    dp[j][c] = (prev_pts + pts, prev_picks + (pid,))
    # make monotonic in cost
    curve = [(NEG, None)] * (budget + 1)
    best = (NEG, None)
    for c in range(budget + 1):
        if dp[count][c][0] > best[0]:
            best = dp[count][c]
        curve[c] = best
    return curve


def optimize(players):
    starter_budget = BUDGET - BENCH_SPOTS  # reserve $1 per bench slot
    by_pos = {}
    for p in players:
        by_pos.setdefault(p["pos"], []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: -x["pts"])
    # candidate caps (top players only; rest are $1 bench fodder)
    caps = {"QB": 40, "RB": 60, "WR": 70, "TE": 40, "K": 32, "DEF": 32}
    cand = {pos: by_pos.get(pos, [])[:caps[pos]] for pos in REPLACEMENT_RANK}

    best = None
    for rb, wr, te in [(3, 2, 1), (2, 3, 1), (2, 2, 2)]:  # flex allocation
        need = {"QB": 1, "RB": rb, "WR": wr, "TE": te, "K": 1, "DEF": 1}
        curves = {pos: _group_curve(cand[pos], need[pos], starter_budget) for pos in need}
        # combine groups over budget
        NEG = -1e9
        dp = [(NEG, {}) for _ in range(starter_budget + 1)]
        dp[0] = (0.0, {})
        for pos in need:
            cur = curves[pos]
            ndp = [(NEG, {}) for _ in range(starter_budget + 1)]
            for c in range(starter_budget + 1):
                if dp[c][0] <= NEG:
                    continue
                base_pts, base_sel = dp[c]
                for x in range(0, starter_budget - c + 1):
                    add_pts, add_ids = cur[x]
                    if add_ids is None:
                        continue
                    tot = base_pts + add_pts
                    if tot > ndp[c + x][0]:
                        sel = dict(base_sel); sel[pos] = add_ids
                        ndp[c + x] = (tot, sel)
            dp = ndp
        # best over all costs
        cand_best = max(dp, key=lambda t: t[0])
        if best is None or cand_best[0] > best[0]:
            best = cand_best + ((rb, wr, te),)
    return best, starter_budget


def main():
    print("Building projections and auction values...")
    players, ctx = build_pool()
    add_auction_values(players)
    by_id = {p["id"]: p for p in players}

    result, starter_budget = optimize(players)
    best_pts, sel, flex_alloc = result

    chosen_ids = [pid for ids in sel.values() for pid in ids]
    starters = [by_id[pid] for pid in chosen_ids]
    start_cost = sum(p["cost"] for p in starters)

    # Bench: best remaining players for realistic depth (cap non-flex positions),
    # affordable with leftover budget.
    taken = set(chosen_ids)
    pos_count = {}
    for p in starters:
        pos_count[p["pos"]] = pos_count.get(p["pos"], 0) + 1
    bench_caps = {"QB": 2, "K": 1, "DEF": 1}  # total roster caps for these positions
    pool = sorted((p for p in players if p["id"] not in taken), key=lambda x: -x["pts"])
    bench, spent = [], 0
    for p in pool:
        if len(bench) >= BENCH_SPOTS:
            break
        if pos_count.get(p["pos"], 0) >= bench_caps.get(p["pos"], 99):
            continue  # enough of this position already; keep bench RB/WR/TE-heavy
        slots_left_after = BENCH_SPOTS - len(bench) - 1
        if start_cost + spent + p["cost"] + slots_left_after * 1 <= BUDGET:
            bench.append(p); spent += p["cost"]
            pos_count[p["pos"]] = pos_count.get(p["pos"], 0) + 1

    total_cost = start_cost + spent
    print("\n" + "=" * 74)
    print(f"OPTIMAL AUCTION ROSTER  (Half-PPR, {TEAMS}-team, ${BUDGET} budget)")
    print(f"Flex used on: {'RB' if flex_alloc[0]==3 else 'WR' if flex_alloc[1]==3 else 'TE'}")
    print("=" * 74)
    order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DEF": 5}
    print("\nSTARTERS")
    print(f"  {'Pos':<4}{'Player':<24}{'Tm':<4}{'Bye':<4}{'Proj':>7}{'AAV':>6}{'Cost':>6}")
    for p in sorted(starters, key=lambda x: (order[x["pos"]], -x["pts"])):
        print(f"  {p['pos']:<4}{p['name'][:23]:<24}{p['team']:<4}{str(p['bye'] or '-'):<4}{p['pts']:>7.0f}{p['aav']:>6}{p['cost']:>6}")
    print(f"\n  Starters: {len(starters)} | proj pts {sum(p['pts'] for p in starters):.0f} | cost ${start_cost}")
    print("\nBENCH (depth; leftover budget)")
    for p in sorted(bench, key=lambda x: (order[x["pos"]], -x["pts"])):
        print(f"  {p['pos']:<4}{p['name'][:23]:<24}{p['team']:<4}{str(p['bye'] or '-'):<4}{p['pts']:>7.0f}{p['aav']:>6}{p['cost']:>6}")
    print(f"\n  TOTAL COST: ${total_cost} / ${BUDGET}   (starters ${start_cost} + bench ${spent})")
    print(f"  STARTING-LINEUP PROJECTED POINTS (season): {sum(p['pts'] for p in starters):.0f}")


if __name__ == "__main__":
    main()
