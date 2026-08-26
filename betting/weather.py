"""Live game-time weather for the projection model (NFL).

Backtests get weather straight from nflverse; upcoming games don't, so here we
fetch a forecast for the home stadium at kickoff from **Open-Meteo** -- free, no
API key, ~16-day horizon.  Indoor/closed-roof stadiums are skipped (weather has
no effect), and results are cached so we hit the API at most once per
stadium-day.

CFB isn't covered (no free stadium-coordinate table for ~130 venues); if that
table appears later this drops in the same way.
"""

from __future__ import annotations

import datetime as _dt

from fantasy import util
from betting import signals

# Roofed NFL venues where outdoor weather doesn't reach the field. Retractable
# roofs (ATL, DAL, HOU, IND, ARI) are treated as closed in bad weather.
NFL_INDOOR = {"ATL", "DAL", "DET", "HOU", "IND", "LV", "LAR", "LAC",
              "MIN", "NO", "ARI"}


def _parse_kick(iso: str):
    if not iso:
        return None
    s = iso.strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def forecast(home_abbr: str, kickoff_iso: str) -> dict | None:
    """Return {roof, temp(F), wind(mph), precip(in)} for a game, or None.

    Indoor venues short-circuit to a no-effect record."""
    if home_abbr in NFL_INDOOR:
        return {"roof": "dome", "temp": None, "wind": None, "precip": 0.0}
    coord = signals.NFL_STADIUM.get(home_abbr)
    kick = _parse_kick(kickoff_iso)
    if not coord or kick is None:
        return None
    # Open-Meteo forecast horizon is ~16 days.
    if not (0 <= (kick.date() - _dt.date.today()).days <= 15):
        return None

    lat, lon = coord[0], coord[1]
    d = kick.strftime("%Y-%m-%d")
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat:.3f}&longitude={lon:.3f}"
           f"&hourly=temperature_2m,wind_speed_10m,precipitation"
           f"&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
           f"&timezone=UTC&start_date={d}&end_date={d}")
    key = f"bet_wx_{home_abbr}_{d}"
    raw = util.cached_fetch(key, url, ttl_seconds=6 * 3600, timeout=30)
    hourly = (raw or {}).get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return None
    # Nearest hour to kickoff.
    target = kick.strftime("%Y-%m-%dT%H:00")
    idx = 0
    best = 1e9
    for i, t in enumerate(times):
        try:
            th = int(t[11:13])
        except (ValueError, IndexError):
            continue
        diff = abs(th - kick.hour)
        if diff < best:
            best, idx = diff, i
    temps = hourly.get("temperature_2m") or []
    winds = hourly.get("wind_speed_10m") or []
    precs = hourly.get("precipitation") or []

    def _at(arr):
        return arr[idx] if idx < len(arr) else None

    return {"roof": "outdoors", "temp": _at(temps), "wind": _at(winds),
            "precip": _at(precs) or 0.0}


def attach_forecasts(board: dict, league: str) -> None:
    """Populate each upcoming NFL game's ``weather`` from the forecast, in place."""
    if league != "nfl":
        return
    for g in board.get("games", []):
        if g.get("completed"):
            continue
        try:
            wx = forecast(g["home"]["abbr"], g.get("date"))
        except Exception:
            wx = None
        if wx:
            g["weather"] = wx
