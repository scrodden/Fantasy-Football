"""Reusable auction-draft optimizer with per-league presets.

Used by both the web app (POST /api/optimize) and the CLI (auction_optimizer.py).
Projections mirror the client-side model; auction values use real Yahoo AAV when
yahoo_aav.csv is present, otherwise a value-based-drafting estimate.
"""

from __future__ import annotations

import csv
import io
import os
import re
from itertools import combinations_with_replacement

from . import model, util

YAHOO_AAV_FILE = os.path.join(util.BASE_DIR, "yahoo_aav.csv")

# --- scoring presets ---
PPR = {
    "pass_yds_per_pt": 25, "pass_td": 4, "pass_int": -2, "pass_2pt": 2,
    "rush_yds_per_pt": 10, "rush_td": 6, "rush_2pt": 2,
    "ppr": 1, "rec_yds_per_pt": 10, "rec_td": 6, "rec_2pt": 2,
    "fum_lost": -2, "misc_td": 6,
    "pat": 1, "fg_0_39": 3, "fg_40_49": 4, "fg_50p": 5, "fg_miss": 0,
    "d_sack": 1, "d_int": 2, "d_fum_rec": 2, "d_td": 6, "d_safe": 2, "d_block": 2, "d_2pt": 2,
}
JOHNNYV = {
    "pass_yds_per_pt": 20, "pass_td": 5, "pass_int": -2, "pass_2pt": 2,
    "rush_yds_per_pt": 8, "rush_td": 5, "rush_2pt": 2,
    "ppr": 0, "rec_yds_per_pt": 8, "rec_td": 5, "rec_2pt": 2,
    "fum_lost": -2, "misc_td": 5,
    "pat": 1, "fg_0_39": 3, "fg_40_49": 4, "fg_50p": 5, "fg_miss": -1,
    "d_sack": 1, "d_int": 2, "d_fum_rec": 2, "d_td": 5, "d_safe": 2, "d_block": 2, "d_2pt": 2,
}
DENNIS = {
    "pass_yds_per_pt": 25, "pass_td": 6, "pass_int": -1, "pass_2pt": 2,
    "rush_yds_per_pt": 10, "rush_td": 6, "rush_2pt": 2,
    "ppr": 0.5, "rec_yds_per_pt": 10, "rec_td": 6, "rec_2pt": 2,
    "fum_lost": -2, "misc_td": 6,
    "pat": 1, "fg_0_39": 3, "fg_40_49": 4, "fg_50p": 5, "fg_miss": -1,
    "d_sack": 1, "d_int": 2, "d_fum_rec": 2, "d_td": 6, "d_safe": 2, "d_block": 2, "d_2pt": 2,
}

# --- league presets ---
LEAGUES = {
    "standard": {
        "name": "Standard (12-team, PPR)", "teams": 12, "budget": 200, "scoring": PPR, "draft": "auction",
        "start": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1},
        "flex": 1, "flex_pos": ["RB", "WR", "TE"], "bench": 6,
    },
    "johnnyv": {
        "name": "Johnny V League", "teams": 10, "budget": 200, "scoring": JOHNNYV, "draft": "auction",
        "start": {"QB": 1, "RB": 2, "WR": 3, "TE": 0, "K": 1, "DEF": 1},
        "flex": 2, "flex_pos": ["RB", "WR", "TE"], "bench": 7,
    },
    "dennishsieh": {
        "name": "Dennis Hsieh League", "teams": 12, "budget": 200, "scoring": DENNIS, "draft": "snake",
        "start": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1},
        "flex": 1, "flex_pos": ["RB", "WR", "TE"], "bench": 6,
    },
}

REPLACEMENT_RANK = {"QB": 14, "RB": 30, "WR": 36, "TE": 14, "K": 12, "DEF": 12}
MATCH_DAMP = 0.7
NEXT_MULT = model.NEXT_GAME_MULT if hasattr(model, "NEXT_GAME_MULT") else {
    "Out": 0, "IR": 0, "PUP": 0, "Sus": 0, "DNR": 0, "NA": 0, "Doubtful": 0.25, "Questionable": 0.9, "COV": 0.5}
AVAIL = {"IR": 0.12, "PUP": 0.35, "Sus": 0.55, "DNR": 0.30, "NA": 0.40,
         "Out": 0.92, "Doubtful": 0.95, "Questionable": 1.0, "COV": 0.9}


# ---------------------------------------------------------------------------
# Scoring + projection
# ---------------------------------------------------------------------------
def _pts_allow_tier(avg):
    if avg <= 0: return 10
    if avg <= 6: return 7
    if avg <= 13: return 4
    if avg <= 20: return 1
    if avg <= 27: return 0
    if avg <= 34: return -1
    return -4


def score_points(cats, s, games):
    if not cats:
        return 0.0
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
        pts += _pts_allow_tier(g("pts_allow") / games) * games
    return pts


DUR_POS_BASE = {"QB": 0.93, "RB": 0.84, "WR": 0.90, "TE": 0.88, "K": 0.97, "DEF": 1.0}


def _durability(p):
    base = DUR_POS_BASE.get(p.get("position"), 0.9)
    played = [h for h in p.get("history", []) if h.get("gp", 0) > 0]
    rate = base
    if played:
        avg = sum(min(h["gp"], 17) for h in played) / len(played)
        rate = 0.6 * (avg / 17) + 0.4 * base
    age = p.get("age") or 0
    if p.get("position") == "RB" and age >= 28:
        rate -= 0.04 * (age - 27)
    elif age >= 32:
        rate -= 0.02 * (age - 31)
    return max(0.6, min(1.0, rate))


def _season_points(p, ctx, s):
    games = ctx.get("reg_season_games", 17)
    played = [(h["gp"], score_points(h["cats"], s, h["gp"])) for h in p["history"] if h["gp"] > 0]
    baseline = 0.0
    if played:
        wts = list(range(len(played), 0, -1))
        tw = sum(wts)
        baseline = sum(w * (pt / gp) for w, (gp, pt) in zip(wts, played)) / tw
    status = (p.get("injury") or {}).get("status", "") or ""
    sn = score_points(p.get("proj_season") or {}, s, games)
    esp = score_points(p.get("proj_season_espn") or {}, s, games)
    if esp > 0:
        sn = (sn + esp) / 2 if sn > 0 else esp
    mu = p.get("matchup") or {}
    m_ros = (mu.get("ros") or {}).get("mult", 1.0)
    adj_ros = 1 + MATCH_DAMP * (m_ros - 1)
    ros_ppg = (0.5 * (sn / games) + 0.5 * baseline if sn > 0 else baseline) * adj_ros
    rf = 1.0
    if (p.get("team") or "FA") == "FA":
        rf *= 0.15  # unsigned free agent
    bp = ((p.get("injury") or {}).get("body_part") or "").lower()
    if (p.get("injury") or {}).get("is_risk") and any(k in bp for k in ("acl", "achilles", "torn", "ruptur", "lisfranc")):
        rf *= 0.35  # season-affecting injury type
    return ros_ppg * ctx.get("remaining_games", games) * _durability(p) * AVAIL.get(status, 1.0) * rf


# ---------------------------------------------------------------------------
# Auction values (real Yahoo AAV or VBD estimate)
# ---------------------------------------------------------------------------
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
    """Return {normalized_name: cost} from yahoo_aav.csv, or None. Handles both
    a simple name+cost layout and Yahoo's stacked copy-paste export."""
    if not os.path.exists(YAHOO_AAV_FILE):
        return None
    try:
        with open(YAHOO_AAV_FILE, encoding="utf-8-sig", errors="replace", newline="") as fh:
            rows = list(csv.reader(fh))
    except OSError:
        return None
    if not rows:
        return None
    header = rows[0]
    name_i = next((i for i, c in enumerate(header) if re.search(r"name|player", c or "", re.I)), 0)
    cost_i = next((i for i, c in enumerate(header) if re.search(r"avg.*\$|avg.*cost|aav", c or "", re.I)), None)
    if cost_i is None:
        cost_i = next((i for i, c in enumerate(header) if re.search(r"cost|value|price|\$", c or "", re.I)), None)
    if cost_i is None:
        return None
    out, pending = {}, None
    for row in rows[1:]:
        nm = (row[name_i] if len(row) > name_i else "").strip()
        costval = _num_or_none(row[cost_i] if len(row) > cost_i else "")
        looks_team_pos = bool(re.match(r"^[A-Za-z]{2,3}\s*-", nm))
        if nm and costval is not None and not looks_team_pos:
            out[_norm(nm)] = costval; pending = None
        elif costval is not None:
            pending = costval
        elif nm and pending is not None and not looks_team_pos:
            out[_norm(nm)] = pending; pending = None
    return out or None


def _add_values(players, teams, budget, roster_size):
    by_pos = {}
    for p in players:
        by_pos.setdefault(p["pos"], []).append(p)
    repl = {}
    for pos, arr in by_pos.items():
        arr.sort(key=lambda x: -x["pts"])
        r = REPLACEMENT_RANK.get(pos, 12)
        repl[pos] = arr[r - 1]["pts"] if len(arr) >= r else (arr[-1]["pts"] if arr else 0)
    for p in players:
        p["vor"] = max(0.0, p["pts"] - repl.get(p["pos"], 0))
    total_vor = sum(p["vor"] for p in players if p["vor"] > 0) or 1.0
    discretionary = teams * budget - teams * roster_size
    yahoo = load_yahoo_aav()
    matched = 0
    for p in players:
        est = 1 + (p["vor"] / total_vor) * discretionary if p["vor"] > 0 else 1.0
        p["aav_est"] = max(1, round(est))  # model (VBD) value, independent of market
        real = yahoo.get(_norm(p["name"])) if yahoo else None
        if real is not None:
            p["aav"] = max(1, round(real)); matched += 1
        else:
            p["aav"] = p["aav_est"]
        p["cost"] = p["aav"] + 5 if p["aav"] > 8 else p["aav"]
    return ("yahoo" if yahoo else "est"), matched


# ---------------------------------------------------------------------------
# Optimizer (exact DP over budget, enumerating flex allocations)
# ---------------------------------------------------------------------------
def _group_curve(cands, count, budget):
    NEG = -1e9
    dp = [[(NEG, None)] * (budget + 1) for _ in range(count + 1)]
    for c in range(budget + 1):
        dp[0][c] = (0.0, ())
    for item in cands:
        cost, pts, pid = item["cost"], item["pts"], item["id"]
        for j in range(count, 0, -1):
            for c in range(budget, cost - 1, -1):
                pp, pk = dp[j - 1][c - cost]
                if pp > NEG and pp + pts > dp[j][c][0]:
                    dp[j][c] = (pp + pts, pk + (pid,))
    curve = [(NEG, None)] * (budget + 1)
    best = (NEG, None)
    for c in range(budget + 1):
        if dp[count][c][0] > best[0]:
            best = dp[count][c]
        curve[c] = best
    return curve


def _flex_distributions(flex_pos, flex):
    seen, out = set(), []
    for combo in combinations_with_replacement(flex_pos, flex):
        d = {}
        for p in combo:
            d[p] = d.get(p, 0) + 1
        key = tuple(sorted(d.items()))
        if key not in seen:
            seen.add(key); out.append(d)
    return out


def league_values(league_key="johnnyv"):
    """Per-player auction dollar values under a league's scoring: the model's
    value ($ you should pay, via VBD) and the market AAV when available."""
    cfg = LEAGUES.get(league_key) or LEAGUES["standard"]
    scoring = cfg["scoring"] or PPR
    teams, budget = cfg["teams"], cfg["budget"]
    roster_size = sum(cfg["start"].values()) + cfg["flex"] + cfg["bench"]
    data = model.build_dataset()
    ctx = data["context"]
    players = []
    for p in data["players"]:
        if p["position"] not in REPLACEMENT_RANK:
            continue
        pts = _season_points(p, ctx, scoring)
        if pts <= 0:
            continue
        players.append({"id": p["id"], "name": p["name"], "pos": p["position"],
                        "team": p["team"], "pts": pts})
    src, _matched = _add_values(players, teams, budget, roster_size)
    out = [{
        "id": p["id"], "name": p["name"], "pos": p["pos"], "team": p["team"],
        "pts": round(p["pts"]), "model_value": p["aav_est"],
        "market_aav": p["aav"] if src == "yahoo" else None,
    } for p in players]
    out.sort(key=lambda x: -x["model_value"])
    return {"source": src, "league": cfg["name"], "budget": budget, "teams": teams,
            "draft": cfg["draft"], "players": out}


def optimize_roster(league_key="johnnyv"):
    cfg = LEAGUES.get(league_key) or LEAGUES["standard"]
    scoring = cfg["scoring"] or PPR
    teams, budget, bench_spots = cfg["teams"], cfg["budget"], cfg["bench"]
    roster_size = sum(cfg["start"].values()) + cfg["flex"] + bench_spots

    data = model.build_dataset()
    ctx = data["context"]
    players = []
    for p in data["players"]:
        if p["position"] not in REPLACEMENT_RANK:
            continue
        pts = _season_points(p, ctx, scoring)
        if pts <= 0:
            continue
        players.append({"id": p["id"], "name": p["name"], "pos": p["position"],
                        "team": p["team"], "bye": p.get("bye_week"), "pts": pts})
    src, matched = _add_values(players, teams, budget, roster_size)
    by_id = {p["id"]: p for p in players}

    by_pos = {}
    for p in players:
        by_pos.setdefault(p["pos"], []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: -x["pts"])
    caps = {"QB": 40, "RB": 60, "WR": 75, "TE": 40, "K": 32, "DEF": 32}
    cand = {pos: by_pos.get(pos, [])[: caps.get(pos, 40)] for pos in REPLACEMENT_RANK}
    starter_budget = budget - bench_spots
    NEG = -1e9

    best = None  # (pts, selection, need)
    for dist in _flex_distributions(cfg["flex_pos"], cfg["flex"]):
        need = dict(cfg["start"])
        for pos, n in dist.items():
            need[pos] = need.get(pos, 0) + n
        need = {pos: c for pos, c in need.items() if c > 0}
        curves = {pos: _group_curve(cand.get(pos, []), c, starter_budget) for pos, c in need.items()}
        dp = [(NEG, {}) for _ in range(starter_budget + 1)]
        dp[0] = (0.0, {})
        for pos, cur in curves.items():
            ndp = [(NEG, {}) for _ in range(starter_budget + 1)]
            for c in range(starter_budget + 1):
                if dp[c][0] <= NEG:
                    continue
                bp, bs = dp[c]
                for x in range(0, starter_budget - c + 1):
                    ap, aids = cur[x]
                    if aids is None:
                        continue
                    if bp + ap > ndp[c + x][0]:
                        sel = dict(bs); sel[pos] = aids
                        ndp[c + x] = (bp + ap, sel)
            dp = ndp
        cb = max(dp, key=lambda t: t[0])
        if best is None or cb[0] > best[0]:
            best = (cb[0], cb[1], need)

    _pts, sel, need = best
    chosen = [pid for ids in sel.values() for pid in ids]
    taken = set(chosen)

    # assign slots: base positions first, extras -> FLEX
    starters = []
    for pos, ids in sel.items():
        base = cfg["start"].get(pos, 0)
        for i, pid in enumerate(ids):
            slot = pos if i < base else "FLEX"
            starters.append((slot, by_id[pid]))
    slot_order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "FLEX": 4, "K": 5, "DEF": 6}
    starters.sort(key=lambda t: (slot_order.get(t[0], 9), -t[1]["pts"]))
    start_cost = sum(p["cost"] for _s, p in starters)

    # bench: best remaining, position-capped for realism
    pos_count = {}
    for _s, p in starters:
        pos_count[p["pos"]] = pos_count.get(p["pos"], 0) + 1
    bench_caps = {"QB": 2, "K": 1, "DEF": 1}
    pool = sorted((p for p in players if p["id"] not in taken), key=lambda x: -x["pts"])
    bench, spent = [], 0
    for p in pool:
        if len(bench) >= bench_spots:
            break
        if pos_count.get(p["pos"], 0) >= bench_caps.get(p["pos"], 99):
            continue
        if start_cost + spent + p["cost"] + (bench_spots - len(bench) - 1) <= budget:
            bench.append(p); spent += p["cost"]
            pos_count[p["pos"]] = pos_count.get(p["pos"], 0) + 1

    def row(slot, p):
        return {"slot": slot, "id": p["id"], "name": p["name"], "pos": p["pos"],
                "team": p["team"], "bye": p["bye"], "pts": round(p["pts"]),
                "aav": p["aav"], "cost": p["cost"]}

    flex_extra = {pos: need.get(pos, 0) - cfg["start"].get(pos, 0) for pos in cfg["flex_pos"]}
    flex_used = ", ".join(f"{n} {pos}" for pos, n in flex_extra.items() if n > 0) or "—"
    note = (f"Real Yahoo AAV ({matched} matched)." if src == "yahoo"
            else "Estimated auction values (VBD) — add yahoo_aav.csv for real prices.")
    note += f" Scoring: {cfg['name']}. Cost rule: AAV + $5 when AAV > $8."
    return {
        "league": cfg["name"], "budget": budget, "teams": teams,
        "aav_source": src,
        "starters": [row(s, p) for s, p in starters],
        "bench": [row("BN", p) for p in sorted(bench, key=lambda x: (slot_order.get(x["pos"], 9), -x["pts"]))],
        "start_cost": start_cost, "bench_cost": spent, "total_cost": start_cost + spent,
        "starter_points": sum(p["pts"] for _s, p in starters),
        "flex_used": flex_used, "note": note,
    }
