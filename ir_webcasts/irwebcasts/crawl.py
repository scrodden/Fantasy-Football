"""Visit each company's IR site and record the webcasts it links to.

From the IR home page we follow links that look like events / presentations
listings (same site only), then event-detail pages, collecting webcast links on
every page. Companies are processed oldest-crawl-first so a time-boxed
scheduled run gradually covers the whole universe.
"""
import time
from urllib.parse import urlsplit

from . import store
from .extract import EVENT_DETAIL_RE, EVENTS_LINK_RE, extract_webcasts, find_links, page_heading
from .http import Fetcher

MAX_LISTING_PAGES = 6
MAX_DETAIL_PAGES = 25


def _site(url):
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-2:])


class Renderer:
    """Optional headless-browser fetch for IR pages built with JavaScript."""

    def __init__(self):
        from playwright.sync_api import sync_playwright  # optional dependency
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()

    def html(self, url):
        page = self._browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            return page.content()
        except Exception:
            return None
        finally:
            page.close()

    def close(self):
        self._browser.close()
        self._pw.stop()


def crawl_company(company, fetcher, renderer=None):
    """Return [(webcast, source_page)] found on the company's IR site."""
    root = company["ir_url"]
    site = _site(root)
    pages, results = {}, []

    def fetch(url):
        if url in pages:
            return pages[url]
        resp = fetcher.get(url)
        html = resp.text if resp is not None else None
        if renderer is not None and (html is None or not extract_webcasts(html, url)):
            html = renderer.html(url) or html
        pages[url] = html
        return html

    home = fetch(root)
    if home is None:
        return None
    listing = [root] + [u for u in find_links(home, root, EVENTS_LINK_RE) if _site(u) == site]
    detail = []
    for url in listing[:MAX_LISTING_PAGES]:
        html = fetch(url)
        if not html:
            continue
        results += [(w, url) for w in extract_webcasts(html, url)]
        detail += [u for u in find_links(html, url, EVENT_DETAIL_RE)
                   if _site(u) == site and u not in detail and u not in listing]

    for url in detail[:MAX_DETAIL_PAGES]:
        html = fetch(url)
        if not html:
            continue
        heading, when = page_heading(html)
        for w in extract_webcasts(html, url):
            # On a detail page the page heading names the event better than link text.
            if heading:
                w["title"] = heading
            w["date"] = w["date"] or when
            results.append((w, url))
    return results


def run(limit=None, max_minutes=None, render=False, tickers=None, fetcher=None):
    fetcher = fetcher or Fetcher()
    renderer = Renderer() if render else None
    companies = store.load_companies()
    webcasts = store.load_webcasts()
    wanted = {t.upper() for t in tickers} if tickers else None
    todo = [c for c in companies.values() if c.get("ir_url") and c.get("listed", True)
            and (wanted is None or wanted & set(c.get("tickers", [])))]
    todo.sort(key=lambda c: c.get("last_crawled") or "")
    if limit:
        todo = todo[:limit]
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    crawled = new = 0
    try:
        for company in todo:
            if deadline and time.monotonic() > deadline:
                break
            found = crawl_company(company, fetcher, renderer)
            company["last_crawled"] = store.now_iso()
            company["crawl_status"] = "unreachable" if found is None else f"ok:{len(found)}"
            for w, page in found or []:
                new += store.upsert_webcast(webcasts, company["id"], w["url"], w["title"],
                                            w["date"], "ir-page", page)
            crawled += 1
            if crawled % 20 == 0:
                store.save_companies(companies)
                store.save_webcasts(webcasts)
    finally:
        if renderer:
            renderer.close()
        store.save_companies(companies)
        store.save_webcasts(webcasts)
    return crawled, new
