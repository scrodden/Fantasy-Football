"""Season-long strategy tracker with honest, locked-in bets.

Three flat-$100 strategies are graded week by week over the season:

  1. ``spread``     -- bet our ATS pick on **every** game.
  2. ``moneyline``  -- bet our predicted winner on **every** game.
  3. ``value``      -- bet each spread/moneyline the model flags as good value
                       ("high conviction").

The integrity rule the user asked for: **a game's bets are locked ~24h before
its kickoff**, using the model and the line as they stand at that moment, and
are never revised afterward.  A game the app never saw inside that window simply
goes unbet (honest -- we can't retro-fit a bet we never placed).

Everything persists to ``data/strategies_<league>.json``.  The locking and
settling run automatically from a background thread while the app is up, on
every board view, and on "Update from results"; they're also safe to call by
hand and are fully idempotent.
"""

from __future__ import annotations

import datetime as _dt
import json
import os

from betting import data, edges
from betting.model import Projector
from betting.elo import DATA_DIR

LOCK_HOURS = 30                 # lock a game's bets up to this long before kickoff
                                # (widened from 24 so flaky cloud scheduling still
                                # catches every game inside its window; the model
                                # + line are captured at first run within it)
STAKE = 100.0                   # flat stake per bet (strategies 1-3)
STARTING_BANKROLL = 10000.0     # strategy 4: grows/shrinks with Kelly-sized bets
KELLY_MIN_STAKE = 1.0           # don't place a Kelly bet smaller than this
STRATEGIES = ("spread", "moneyline", "value", "bankroll")
STRATEGY_LABEL = {
    "spread": "Spread — every game",
    "moneyline": "Moneyline — every game",
    "value": "High conviction (value)",
    "bankroll": "Bankroll — ¼-Kelly on every edge ($10k start)",
}


def _path(league: str) -> str:
    return os.path.join(DATA_DIR, f"strategies_{league}.json")


def _load(league: str) -> dict:
    try:
        with open(_path(league), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"locks": {}}


def _save(league: str, obj: dict) -> None:
    tmp = _path(league) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, _path(league))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def _parse_dt(iso: str):
    if not iso:
        return None
    s = iso.strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        try:
            d = _dt.datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------
def _profit(stake: float, price) -> float:
    price = price if price is not None else -110
    return round(stake * (edges.american_to_decimal(int(price)) - 1), 2)


def _grade_spread(side: str, line: float, price, hs: int, as_: int):
    margin = hs - as_                       # home minus away
    diff = (margin + line) if side == "home" else (-margin + line)
    if abs(diff) < 1e-9:
        return "push", 0.0
    return ("win", _profit(STAKE, price)) if diff > 0 else ("loss", -STAKE)


def _grade_ml(side: str, price, hs: int, as_: int):
    margin = hs - as_
    if margin == 0:
        return "push", 0.0
    win = (margin > 0) == (side == "home")
    return ("win", _profit(STAKE, price)) if win else ("loss", -STAKE)


def _grade_bet(bet: dict, hs: int, as_: int):
    """Grade a bet of any market at its OWN stake (used for Kelly bets)."""
    stake = bet.get("stake", STAKE)
    price = bet.get("price") if bet.get("price") is not None else -110
    m = bet["market"]
    if m == "total":
        total = hs + as_
        line = bet.get("line")
        if line is None:
            return "void", 0.0
        if abs(total - line) < 1e-9:
            return "push", 0.0
        win = (total > line) == (bet["side"] == "over")
    elif m == "spread":
        margin = hs - as_
        diff = (margin + bet["line"]) if bet["side"] == "home" else (-margin + bet["line"])
        if abs(diff) < 1e-9:
            return "push", 0.0
        win = diff > 0
    else:  # moneyline
        margin = hs - as_
        if margin == 0:
            return "push", 0.0
        win = (margin > 0) == (bet["side"] == "home")
    return ("win", _profit(stake, price)) if win else ("loss", -stake)


# ---------------------------------------------------------------------------
# Building a game's bets at lock time
# ---------------------------------------------------------------------------
def _bets_for_game(g: dict, proj: dict, league: str) -> dict:
    market = g.get("espn_odds") or {}
    home, away = g["home"], g["away"]
    bets = {"spread": None, "moneyline": None, "value": []}

    # 1) Spread: our ATS lean, at the game's spread number (-110 assumed).
    hs = market.get("home_spread")
    if hs is not None:
        lean = proj["proj_margin"] + hs        # >0 => home covers
        if abs(lean) > 1e-6:
            if lean > 0:
                side, team, number = "home", home["abbr"], hs
            else:
                side, team, number = "away", away["abbr"], -hs
            bets["spread"] = {"market": "spread", "side": side, "team": team, "line": number,
                              "price": -110, "stake": STAKE,
                              "pick": f"{team} {number:+g}", "lean": round(lean, 2)}

    # 2) Moneyline: our predicted winner at its price.
    hml, aml = market.get("home_ml"), market.get("away_ml")
    if proj["proj_margin"] > 0 and hml is not None:
        bets["moneyline"] = {"market": "moneyline", "side": "home", "team": home["abbr"],
                             "price": hml, "stake": STAKE, "pick": f"{home['abbr']} ML {hml:+d}"}
    elif proj["proj_margin"] < 0 and aml is not None:
        bets["moneyline"] = {"market": "moneyline", "side": "away", "team": away["abbr"],
                             "price": aml, "stake": STAKE, "pick": f"{away['abbr']} ML {aml:+d}"}

    # 3) Value: the model's flagged spread/moneyline edges (skip totals).
    # 4) Bankroll: EVERY flagged edge (spread/total/moneyline), Kelly-sized later.
    bets["bankroll"] = []
    for e in edges.evaluate(g, proj, league):
        entry = {
            "market": e["market"], "side": e["side"], "team": e.get("team"),
            "pick": e["pick"], "line": e.get("line"),
            "price": e.get("price") if e.get("price") is not None else -110,
            "ev": e["ev"], "edge": e["edge"], "kelly": e.get("kelly", 0.0),
            "confidence": e["confidence"], "book": e.get("book"),
        }
        if e["market"] in ("spread", "moneyline"):
            bets["value"].append({**entry, "stake": STAKE})
        # Kelly stake filled in at lock time from the current bankroll.
        if e.get("kelly", 0) > 0:
            bets["bankroll"].append(dict(entry))
    return bets


def _has_any_bet(bets: dict) -> bool:
    return bool(bets["spread"] or bets["moneyline"] or bets["value"] or bets.get("bankroll"))


def current_bankroll(ledger: dict) -> float:
    """$10,000 plus the net P&L of every already-settled Kelly bet."""
    pnl = 0.0
    for lk in ledger["locks"].values():
        if lk.get("settled"):
            for b in lk["bets"].get("bankroll", []):
                pnl += b.get("pnl", 0.0)
    return STARTING_BANKROLL + pnl


# ---------------------------------------------------------------------------
# Lock: freeze bets ~24h before kickoff
# ---------------------------------------------------------------------------
def lock_due_bets(league: str, hours: int = LOCK_HOURS) -> dict:
    """Lock any not-yet-locked game whose kickoff is within ``hours`` (and not
    yet started).  Best-effort; safe to call repeatedly."""
    proj = Projector(league)
    if proj.elo.meta.get("n_games", 0) == 0:
        return {"locked": 0, "reason": "model not seeded"}

    from betting import train
    board = train._relevant_board(league)
    board = data.attach_multibook(board, league)
    ledger = _load(league)
    locks = ledger["locks"]
    now = _now()
    horizon = now + _dt.timedelta(hours=hours)

    locked = 0
    for g in board["games"]:
        gid = g["id"]
        if gid in locks or g["completed"]:
            continue
        ko = _parse_dt(g["date"])
        if ko is None:
            continue
        # Only within the final `hours` before kickoff, and not already started.
        if not (now <= ko <= horizon):
            continue
        proj.elo  # (ratings already loaded)
        g["home"]["_gp"] = (proj.elo.teams.get(str(g["home"]["id"]), {}) or {}).get("gp", 0)
        g["away"]["_gp"] = (proj.elo.teams.get(str(g["away"]["id"]), {}) or {}).get("gp", 0)
        p = proj.project(g)
        p["_spread_sd"] = proj.elo.score_sd
        bets = _bets_for_game(g, p, league)
        # Kelly-size the bankroll bets from the bankroll as it stands now.
        bankroll = current_bankroll(ledger)
        sized = []
        for b in bets.get("bankroll", []):
            stake = round(b["kelly"] * bankroll, 2)
            if stake >= KELLY_MIN_STAKE:
                sized.append({**b, "stake": stake, "bankroll_at_bet": round(bankroll, 2)})
        bets["bankroll"] = sized
        if not _has_any_bet(bets):
            continue
        locks[gid] = {
            "game_id": gid, "league": league,
            "season": board.get("year"), "week": board.get("week"),
            "kickoff": g["date"], "locked_at": now.isoformat(timespec="seconds"),
            "home": g["home"]["abbr"], "away": g["away"]["abbr"],
            "home_full": g["home"]["full"], "away_full": g["away"]["full"],
            "line": {"home_spread": (g["espn_odds"] or {}).get("home_spread"),
                     "total": (g["espn_odds"] or {}).get("total"),
                     "home_ml": (g["espn_odds"] or {}).get("home_ml"),
                     "away_ml": (g["espn_odds"] or {}).get("away_ml"),
                     "book": (g["espn_odds"] or {}).get("book")},
            "projection": {"proj_margin": p["proj_margin"], "proj_total": p["proj_total"],
                           "proj_home_score": p["proj_home_score"],
                           "proj_away_score": p["proj_away_score"],
                           "home_win_prob": p["home_win_prob"]},
            "bets": bets,
            "settled": False,
        }
        locked += 1

    if locked:
        _save(league, ledger)
    return {"locked": locked, "total_locks": len(locks)}


# ---------------------------------------------------------------------------
# Settle: grade locked bets once their games finish
# ---------------------------------------------------------------------------
def settle(league: str, since_days: int = 12) -> dict:
    ledger = _load(league)
    locks = ledger["locks"]
    pending = [l for l in locks.values() if not l.get("settled")]
    if not pending:
        return {"settled": 0}

    today = _dt.date.today()
    finals = data.historical_games(league, today - _dt.timedelta(days=since_days), today)
    finals_by_id = {g["id"]: g for g in finals}

    settled = 0
    for lk in pending:
        g = finals_by_id.get(lk["game_id"])
        if not g or g["home"]["score"] is None:
            continue
        hs, as_ = g["home"]["score"], g["away"]["score"]
        lk["home_score"], lk["away_score"] = hs, as_
        b = lk["bets"]
        if b.get("spread"):
            res, pnl = _grade_spread(b["spread"]["side"], b["spread"]["line"],
                                     b["spread"]["price"], hs, as_)
            b["spread"]["result"], b["spread"]["pnl"] = res, pnl
        if b.get("moneyline"):
            res, pnl = _grade_ml(b["moneyline"]["side"], b["moneyline"]["price"], hs, as_)
            b["moneyline"]["result"], b["moneyline"]["pnl"] = res, pnl
        for v in b.get("value", []):
            if v["market"] == "spread":
                res, pnl = _grade_spread(v["side"], v["line"], v["price"], hs, as_)
            else:
                res, pnl = _grade_ml(v["side"], v["price"], hs, as_)
            v["result"], v["pnl"] = res, pnl
        for v in b.get("bankroll", []):     # Kelly bets, own stake, any market
            v["result"], v["pnl"] = _grade_bet(v, hs, as_)
        lk["settled"] = True
        settled += 1

    if settled:
        _save(league, ledger)
    return {"settled": settled}


# ---------------------------------------------------------------------------
# Report: weekly + cumulative performance of each strategy
# ---------------------------------------------------------------------------
def _blank_bucket():
    return {"bets": [], "n": 0, "win": 0, "loss": 0, "push": 0, "pending": 0,
            "staked": 0.0, "profit": 0.0}


def _add_bet(bucket, bet, meta):
    row = dict(bet)
    row.update(meta)
    bucket["bets"].append(row)
    if bet.get("result") in ("win", "loss", "push"):
        bucket["n"] += 1
        bucket[bet["result"]] += 1
        bucket["staked"] += bet.get("stake", STAKE)
        bucket["profit"] += bet.get("pnl", 0.0)
    else:
        bucket["pending"] += 1


def _finalize(bucket):
    decided = bucket["win"] + bucket["loss"]
    bucket["profit"] = round(bucket["profit"], 2)
    bucket["staked"] = round(bucket["staked"], 2)
    bucket["roi"] = round(100.0 * bucket["profit"] / bucket["staked"], 1) if bucket["staked"] else None
    bucket["record"] = f"{bucket['win']}-{bucket['loss']}" + (f"-{bucket['push']}" if bucket["push"] else "")
    bucket["win_pct"] = round(100.0 * bucket["win"] / decided, 1) if decided else None
    return bucket


def report(league: str) -> dict:
    ledger = _load(league)
    locks = list(ledger["locks"].values())
    # weeks[week][strategy] = bucket
    weeks: dict = {}
    seasons = set()

    def wk_bucket(week, strat):
        w = weeks.setdefault(week, {s: _blank_bucket() for s in STRATEGIES})
        return w[strat]

    for lk in locks:
        wk = lk.get("week")
        seasons.add(lk.get("season"))
        meta = {"game": f"{lk['away']}@{lk['home']}", "game_id": lk["game_id"],
                "kickoff": lk.get("kickoff"), "week": wk,
                "score": (f"{lk['away_score']}-{lk['home_score']}"
                          if lk.get("settled") else None)}
        b = lk["bets"]
        if b.get("spread"):
            _add_bet(wk_bucket(wk, "spread"), b["spread"], meta)
        if b.get("moneyline"):
            _add_bet(wk_bucket(wk, "moneyline"), b["moneyline"], meta)
        for v in b.get("value", []):
            _add_bet(wk_bucket(wk, "value"), v, meta)
        for v in b.get("bankroll", []):
            _add_bet(wk_bucket(wk, "bankroll"), v, meta)

    # Order weeks, finalize buckets, and build cumulative curves per strategy.
    week_nums = sorted([w for w in weeks if w is not None])
    out_weeks = []
    cum = {s: 0.0 for s in STRATEGIES}
    totals = {s: _blank_bucket() for s in STRATEGIES}
    curves = {s: [] for s in STRATEGIES}
    for wk in week_nums:
        row = {"week": wk, "strategies": {}}
        for s in STRATEGIES:
            bkt = _finalize(weeks[wk][s])
            cum[s] += bkt["profit"]
            # Every strategy is tracked as a $10,000 bankroll (flat $100 stakes
            # for 1-3; compounding Kelly for the 4th).
            row["strategies"][s] = {**bkt, "cumulative": round(cum[s], 2),
                                    "bankroll_value": round(STARTING_BANKROLL + cum[s], 2)}
            curves[s].append({"week": wk, "cumulative": round(cum[s], 2), "profit": bkt["profit"],
                              "value": round(STARTING_BANKROLL + cum[s], 2)})
            # accumulate season totals
            t = totals[s]
            for k in ("win", "loss", "push", "pending"):
                t[k] += bkt[k]
            t["n"] += bkt["n"]
            t["staked"] += bkt["staked"]
            t["profit"] += bkt["profit"]
            t["bets"].extend(bkt["bets"])
        out_weeks.append(row)

    for s in STRATEGIES:
        _finalize(totals[s])
        totals[s]["bankroll_value"] = round(STARTING_BANKROLL + totals[s]["profit"], 2)
        totals[s]["return_pct"] = round(100.0 * totals[s]["profit"] / STARTING_BANKROLL, 1)

    # The most recent week that has any graded (settled) bets -- the "how did
    # this week's strategies do" summary the user sees after a slate finishes.
    latest_week = None
    for row in reversed(out_weeks):
        if any(row["strategies"][s]["n"] > 0 for s in STRATEGIES):
            latest_week = {"week": row["week"], "strategies": {
                s: {k: row["strategies"][s].get(k) for k in
                    ("record", "profit", "roi", "win_pct", "n", "pending", "bankroll_value")}
                for s in STRATEGIES}}
            # combined line across the three flat strategies (not the bankroll one)
            allb = [b for s in ("spread", "moneyline", "value") for b in row["strategies"][s]["bets"]]
            wins = sum(1 for b in allb if b.get("result") == "win")
            losses = sum(1 for b in allb if b.get("result") == "loss")
            pushes = sum(1 for b in allb if b.get("result") == "push")
            profit = round(sum(b.get("pnl", 0.0) for b in allb if b.get("result") in ("win", "loss", "push")), 2)
            staked = STAKE * (wins + losses)
            latest_week["combined"] = {
                "record": f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
                "profit": profit, "bets": wins + losses + pushes,
                "roi": round(100 * profit / staked, 1) if staked else None}
            break

    return {
        "league": league, "label": data.LEAGUE_LABEL[league],
        "season": (sorted(s for s in seasons if s) or [None])[-1],
        "strategy_labels": STRATEGY_LABEL,
        "weeks": out_weeks,
        "latest_week": latest_week,
        "totals": totals,
        "curves": curves,
        "n_locks": len(locks),
        "n_settled": sum(1 for l in locks if l.get("settled")),
        "lock_hours": LOCK_HOURS,
        "stake": STAKE,
        "starting_bankroll": STARTING_BANKROLL,
        "bankroll_value": round(STARTING_BANKROLL + totals["bankroll"]["profit"], 2),
        "bankroll_return_pct": round(100.0 * totals["bankroll"]["profit"] / STARTING_BANKROLL, 1),
        "bankroll_pending": totals["bankroll"]["pending"],
    }


def maintain(league: str) -> dict:
    """One call that both locks due bets and settles finished ones."""
    lock_res = lock_due_bets(league)
    settle_res = settle(league)
    return {"lock": lock_res, "settle": settle_res}
