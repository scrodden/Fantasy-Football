"""Find each company's investor-relations site.

Order of evidence:
  1. data/overrides.csv (manual)
  2. IR URLs printed in the company's own 8-K press releases (sec_filings)
  3. Common IR locations on the company's domain (investors.<d>, <d>/investors, ...)
  4. An "Investors" link on the company homepage
A candidate is accepted only if the page actually reads like an IR site.
"""
import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import store
from .http import Fetcher

_IR_WORDS = ("investor", "sec filings", "stock", "events", "presentations", "webcast",
             "annual report", "quarterly results", "governance", "shareholder", "dividend",
             "earnings", "financial information", "press releases")
_INVESTOR_LINK = re.compile(r"^\s*investors?(?: relations)?\s*$|investor relations", re.I)
_NAME_STOP = {"inc", "corp", "corporation", "co", "company", "holdings", "group", "ltd", "plc",
              "the", "and", "of", "trust", "sa", "nv", "ag", "lp", "llc", "international", "bancorp"}


def _name_tokens(name):
    return [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in _NAME_STOP and len(t) > 1]


def pick_domain(company):
    """Most likely corporate domain, from overrides or 8-K hints."""
    if company.get("website"):
        return re.sub(r"^https?://(www\.)?", "", company["website"]).split("/")[0].lower()
    domains = company.get("hints", {}).get("domains", {})
    if not domains:
        return None
    tokens = _name_tokens(company.get("name"))
    tickers = [t.lower() for t in company.get("tickers", [])]

    def score(item):
        domain, count = item
        label = domain.split(".")[0]
        bonus = sum(5 for t in tokens if t in label) + sum(4 for t in tickers if label == t)
        return count + bonus

    return max(domains.items(), key=score)[0]


def looks_like_ir(html):
    text = BeautifulSoup(html, "lxml").get_text(" ").lower()
    return sum(1 for w in _IR_WORDS if w in text) >= 4 and "investor" in text


def candidates(company):
    domain = pick_domain(company)
    seen, out = set(), []

    def add(url):
        if url and url not in seen:
            seen.add(url)
            out.append(url)

    ir_hints = company.get("hints", {}).get("ir_urls", {})
    for url, _ in sorted(ir_hints.items(), key=lambda kv: -kv[1]):
        if not domain or domain in url:
            add(url)
    if domain:
        for pattern in ("https://investors.{d}", "https://investor.{d}", "https://ir.{d}",
                        "https://www.{d}/investors", "https://www.{d}/investor-relations",
                        "https://{d}/investors"):
            add(pattern.format(d=domain))
    return domain, out


def discover_one(company, fetcher):
    domain, urls = candidates(company)
    for url in urls:
        resp = fetcher.get(url)
        if resp is not None and looks_like_ir(resp.text):
            return resp.url, "probe"
    if domain:
        resp = fetcher.get(f"https://www.{domain}")
        if resp is not None:
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                if _INVESTOR_LINK.search(a.get_text(" ")):
                    href = urljoin(resp.url, a["href"])
                    page = fetcher.get(href)
                    if page is not None and looks_like_ir(page.text):
                        return page.url, "homepage-link"
    return None, None


def run(limit=None, retry_failed=False, max_minutes=None, fetcher=None):
    fetcher = fetcher or Fetcher()
    companies = store.load_companies()
    todo = [c for c in companies.values()
            if c.get("listed", True) and not c.get("ir_url")
            and (retry_failed or not c.get("ir_checked"))]
    if limit:
        todo = todo[:limit]
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    found = 0
    for i, company in enumerate(todo, 1):
        if deadline and time.monotonic() > deadline:
            todo = todo[:i - 1]
            break
        url, how = discover_one(company, fetcher)
        company["ir_checked"] = store.now_iso()
        if url:
            company["ir_url"], company["ir_source"] = url, how
            found += 1
        if i % 25 == 0:
            store.save_companies(companies)
    store.save_companies(companies)
    return len(todo), found
