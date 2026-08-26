"""Margin-of-victory-adjusted Elo ratings for NFL and FBS college football.

Elo is the backbone of the projection model: it is naturally recency-weighted
(recent games move a team's rating more than old ones fade) and it updates
cleanly from each week's finals, which is exactly the "learn every week"
behaviour we want.

Design follows the FiveThirtyEight NFL Elo approach:
  * expected result from the rating gap + home-field advantage,
  * a margin-of-victory multiplier so blowouts move ratings more than
    nail-biters, damped for large favorites (auto-correlation guard),
  * a between-season regression of every team toward the mean.

Ratings persist to ``data/elo_<league>.json`` so the model keeps its memory
across restarts.  Points and Elo relate through a per-league conversion so the
same object yields both a win probability and an expected point spread.
"""

from __future__ import annotations

import json
import math
import os

from fantasy import util

DATA_DIR = os.path.join(util.BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Per-league tuning.  Defaults are literature-standard starting points; the
# trainer (train.py) can overwrite these in the saved state as it calibrates
# against accumulated results.
DEFAULTS = {
    "nfl": {
        "k": 20.0,
        "hfa": 48.0,          # Elo points of home edge (~1.9 pts)
        "points_per_elo": 25.0,
        "revert": 0.33,       # fraction reverted toward mean each new season
        "mean": 1500.0,
        "score_sd": 13.2,     # stdev of actual margin vs projection (NFL)
    },
    "cfb": {
        "k": 42.0,            # more teams, larger spread of talent -> bigger K
        "hfa": 65.0,          # college home edge is larger (~2.6 pts)
        "points_per_elo": 27.0,
        "revert": 0.30,
        "mean": 1500.0,
        "score_sd": 16.5,     # college margins are noisier
    },
}


def _state_path(league: str) -> str:
    return os.path.join(DATA_DIR, f"elo_{league}.json")


class EloModel:
    """A league's Elo state plus the update math."""

    def __init__(self, league: str, state: dict | None = None):
        self.league = league
        d = DEFAULTS[league]
        state = state or {}
        params = state.get("params") or {}
        self.k = params.get("k", d["k"])
        self.hfa = params.get("hfa", d["hfa"])
        self.points_per_elo = params.get("points_per_elo", d["points_per_elo"])
        self.revert = params.get("revert", d["revert"])
        self.mean = params.get("mean", d["mean"])
        self.score_sd = params.get("score_sd", d["score_sd"])
        # teams: id -> {rating, name, abbr, gp, last_date}
        self.teams: dict = state.get("teams", {})
        self.meta: dict = state.get("meta", {"last_game_date": None, "season": None,
                                             "n_games": 0})

    # -- persistence --
    @classmethod
    def load(cls, league: str) -> "EloModel":
        try:
            with open(_state_path(league), "r", encoding="utf-8") as fh:
                return cls(league, json.load(fh))
        except (OSError, ValueError):
            return cls(league)

    def save(self) -> None:
        payload = {
            "league": self.league,
            "params": {"k": self.k, "hfa": self.hfa,
                       "points_per_elo": self.points_per_elo,
                       "revert": self.revert, "mean": self.mean,
                       "score_sd": self.score_sd},
            "teams": self.teams,
            "meta": self.meta,
        }
        path = _state_path(self.league)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)

    # -- ratings access --
    def rating(self, team_id: str) -> float:
        t = self.teams.get(str(team_id))
        return t["rating"] if t else self.mean

    def _ensure(self, team: dict) -> dict:
        tid = str(team["id"])
        node = self.teams.get(tid)
        if node is None:
            node = {"rating": self.mean, "name": team.get("name"),
                    "abbr": team.get("abbr"), "gp": 0, "last_date": None}
            self.teams[tid] = node
        else:  # keep display fields fresh
            node["name"] = team.get("name") or node.get("name")
            node["abbr"] = team.get("abbr") or node.get("abbr")
        return node

    # -- prediction --
    def expected_home(self, home_id: str, away_id: str, neutral: bool = False) -> float:
        """Win probability for the home team."""
        hfa = 0.0 if neutral else self.hfa
        diff = self.rating(home_id) + hfa - self.rating(away_id)
        return 1.0 / (1.0 + 10 ** (-diff / 400.0))

    def expected_margin(self, home_id: str, away_id: str, neutral: bool = False) -> float:
        """Projected home margin in points (positive = home favored)."""
        hfa = 0.0 if neutral else self.hfa
        diff = self.rating(home_id) + hfa - self.rating(away_id)
        return diff / self.points_per_elo

    # -- learning --
    @staticmethod
    def _mov_multiplier(margin: float, winner_elo_diff: float) -> float:
        """FiveThirtyEight margin-of-victory multiplier.

        winner_elo_diff = winner_rating - loser_rating (pre-game, +HFA for the
        actual winner side).  Damps rating gains for heavy favorites so a big
        win over a weak team doesn't over-reward.
        """
        return math.log(abs(margin) + 1.0) * (2.2 / (winner_elo_diff * 0.001 + 2.2))

    def update_game(self, game: dict) -> dict:
        """Apply one completed game to the ratings.  Returns the pre-game
        prediction so callers can score calibration.  ``game`` is a normalized
        record from ``betting.data`` with integer home/away scores."""
        home, away = game["home"], game["away"]
        h = self._ensure(home)
        a = self._ensure(away)
        neutral = game.get("neutral", False)
        hfa = 0.0 if neutral else self.hfa

        r_home, r_away = h["rating"], a["rating"]
        exp_home = 1.0 / (1.0 + 10 ** (-(r_home + hfa - r_away) / 400.0))
        pred_margin = (r_home + hfa - r_away) / self.points_per_elo

        hs, as_ = home["score"], away["score"]
        margin = hs - as_
        if margin > 0:
            s_home = 1.0
            winner_diff = (r_home + hfa) - r_away
        elif margin < 0:
            s_home = 0.0
            winner_diff = r_away - (r_home + hfa)
        else:
            s_home = 0.5
            winner_diff = 0.0

        mult = self._mov_multiplier(margin if margin != 0 else 1, winner_diff)
        shift = self.k * mult * (s_home - exp_home)
        h["rating"] = r_home + shift
        a["rating"] = r_away - shift
        for node, tm, dt in ((h, home, game.get("date")), (a, away, game.get("date"))):
            node["gp"] = node.get("gp", 0) + 1
            node["last_date"] = dt
        self.meta["last_game_date"] = game.get("date")
        self.meta["n_games"] = self.meta.get("n_games", 0) + 1

        return {"exp_home_win": exp_home, "pred_margin": pred_margin,
                "actual_margin": margin, "home_win": s_home}

    def new_season(self, season: int) -> None:
        """Revert every team toward the mean at the start of a new season."""
        for node in self.teams.values():
            node["rating"] = self.mean + (1 - self.revert) * (node["rating"] - self.mean)
            node["gp"] = 0
        self.meta["season"] = season

    # -- reporting --
    def rankings(self, limit: int | None = None) -> list[dict]:
        rows = [{"id": tid, "rating": round(n["rating"], 1),
                 "name": n.get("name"), "abbr": n.get("abbr"), "gp": n.get("gp", 0)}
                for tid, n in self.teams.items()]
        rows.sort(key=lambda r: r["rating"], reverse=True)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows[:limit] if limit else rows
