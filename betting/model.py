"""The projection model: winner, score, and total for every game.

Two learned components, both updated from each week's finals:

  * ``EloModel`` (elo.py) -> the point spread and win probability.
  * ``PaceModel`` (here)  -> opponent-adjusted offensive/defensive scoring,
    which yields the game total.  It also tracks each team's *recent form*
    (how it has performed against its own rating lately) and its last game
    date (to derive rest-day edges).

``Projector`` ties them together.  The projected margin comes from Elo, nudged
by rest and recent form; the projected total comes from the pace model; the two
teams' scores are then the unique pair consistent with that margin and total.
This keeps the winner, the spread, and the total internally coherent.

State persists under ``data/`` so the model's memory survives restarts and
sharpens week over week.
"""

from __future__ import annotations

import json
import os

from fantasy import util
from betting import elo as _elo
from betting import signals as _signals
from betting import epa as _epa

DATA_DIR = _elo.DATA_DIR

# League-average points *per team* per game (seeds the pace model; it adapts).
PACE_DEFAULTS = {
    "nfl": {"avg_pts": 22.5, "alpha": 0.12, "home_pts": 1.0, "form_w": 0.20,
            "rest_per_day": 0.35, "rest_cap": 3.0, "bye_bonus": 1.0},
    "cfb": {"avg_pts": 29.0, "alpha": 0.14, "home_pts": 1.4, "form_w": 0.22,
            "rest_per_day": 0.30, "rest_cap": 4.0, "bye_bonus": 1.2},
}
FORM_WINDOW = 4  # games


def _pace_path(league: str) -> str:
    return os.path.join(DATA_DIR, f"pace_{league}.json")


def _parse_date(iso: str | None):
    if not iso:
        return None
    import datetime as dt
    try:
        return dt.datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


class PaceModel:
    """Opponent-adjusted team scoring, recent form, and rest tracking."""

    def __init__(self, league: str, state: dict | None = None):
        self.league = league
        d = PACE_DEFAULTS[league]
        state = state or {}
        p = state.get("params") or {}
        self.avg_pts = p.get("avg_pts", d["avg_pts"])
        self.alpha = p.get("alpha", d["alpha"])
        self.home_pts = p.get("home_pts", d["home_pts"])
        self.form_w = p.get("form_w", d["form_w"])
        self.rest_per_day = p.get("rest_per_day", d["rest_per_day"])
        self.rest_cap = p.get("rest_cap", d["rest_cap"])
        self.bye_bonus = p.get("bye_bonus", d["bye_bonus"])
        # id -> {off, def, last_date, form:[resid,...], name, abbr}
        self.teams: dict = state.get("teams", {})

    def to_state(self) -> dict:
        return {"params": {"avg_pts": self.avg_pts, "alpha": self.alpha,
                           "home_pts": self.home_pts, "form_w": self.form_w,
                           "rest_per_day": self.rest_per_day,
                           "rest_cap": self.rest_cap, "bye_bonus": self.bye_bonus},
                "teams": self.teams}

    def _ensure(self, team: dict) -> dict:
        tid = str(team["id"])
        node = self.teams.get(tid)
        if node is None:
            node = {"off": 0.0, "def": 0.0, "last_date": None, "form": [],
                    "name": team.get("name"), "abbr": team.get("abbr")}
            self.teams[tid] = node
        else:
            node["name"] = team.get("name") or node.get("name")
            node["abbr"] = team.get("abbr") or node.get("abbr")
        return node

    def team_form(self, team_id: str) -> float:
        node = self.teams.get(str(team_id))
        if not node or not node.get("form"):
            return 0.0
        f = node["form"][-FORM_WINDOW:]
        return sum(f) / len(f)

    def rest_days(self, team_id: str, game_date):
        node = self.teams.get(str(team_id))
        if not node or not node.get("last_date") or game_date is None:
            return None
        prev = _parse_date(node["last_date"])
        if prev is None:
            return None
        return (game_date - prev).days

    def projected_scores(self, home: dict, away: dict, margin: float,
                         neutral: bool = False) -> tuple[float, float]:
        """Total from opponent-adjusted pace; split by the Elo-derived margin."""
        h = self.teams.get(str(home["id"])) or {"off": 0.0, "def": 0.0}
        a = self.teams.get(str(away["id"])) or {"off": 0.0, "def": 0.0}
        home_pts = self.avg_pts + h["off"] + a["def"] + (0 if neutral else self.home_pts)
        away_pts = self.avg_pts + a["off"] + h["def"] - (0 if neutral else self.home_pts)
        total = max(home_pts + away_pts, 10.0)
        # Re-split the total so the margin matches the (richer) projection.
        home_score = (total + margin) / 2.0
        away_score = (total - margin) / 2.0
        return home_score, away_score

    def update_game(self, game: dict, pred_margin: float) -> None:
        home, away = game["home"], game["away"]
        h = self._ensure(home)
        a = self._ensure(away)
        neutral = game.get("neutral", False)
        hp = 0 if neutral else self.home_pts
        hs, as_ = home["score"], away["score"]

        exp_home = self.avg_pts + h["off"] + a["def"] + hp
        exp_away = self.avg_pts + a["off"] + h["def"] - hp
        eh, ea = hs - exp_home, as_ - exp_away
        # Split each residual between the scoring offense and the yielding defense.
        h["off"] += self.alpha * eh
        a["def"] += self.alpha * eh
        a["off"] += self.alpha * ea
        h["def"] += self.alpha * ea

        # Recent form = actual margin above the pre-game projection.
        resid = (hs - as_) - pred_margin
        h.setdefault("form", []).append(resid)
        a.setdefault("form", []).append(-resid)
        h["form"] = h["form"][-FORM_WINDOW:]
        a["form"] = a["form"][-FORM_WINDOW:]
        h["last_date"] = game.get("date")
        a["last_date"] = game.get("date")

    def new_season(self):
        for node in self.teams.values():
            node["off"] *= 0.5
            node["def"] *= 0.5
            node["form"] = []
            node["last_date"] = None


# Default market-anchor schedule per league: (week-1 weight, decay/game, floor).
# College openers are far sharper than a seeded power rating, so anchor CFB
# harder and let it loosen more slowly than the NFL.
_BLEND_DEFAULT = {"nfl": (0.50, 0.045, 0.10), "cfb": (0.68, 0.050, 0.15)}


class Projector:
    """Elo + Pace, combined, with persistence and full-game projection."""

    def __init__(self, league: str, load: bool = True, overrides: dict | None = None):
        self.league = league
        if load:
            self.elo = _elo.EloModel.load(league)
            self.pace = self._load_pace()
        else:  # ephemeral (backtest/calibration) -- fresh, never persisted
            self.elo = _elo.EloModel(league)
            self.pace = PaceModel(league)
        self.blend = list(_BLEND_DEFAULT.get(league, (0.50, 0.045, 0.10)))
        # Signal toggles (calibration/live can enable). Persisted in elo.meta.
        flags = (self.elo.meta.get("signals") if load else {}) or {}
        self.use_qb = flags.get("qb", False)
        self.use_weather = flags.get("weather", False)
        self.use_travel = flags.get("travel", False)
        self.use_epa = flags.get("epa", False)
        self.epa_blend = _epa.BLEND_BY_LEAGUE.get(league, _epa.DEFAULT_BLEND)
        self.qb = _signals.QBTracker()
        # Win-prob calibration (Platt [a,b]); validated to help CFB, and to be
        # a no-op (identity) for NFL, so only CFB stores it.
        self.winprob_platt = (self.elo.meta.get("winprob_platt") if load else None)
        # EPA ratings (NFL: keyed by abbr from PBP; CFB: keyed by CFBD name +
        # an ESPN-id map). Loaded from disk only for the live model.
        self.epa_model = _epa.load(league) if load else None
        # QB passing-EPA values (descriptive context in the explanation; NOT a
        # margin input -- the backtest showed a QB adjustment doesn't help).
        self.qb_state = _epa.load_qb() if (load and league == "nfl") else None
        if overrides:
            self.apply_overrides(overrides)

    @classmethod
    def fresh(cls, league: str, overrides: dict | None = None) -> "Projector":
        """An in-memory Projector with no disk state -- for backtests."""
        return cls(league, load=False, overrides=overrides)

    def apply_overrides(self, ov: dict) -> None:
        """Set calibratable parameters from a flat override dict.
        Keys: elo.{k,hfa,points_per_elo,revert,score_sd},
              pace.{alpha,home_pts,form_w,rest_per_day,rest_cap,bye_bonus,avg_pts},
              blend=[w0,slope,floor]."""
        for k, v in (ov.get("elo") or {}).items():
            setattr(self.elo, k, v)
        for k, v in (ov.get("pace") or {}).items():
            setattr(self.pace, k, v)
        if ov.get("blend"):
            self.blend = list(ov["blend"])
        sig = ov.get("signals") or {}
        if "qb" in sig:
            self.use_qb = sig["qb"]
        if "weather" in sig:
            self.use_weather = sig["weather"]
        if "travel" in sig:
            self.use_travel = sig["travel"]
        if "epa" in sig:
            self.use_epa = sig["epa"]
        if "epa_blend" in ov:
            self.epa_blend = ov["epa_blend"]

    def _load_pace(self) -> PaceModel:
        try:
            with open(_pace_path(self.league), "r", encoding="utf-8") as fh:
                return PaceModel(self.league, json.load(fh))
        except (OSError, ValueError):
            return PaceModel(self.league)

    def save(self):
        self.elo.save()
        path = _pace_path(self.league)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.pace.to_state(), fh, indent=2)
        os.replace(tmp, path)

    # -- learning --
    def process_game(self, game: dict) -> dict:
        """Update both models from one final. Returns the pre-game prediction."""
        pred = self.elo.update_game(game)          # also mutates ratings
        self.pace.update_game(game, pred["pred_margin"])
        self.qb.update(game, pred["pred_margin"])
        return pred

    def new_season(self, season: int):
        self.elo.new_season(season)
        self.pace.new_season()

    # -- projection --
    def _blend_weight(self, gp: int) -> float:
        """How much to anchor to the market. Heavy early (our seeded rating
        shouldn't out-argue a sharp opener), decaying as in-season games
        accumulate and the model earns its confidence."""
        w0, slope, floor = self.blend
        return max(floor, min(w0, w0 - slope * gp))

    def project(self, game: dict, anchor: bool = True) -> dict:
        """Full projection for an upcoming game (no scores needed).

        The pure-model number is anchored toward the market line by a weight
        that shrinks as the season's data grows; both are reported so the edge
        is transparent rather than a black box."""
        home, away = game["home"], game["away"]
        neutral = game.get("neutral", False)
        base_margin = self.elo.expected_margin(home["id"], away["id"], neutral)

        # Richer signals layered on the Elo baseline ----------------------
        gdate = _parse_date(game.get("date"))
        # (1) recent form: teams beating/undershooting their rating lately
        form_edge = (self.pace.team_form(home["id"]) - self.pace.team_form(away["id"]))
        form_adj = self.pace.form_w * form_edge
        # (2) rest-day edge (bye weeks, short weeks)
        rest_adj, rest_note = self._rest_adjustment(home, away, gdate)
        # (2b) extra signals: QB change, altitude/travel
        qb_adj = self.qb.adjustment(game) if self.use_qb else 0.0
        travel_adj = (_signals.travel_altitude_adjustment(home["abbr"], away["abbr"])
                      if (self.use_travel and self.league == "nfl") else 0.0)
        model_margin = base_margin + form_adj + rest_adj + qb_adj + travel_adj
        # (2d) EPA efficiency blend (NFL): supplement Elo with opponent-adjusted
        # EPA/play once ratings exist for both teams.
        epa_adj = 0.0
        if self.use_epa and self.epa_model is not None:
            em = self.epa_model
            hk, ak = self._epa_key(home), self._epa_key(away)
            if hk and ak and em.teams.get(hk) and em.teams.get(ak):
                epa_margin = em.margin(hk, ak, neutral)
                blended = (1 - self.epa_blend) * model_margin + self.epa_blend * epa_margin
                epa_adj = blended - model_margin
                model_margin = blended
        home_score, away_score = self.pace.projected_scores(home, away, model_margin, neutral)
        model_total = home_score + away_score
        # (2c) weather suppresses scoring outdoors
        weather_adj = (_signals.weather_total_adjustment(game.get("weather") or {})
                       if self.use_weather else 0.0)
        model_total += weather_adj

        # (3) market anchoring -------------------------------------------
        # Backtests pass anchor=False: we grade the *pure* model against the
        # closing line, so anchoring to that same line would be circular.
        gp = min((self.elo.teams.get(str(home["id"]), {}) or {}).get("gp", 0),
                 (self.elo.teams.get(str(away["id"]), {}) or {}).get("gp", 0))
        w = self._blend_weight(gp) if anchor else 0.0
        mkt = game.get("espn_odds") or {}
        mkt_spread = mkt.get("home_spread")
        mkt_total = mkt.get("total")
        margin = model_margin
        total = model_total
        blended = False
        if anchor and mkt_spread is not None:
            margin = (1 - w) * model_margin + w * (-mkt_spread)
            blended = True
        if anchor and mkt_total is not None:
            total = (1 - w) * model_total + w * mkt_total
            blended = True

        # Re-derive scores from the (possibly blended) margin & total.
        home_score = (total + margin) / 2.0
        away_score = (total - margin) / 2.0
        win_prob = self._win_prob_from_margin(margin)
        # Gentle Platt recalibration (CFB) so stated probabilities match reality.
        if self.winprob_platt:
            from betting import probcal
            win_prob = probcal.apply_platt(self.winprob_platt, win_prob)
        # Anchor win prob to the de-vigged market moneyline too, so we don't
        # inherit the spread->win-prob conversion's underdog bias.
        hml, aml = mkt.get("home_ml"), mkt.get("away_ml")
        if anchor and hml is not None and aml is not None:
            from betting.edges import american_to_prob, devig_two_way
            fair_home, _ = devig_two_way(american_to_prob(hml), american_to_prob(aml))
            win_prob = (1 - w) * win_prob + w * fair_home
        favored = home if margin >= 0 else away
        return {
            "proj_margin": round(margin, 1),          # home-relative
            "proj_home_score": round(home_score, 1),
            "proj_away_score": round(away_score, 1),
            "proj_total": round(total, 1),
            "home_win_prob": round(win_prob, 4),
            "away_win_prob": round(1 - win_prob, 4),
            "favorite": favored["abbr"],
            "elo_home": round(self.elo.rating(home["id"]), 0),
            "elo_away": round(self.elo.rating(away["id"]), 0),
            "model_margin": round(model_margin, 1),   # pure model, pre-anchor
            "model_total": round(model_total, 1),
            "market_blend": round(w, 2) if blended else 0.0,
            "components": {
                "elo_margin": round(base_margin, 2),
                "form_adj": round(form_adj, 2),
                "rest_adj": round(rest_adj, 2),
                "rest_note": rest_note,
                "qb_adj": round(qb_adj, 2),
                "travel_adj": round(travel_adj, 2),
                "epa_adj": round(epa_adj, 2),
                "weather_adj": round(weather_adj, 2),
                "market_anchor_w": round(w, 2) if blended else 0.0,
            },
        }

    # -- explanation --
    def explain(self, game: dict) -> dict:
        """A full, human-readable breakdown of how the projection was built:
        the Elo gap, home edge, form/rest nudges, market anchoring, the
        pace-based total, the win-probability derivation, and the rationale for
        each flagged bet.  Used by the game-detail view."""
        from betting import edges as _edges
        home, away = game["home"], game["away"]
        neutral = game.get("neutral", False)
        p = self.project(game)
        hid, aid = str(home["id"]), str(away["id"])
        ha, aa = home["abbr"], away["abbr"]

        ranked = self.elo.rankings()
        rank_of = {r["id"]: r["rank"] for r in ranked}
        n = len(ranked)
        eh, ea = round(self.elo.rating(hid)), round(self.elo.rating(aid))
        hfa = 0.0 if neutral else self.elo.hfa
        ppe = self.elo.points_per_elo
        base = p["components"]["elo_margin"]
        form_adj = p["components"]["form_adj"]
        rest_adj = p["components"]["rest_adj"]
        rest_note = p["components"]["rest_note"]
        w = p.get("market_blend", 0.0)
        gp = min((self.elo.teams.get(hid, {}) or {}).get("gp", 0),
                 (self.elo.teams.get(aid, {}) or {}).get("gp", 0))

        market = game.get("espn_odds") or {}
        mkt_spread = market.get("home_spread")
        mkt_total = market.get("total")

        # Pace contributions to the projected total.
        hp = self.pace.teams.get(hid) or {"off": 0.0, "def": 0.0}
        ap = self.pace.teams.get(aid) or {"off": 0.0, "def": 0.0}
        avg = self.pace.avg_pts

        fav = ha if p["proj_margin"] >= 0 else aa
        dog = aa if p["proj_margin"] >= 0 else ha
        by = abs(p["proj_margin"])

        # ---- narrative ------------------------------------------------
        narr = []
        narr.append(
            f"Model favors {fav} by {by:.1f} over {dog}, "
            f"projected {p['proj_away_score']:.0f}–{p['proj_home_score']:.0f} "
            f"({ha} home{' , neutral site' if neutral else ''}). "
            f"Win probability: {ha} {p['home_win_prob']*100:.0f}% / {aa} {p['away_win_prob']*100:.0f}%.")
        narr.append(
            f"Power ratings: {ha} {eh} Elo (#{rank_of.get(hid,'?')} of {n}), "
            f"{aa} {ea} Elo (#{rank_of.get(aid,'?')} of {n}). "
            f"That {eh-ea:+d}-point gap"
            + ("" if neutral else f" plus {hfa:.0f} of home-field edge")
            + f" is about {base:+.1f} points for {ha} (~{ppe:.0f} Elo = 1 point).")
        if abs(form_adj) >= 0.1:
            hf, af = self.pace.team_form(hid), self.pace.team_form(aid)
            better = ha if hf > af else aa
            narr.append(
                f"Recent form nudges it {form_adj:+.1f}: {better} has been beating its "
                f"rating lately ({ha} {hf:+.1f}, {aa} {af:+.1f} vs. expectation over the last few games).")
        if abs(rest_adj) >= 0.1 or rest_note:
            narr.append(f"Rest/schedule: {rest_adj:+.1f} point"
                        + (f" ({rest_note})." if rest_note else "."))
        epa_adj = p["components"].get("epa_adj", 0.0)
        if abs(epa_adj) >= 0.1 and self.epa_model is not None:
            hk, ak = self._epa_key(home), self._epa_key(away)
            hn = self.epa_model.net(hk) * 100 if hk else 0.0
            an = self.epa_model.net(ak) * 100 if ak else 0.0
            src = "EPA/play" if self.league == "nfl" else "PPA/play"
            narr.append(
                f"{'EPA' if self.league=='nfl' else 'PPA'} efficiency shifts it {epa_adj:+.1f}: "
                f"opponent-adjusted {src} rates {ha} {hn:+.1f} and {aa} {an:+.1f} "
                f"(per 100 plays), blended {int(self.epa_blend*100)}% with the power rating.")
        qb_ctx = self._qb_context(home, away)
        if qb_ctx:
            narr.append(qb_ctx)
        if w > 0 and mkt_spread is not None:
            narr.append(
                f"The pure model number is {ha} {p['model_margin']:+.1f}; the market has "
                f"{ha} {-mkt_spread:+.1f}. Because the model is still early in its season "
                f"({gp} game{'s' if gp!=1 else ''} of in-season data), it anchors "
                f"{int(round(w*100))}% toward the sharp market line, giving a final "
                f"{ha} {p['proj_margin']:+.1f}.")
        elif mkt_spread is not None:
            narr.append(f"Final projection: {ha} {p['proj_margin']:+.1f} vs. the market's {ha} {-mkt_spread:+.1f}.")
        # total
        tnarr = (f"Total: pace model has {ha} offense {hp['off']:+.1f} and {aa} offense "
                 f"{ap['off']:+.1f} vs. a {avg:.0f}-point league baseline, for a projected "
                 f"{p['proj_total']:.0f}.")
        wx_adj = p["components"].get("weather_adj", 0.0)
        wx = game.get("weather") or {}
        if wx_adj and wx.get("wind") is not None:
            tnarr += (f" Weather knocks {abs(wx_adj):.1f} off the total "
                      f"({wx['wind']:.0f} mph wind"
                      + (f", {wx['temp']:.0f}°F" if wx.get('temp') is not None else "")
                      + (", precip" if (wx.get('precip') or 0) > 0.03 else "") + ").")
        if mkt_total is not None:
            lean = p["proj_total"] - mkt_total
            tnarr += f" Market total is {mkt_total:g} → model leans {'Over' if lean>0 else 'Under'} by {abs(lean):.1f}."
        narr.append(tnarr)

        # ---- structured factors --------------------------------------
        factors = [
            {"label": "Elo gap + home edge", "value": f"{base:+.1f} pt", "detail":
             f"{ha} {eh} (#{rank_of.get(hid,'?')}) vs {aa} {ea} (#{rank_of.get(aid,'?')})"
             + ("" if neutral else f", +{hfa:.0f} HFA")},
            {"label": "Recent form", "value": f"{form_adj:+.1f} pt",
             "detail": f"{ha} {self.pace.team_form(hid):+.1f} vs {aa} {self.pace.team_form(aid):+.1f} vs. rating"},
            {"label": "Rest / schedule", "value": f"{rest_adj:+.1f} pt", "detail": rest_note or "no meaningful rest edge"},
        ]
        if self.use_epa and self.epa_model is not None and abs(p["components"].get("epa_adj", 0.0)) >= 0.01:
            factors.append({"label": "EPA efficiency", "value": f"{p['components']['epa_adj']:+.1f} pt",
                            "detail": f"opponent-adjusted EPA/play, {int(self.epa_blend*100)}% blend"})
        if abs(p["components"].get("weather_adj", 0.0)) >= 0.1:
            factors.append({"label": "Weather (total)", "value": f"{p['components']['weather_adj']:+.1f} pt",
                            "detail": (f"{(game.get('weather') or {}).get('wind',0):.0f} mph wind")})
        factors.append({"label": "Market anchor", "value": (f"{int(round(w*100))}% to line" if w else "none"),
                        "detail": (f"pure model {ha} {p['model_margin']:+.1f} → final {ha} {p['proj_margin']:+.1f}" if w else "model stands on its own")})

        # ---- bet rationale -------------------------------------------
        g_for_edges = dict(game)
        g_for_edges["home"] = {**home, "_gp": (self.elo.teams.get(hid, {}) or {}).get("gp", 99)}
        g_for_edges["away"] = {**away, "_gp": (self.elo.teams.get(aid, {}) or {}).get("gp", 99)}
        pe = dict(p); pe["_spread_sd"] = self.elo.score_sd
        found = _edges.evaluate(g_for_edges, pe, self.league)
        bet_expl = []
        for e in found:
            if e["market"] == "spread":
                why = (f"We make {e['team']} worth about {e['edge']:.1f} more points than the "
                       f"{e['line']:+g} line — a {e['win_prob']*100:.0f}% cover estimate, "
                       f"{e['ev']*100:+.1f}% expected value at {e['price']}.")
            elif e["market"] == "moneyline":
                why = (f"Our win probability ({e['model_prob']*100:.0f}%) is {e['edge']:.1f} points "
                       f"above the de-vigged market price ({e['fair_prob']*100:.0f}% fair), "
                       f"{e['ev']*100:+.1f}% EV at {e['price']:+d}.")
            else:
                why = (f"Model total vs. line gives {e['edge']:.1f} points of edge, "
                       f"{e['win_prob']*100:.0f}% / {e['ev']*100:+.1f}% EV.")
            bet_expl.append({**e, "why": why})
        if not bet_expl and (mkt_spread is not None):
            bet_expl_note = ("No bet: the model's number is close enough to the market that there's "
                             "no edge worth taking here.")
        else:
            bet_expl_note = None

        return {
            "game_id": game.get("id"),
            "matchup": f"{aa} @ {ha}",
            "projection": p,
            "narrative": narr,
            "factors": factors,
            "bets": bet_expl,
            "bets_note": bet_expl_note,
            "market": {"home_spread": mkt_spread, "total": mkt_total,
                       "home_ml": market.get("home_ml"), "away_ml": market.get("away_ml"),
                       "book": market.get("book")},
            "confidence_note": (
                "Early-season note: with limited in-season data the model leans on last year's "
                "ratings and the market, so treat edges as low-confidence for now."
                if gp < 4 else None),
        }

    def _epa_key(self, team: dict):
        """Team key into the EPA model: abbr for NFL, CFBD name (via the id map)
        for CFB."""
        if self.epa_model is None:
            return None
        if self.league == "nfl":
            return team.get("abbr")
        return (getattr(self.epa_model, "id_to_name", {}) or {}).get(str(team.get("id")))

    def _qb_context(self, home: dict, away: dict) -> str | None:
        """Descriptive QB passing value for each team's expected starter (NFL)."""
        if not self.qb_state:
            return None
        tq = self.qb_state.get("team_qb") or {}
        names = self.qb_state.get("names") or {}
        hq = tq.get(home["abbr"]); aq = tq.get(away["abbr"])
        if not hq and not aq:
            return None

        def _fmt(team, qid):
            if not qid:
                return f"{team['abbr']} QB unknown"
            return f"{team['abbr']} {names.get(qid, 'QB')} {_epa.qb_points(self.qb_state, qid):+.1f} pts/gm"
        return ("Quarterbacks (passing-EPA value, context only — not in the projection): "
                + _fmt(home, hq) + ", " + _fmt(away, aq) + ".")

    def _win_prob_from_margin(self, margin: float) -> float:
        """Cover-of-zero probability from a projected margin, using the
        league's error stdev (a normal-CDF, matching how spreads price wins)."""
        import math
        sd = self.elo.score_sd
        z = margin / (sd if sd else 13.0)
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))

    def _rest_adjustment(self, home: dict, away: dict, gdate):
        if gdate is None:
            return 0.0, None
        rh = self.pace.rest_days(home["id"], gdate)
        ra = self.pace.rest_days(away["id"], gdate)
        if rh is None or ra is None:
            return 0.0, None
        # Cap each side's rest at ~2 weeks (bye) before differencing.
        rh_c, ra_c = min(rh, 14), min(ra, 14)
        diff = rh_c - ra_c
        adj = max(-self.pace.rest_cap, min(self.pace.rest_cap,
                                            diff * self.pace.rest_per_day))
        note = None
        if rh_c >= 13 and ra_c < 9:
            adj += self.pace.bye_bonus
            note = f"{home['abbr']} off a bye"
        elif ra_c >= 13 and rh_c < 9:
            adj -= self.pace.bye_bonus
            note = f"{away['abbr']} off a bye"
        elif abs(diff) >= 3:
            more = home['abbr'] if diff > 0 else away['abbr']
            note = f"{more} +{abs(diff)}d rest"
        return round(adj, 2), note
