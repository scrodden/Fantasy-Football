"""HTTP + disk-cache helpers built entirely on the Python standard library."""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
WEB_DIR = os.path.join(BASE_DIR, "web")

os.makedirs(CACHE_DIR, exist_ok=True)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FantasyFootballAssistant/1.0 "
    "(+local research tool)"
)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def fetch_json(url: str, timeout: int = 45, retries: int = 3, headers: dict | None = None):
    """GET a URL and parse JSON. Retries with backoff. Returns None on failure."""
    last_err = None
    base_headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    if headers:
        base_headers.update(headers)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=base_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8", errors="replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    print(f"[fetch_json] giving up on {url}: {last_err}")
    return None


def fetch_text(url: str, timeout: int = 60, retries: int = 3):
    """GET a URL and return decoded text (handles gzip). None on failure."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    print(f"[fetch_text] giving up on {url}: {last_err}")
    return None


# ---------------------------------------------------------------------------
# Disk cache (JSON blobs keyed by a filename-safe string)
# ---------------------------------------------------------------------------
def _cache_path(key: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return os.path.join(CACHE_DIR, safe + ".json")


def cache_get(key: str, ttl_seconds: float | None):
    """Return cached JSON if present and fresh; else None.

    ttl_seconds=None means "never expires" (used for completed historical seasons).
    """
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    if ttl_seconds is not None and (time.time() - os.path.getmtime(path)) > ttl_seconds:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def cache_put(key: str, data) -> None:
    path = _cache_path(key)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[cache_put] could not write {key}: {exc}")


def cache_age(key: str):
    """Seconds since the cache entry was written, or None if absent."""
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    return time.time() - os.path.getmtime(path)


def cache_clear(prefix: str = "") -> int:
    """Delete cache files whose key starts with `prefix`. Returns count removed."""
    removed = 0
    for name in os.listdir(CACHE_DIR):
        if name.endswith(".json") and name.startswith(prefix):
            try:
                os.remove(os.path.join(CACHE_DIR, name))
                removed += 1
            except OSError:
                pass
    return removed


def cached_fetch(key: str, url: str, ttl_seconds: float | None, timeout: int = 45):
    """Return cached JSON if fresh, otherwise fetch, cache, and return it.

    If the network fetch fails but a stale cache exists, the stale copy is
    returned so the app degrades gracefully offline.
    """
    hit = cache_get(key, ttl_seconds)
    if hit is not None:
        return hit
    fresh = fetch_json(url, timeout=timeout)
    if fresh is not None:
        cache_put(key, fresh)
        return fresh
    # network failed -- fall back to any stale copy we have
    return cache_get(key, None)
