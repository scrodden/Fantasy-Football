"""Extra predictive signals layered on the Elo/pace baseline.

Each is designed to be toggled on/off so the backtester can prove it actually
helps before we trust it:

  * **QB adjustment** -- a rolling value for each quarterback; when the listed
    starter differs from the guy the team's rating was built on (an injury /
    benching), shift the team's strength by the gap.  Backups starting is one
    of the most mispriced situations in football.
  * **Weather** -- wind and cold suppress scoring; knock down the projected
    total for outdoor games (domes/closed roofs unaffected).
  * **Travel / altitude** -- Denver's altitude and long cross-country trips
    nudge the home edge (NFL; free stadium coordinates).
"""

from __future__ import annotations

import math

# NFL stadium coordinates + elevation (ft). Used for travel & altitude.
NFL_STADIUM = {
    "ARI": (33.5277, -112.2626, 1070), "ATL": (33.7554, -84.4008, 1050),
    "BAL": (39.2780, -76.6227, 50), "BUF": (42.7738, -78.7870, 600),
    "CAR": (35.2258, -80.8528, 750), "CHI": (41.8623, -87.6167, 600),
    "CIN": (39.0954, -84.5160, 490), "CLE": (41.5061, -81.6995, 580),
    "DAL": (32.7473, -97.0945, 600), "DEN": (39.7439, -105.0201, 5280),
    "DET": (42.3400, -83.0456, 600), "GB": (44.5013, -88.0622, 640),
    "HOU": (29.6847, -95.4107, 50), "IND": (39.7601, -86.1639, 715),
    "JAX": (30.3239, -81.6373, 20), "KC": (39.0489, -94.4839, 750),
    "LV": (36.0909, -115.1833, 2030), "LAC": (33.9535, -118.3392, 100),
    "LAR": (33.9535, -118.3392, 100), "MIA": (25.9580, -80.2389, 10),
    "MIN": (44.9736, -93.2575, 830), "NE": (42.0909, -71.2643, 290),
    "NO": (29.9511, -90.0812, 3), "NYG": (40.8135, -74.0745, 20),
    "NYJ": (40.8135, -74.0745, 20), "PHI": (39.9008, -75.1675, 40),
    "PIT": (40.4468, -80.0158, 730), "SF": (37.4030, -121.9700, 20),
    "SEA": (47.5952, -122.3316, 20), "TB": (27.9759, -82.5033, 40),
    "TEN": (36.1665, -86.7713, 440), "WSH": (38.9077, -76.8645, 200),
}


def haversine_mi(a, b) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def travel_altitude_adjustment(home_abbr: str, away_abbr: str) -> float:
    """Home-margin points from altitude + the visitor's travel. NFL only."""
    hs = NFL_STADIUM.get(home_abbr)
    as_ = NFL_STADIUM.get(away_abbr)
    if not hs or not as_:
        return 0.0
    adj = 0.0
    # Altitude: real edge only at Denver's extreme; small elsewhere.
    if hs[2] >= 4000:
        adj += 1.2
    elif hs[2] >= 1800:
        adj += 0.3
    # Long trips fatigue the visitor a touch (>1500 mi).
    dist = haversine_mi(hs, as_)
    if dist > 2000:
        adj += 0.6
    elif dist > 1500:
        adj += 0.3
    return round(adj, 2)


def weather_total_adjustment(weather: dict) -> float:
    """Points to add to the projected total (negative = suppress)."""
    if not weather:
        return 0.0
    roof = (weather.get("roof") or "").lower()
    if roof in ("dome", "closed", "retractable"):
        return 0.0
    adj = 0.0
    wind = weather.get("wind")
    if wind is not None and wind > 8:
        adj -= (wind - 8) * 0.35          # wind kills passing/kicking
    temp = weather.get("temp")
    if temp is not None and temp < 32:
        adj -= (32 - temp) * 0.05
    precip = weather.get("precip")
    if precip is not None and precip > 0.03:   # measurable rain/snow
        adj -= min(precip, 0.5) * 4.0
    return round(adj, 2)


# ---------------------------------------------------------------------------
# QB value tracker
# ---------------------------------------------------------------------------
class QBTracker:
    """Rolling per-QB value in points, and each team's current starter."""

    def __init__(self, alpha: float = 0.20):
        self.alpha = alpha
        self.value: dict = {}          # qb key -> points above average
        self.starts: dict = {}         # qb key -> count
        self.team_qb: dict = {}        # team id -> qb key of last start

    def _key(self, qb: dict, side: str):
        return qb.get(side + "_id") or qb.get(side) or None

    def adjustment(self, game: dict) -> float:
        """Home-margin points from a starter differing from the team's usual QB."""
        qb = game.get("qb") or {}
        if not qb:
            return 0.0
        home_id = str(game["home"]["id"]); away_id = str(game["away"]["id"])
        dh = self._side_delta(home_id, self._key(qb, "home"))
        da = self._side_delta(away_id, self._key(qb, "away"))
        return round(dh - da, 2)

    # Trust a QB's value only once it has a few starts; cap the swing.
    RELIABLE_STARTS = 10
    MAX_DELTA = 4.0

    def _rv(self, key) -> float:
        """Reliability-shrunk value (few starts -> pulled toward 0)."""
        v = self.value.get(key, 0.0)
        s = self.starts.get(key, 0)
        return v * min(1.0, s / self.RELIABLE_STARTS)

    def _side_delta(self, team_id: str, starter_key) -> float:
        last = self.team_qb.get(team_id)
        if not starter_key or not last or starter_key == last:
            return 0.0
        d = self._rv(starter_key) - self._rv(last)
        return max(-self.MAX_DELTA, min(self.MAX_DELTA, d))

    def update(self, game: dict, pred_margin: float) -> None:
        qb = game.get("qb") or {}
        if not qb:
            return
        home, away = game["home"], game["away"]
        actual = home["score"] - away["score"]
        resid = actual - pred_margin            # home-relative overperformance
        # Attribute half the residual to each side's QB (offense's share).
        hk = self._key(qb, "home"); ak = self._key(qb, "away")
        if hk:
            self.value[hk] = (1 - self.alpha) * self.value.get(hk, 0.0) + self.alpha * (resid / 2.0)
            self.starts[hk] = self.starts.get(hk, 0) + 1
            self.team_qb[str(home["id"])] = hk
        if ak:
            self.value[ak] = (1 - self.alpha) * self.value.get(ak, 0.0) + self.alpha * (-resid / 2.0)
            self.starts[ak] = self.starts.get(ak, 0) + 1
            self.team_qb[str(away["id"])] = ak
