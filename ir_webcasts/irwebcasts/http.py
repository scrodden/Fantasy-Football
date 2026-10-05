"""A polite HTTP client: per-host rate limiting, retries and robots.txt."""
import time
import urllib.robotparser
from urllib.parse import urlsplit

import requests

from . import config


class Fetcher:
    def __init__(self, user_agent=config.USER_AGENT, respect_robots=True):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        })
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self._last_hit = {}
        self._robots = {}

    def _delay_for(self, host):
        return config.SEC_DELAY if host.endswith("sec.gov") else config.SITE_DELAY

    def _wait(self, host):
        wait = self._last_hit.get(host, 0) + self._delay_for(host) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def allowed(self, url):
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        if parts.netloc.endswith("sec.gov"):
            return True
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp = self.session.get(root + "/robots.txt", timeout=config.TIMEOUT)
                rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[root] = rp
        return self._robots[root].can_fetch(self.user_agent, url)

    def get(self, url, retries=2, **kwargs):
        """Return a Response, or None when blocked, missing or unreachable."""
        if not self.allowed(url):
            return None
        host = urlsplit(url).netloc
        for attempt in range(retries + 1):
            self._wait(host)
            try:
                resp = self.session.get(url, timeout=config.TIMEOUT, **kwargs)
            except requests.RequestException:
                resp = None
            if resp is not None and resp.status_code < 400:
                return resp
            if resp is not None and resp.status_code not in (429, 500, 502, 503, 504):
                return None
            time.sleep(2 ** attempt * 2)
        return None

    def get_json(self, url, **kwargs):
        resp = self.get(url, **kwargs)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            return None
