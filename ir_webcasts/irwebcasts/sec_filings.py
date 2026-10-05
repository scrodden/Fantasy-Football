"""Mine 8-K press releases on EDGAR for webcast links and IR-site URLs.

Companies announce earnings calls and conference presentations in 8-K exhibits
("A live webcast will be available at ..."). EDGAR full-text search lets us find
those across every filer in one query, which gives us (1) webcasts directly and
(2) the company's own domain / IR URL, which EDGAR otherwise doesn't publish.
"""
import re
import warnings
from collections import Counter
from datetime import date, timedelta
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from . import store
from .conferences import extract_conference_name
from .dates import find_event_date
from .extract import is_webcast_url
from .http import Fetcher

# Some exhibits are XHTML; the HTML parser handles them fine.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{filename}"

# Domains that show up in press releases but aren't the issuer's own site.
THIRD_PARTY = (
    "sec.gov", "prnewswire.com", "businesswire.com", "globenewswire.com", "accesswire.com",
    "newsfilecorp.com", "einpresswire.com", "linkedin.com", "twitter.com", "x.com",
    "facebook.com", "instagram.com", "youtube.com", "gmail.com", "outlook.com",
    "yahoo.com", "q4cdn.com", "q4inc.com", "gcs-web.com", "investorroom.com",
    "w3.org", "xbrl.org", "fasb.org", "nasdaq.com", "nyse.com", "google.com",
    "spotify.com", "icrinc.com", "gilmartinir.com", "alpha-ir.com",
    "lhai.com", "sternir.com", "edelman.com", "fticonsulting.com", "argotpartners.com",
    "mzgroup.us", "haydenir.com", "kcsa.com", "theequitygroup.com", "precisionaq.com",
    "lifesciadvisors.com", "sloanepr.com", "jpmorgan.com", "cowen.com", "tdcowen.com",
    "bofa.com", "goldmansachs.com", "morganstanley.com", "investor.gov",
)

_URL_RE = re.compile(r"(?:https?://|www\.)[A-Za-z0-9.-]+\.[a-z]{2,}(?:/[^\s<>\"'()]*)?", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[a-z]{2,})")
_IR_RE = re.compile(r"(^|\.)(investors?|ir|investor-relations)\.|/(investors?|investor-relations|ir)(/|$)", re.I)
_EARNINGS_RE = re.compile(
    r"\b((?:first|second|third|fourth|1st|2nd|3rd|4th|q[1-4])[- ]quarter(?:\s+(?:fiscal\s+)?(?:year\s+)?20\d\d)?|"
    r"(?:fiscal\s+)?(?:year[- ]end|full[- ]year)\s+(?:fiscal\s+)?20\d\d|q[1-4]\s+(?:fy\s*)?20\d\d)", re.I)


def _registrable(host):
    parts = host.lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_third_party(host):
    return any(host == t or host.endswith("." + t) for t in THIRD_PARTY)


def _normalize_url(raw):
    raw = raw.rstrip(".,;:)]")
    return raw if raw.lower().startswith("http") else "https://" + raw


def search(query, start, end, ciks=None, forms="8-K,6-K", fetcher=None, max_hits=2000):
    """Yield EFTS hits ({cik, adsh, filename, file_date, file_type, name})."""
    fetcher = fetcher or Fetcher()
    offset = 0
    while offset < max_hits:
        params = {"q": f'"{query}"', "forms": forms, "dateRange": "custom",
                  "startdt": start.isoformat(), "enddt": end.isoformat(), "from": offset}
        if ciks:
            params["ciks"] = ",".join(f"{int(c):010d}" for c in ciks)
        data = fetcher.get_json(EFTS_URL, params=params)
        hits = (data or {}).get("hits", {}).get("hits", [])
        if not hits:
            return
        for h in hits:
            src = h["_source"]
            adsh, _, filename = h["_id"].partition(":")
            for cik in src.get("ciks", []):
                yield {"cik": int(cik), "adsh": adsh, "filename": filename,
                       "file_date": src.get("file_date"), "file_type": src.get("file_type", ""),
                       "name": (src.get("display_names") or [""])[0]}
        offset += len(hits)
        if offset >= data["hits"]["total"]["value"]:
            return


def parse_exhibit(html, filed=None):
    """Return {webcasts: [{url,title,date}], ir_urls: Counter, domains: Counter}."""
    soup = BeautifulSoup(html, "lxml")
    text = re.sub(r"\s+", " ", soup.get_text(" "))
    urls = [_normalize_url(a["href"]) for a in soup.find_all("a", href=True)
            if a["href"].lower().startswith(("http", "www"))]
    urls += [_normalize_url(u) for u in _URL_RE.findall(text)]

    ir_urls, domains, webcast_urls = Counter(), Counter(), []
    for url in urls:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if not host:
            continue
        if is_webcast_url(url):
            if url not in webcast_urls:
                webcast_urls.append(url)
            continue
        if _is_third_party(host):
            continue
        domains[_registrable(host)] += 1
        if _IR_RE.search(host + parts.path):
            ir_urls[f"{parts.scheme}://{host}{parts.path}".rstrip("/")] += 1
    for host in _EMAIL_RE.findall(text):
        if not _is_third_party(host.lower()):
            domains[_registrable(host)] += 1

    webcasts = []
    if webcast_urls:
        title, when = describe_event(text, filed)
        webcasts = [{"url": u, "title": title, "date": when} for u in webcast_urls]
    return {"webcasts": webcasts, "ir_urls": ir_urls, "domains": domains}


def describe_event(text, filed=None):
    """Title and date for the event a press release announces.

    filed (YYYY-MM-DD) bounds the event date to shortly before/after filing.
    """
    idx = text.lower().find("webcast")
    window = text[max(0, idx - 600): idx + 400] if idx >= 0 else text[:1500]
    conference = extract_conference_name(window) or extract_conference_name(text[:3000])
    bounds = {}
    if filed:
        day = date.fromisoformat(filed)
        bounds = {"earliest": (day - timedelta(days=7)).isoformat(),
                  "latest": (day + timedelta(days=120)).isoformat()}
    when = find_event_date(window, **bounds)
    if conference:
        return f"Presentation at {conference}", when
    m = _EARNINGS_RE.search(window) or _EARNINGS_RE.search(text[:3000])
    if m:
        return f"{m.group(1).strip().title()} Earnings Call", when
    if re.search(r"investor day|analyst day|capital markets day", window, re.I):
        return "Investor Day", when
    if re.search(r"annual (?:general )?meeting", window, re.I):
        return "Annual Meeting of Stockholders", when
    return "Webcast", when


def _exhibit_url(hit):
    return ARCHIVE_URL.format(cik=hit["cik"], adsh=hit["adsh"].replace("-", ""), filename=hit["filename"])


def _read(hit, fetcher):
    resp = fetcher.get(_exhibit_url(hit))
    return parse_exhibit(resp.text, hit.get("file_date")) if resp is not None else None


def _absorb(company, parsed, webcasts, source_url):
    new = 0
    for w in parsed["webcasts"]:
        new += store.upsert_webcast(webcasts, company["id"], w["url"], w["title"], w["date"],
                                    "sec-8k", source_url)
    hints = company.setdefault("hints", {"domains": {}, "ir_urls": {}})
    for key, counter in (("domains", parsed["domains"]), ("ir_urls", parsed["ir_urls"])):
        for value, n in counter.items():
            hints[key][value] = hints[key].get(value, 0) + n
    return new


def scan_recent(days=3, fetcher=None):
    """Daily pass: every 8-K exhibit mentioning a webcast in the last N days."""
    fetcher = fetcher or Fetcher()
    companies = store.load_companies()
    webcasts = store.load_webcasts()
    end = date.today()
    start = end - timedelta(days=days)
    done, new = set(), 0
    for hit in search("webcast", start, end, fetcher=fetcher):
        company = companies.get(f"us-{hit['cik']}")
        key = (hit["adsh"], hit["filename"])
        if company is None or key in done or not hit["file_type"].upper().startswith("EX-99"):
            continue
        done.add(key)
        parsed = _read(hit, fetcher)
        if parsed:
            new += _absorb(company, parsed, webcasts, _exhibit_url(hit))
    store.save_companies(companies)
    store.save_webcasts(webcasts)
    return len(done), new


def mine_companies(limit=None, months=18, per_company=4, fetcher=None, only_missing=True):
    """Backfill: look through each company's recent 8-Ks for domain/IR hints."""
    fetcher = fetcher or Fetcher()
    companies = store.load_companies()
    webcasts = store.load_webcasts()
    end = date.today()
    start = end - timedelta(days=30 * months)
    todo = [c for c in companies.values() if c.get("listed", True)
            and not (only_missing and (c.get("ir_url") or c.get("hints_mined")))]
    if limit:
        todo = todo[:limit]
    new = 0
    for i, company in enumerate(todo, 1):
        hits = [h for h in search("webcast", start, end, ciks=[company["cik"]], fetcher=fetcher, max_hits=100)
                if h["file_type"].upper().startswith("EX-99")]
        hits.sort(key=lambda h: h["file_date"] or "", reverse=True)
        seen = set()
        for hit in hits:
            if len(seen) >= per_company or (hit["adsh"], hit["filename"]) in seen:
                continue
            seen.add((hit["adsh"], hit["filename"]))
            parsed = _read(hit, fetcher)
            if parsed:
                new += _absorb(company, parsed, webcasts, _exhibit_url(hit))
        company["hints_mined"] = store.now_iso()
        if i % 50 == 0:
            store.save_companies(companies)
            store.save_webcasts(webcasts)
    store.save_companies(companies)
    store.save_webcasts(webcasts)
    return len(todo), new
