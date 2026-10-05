"""JSON-backed storage for companies and webcasts.

Plain JSON keeps the data diffable when it is committed by the scheduled job.
Companies are keyed by a stable id ("us-<CIK>" for U.S. filers) so that ticker
changes don't orphan history; other countries can use their own prefix later.
"""
import csv
import hashlib
import json
from datetime import datetime, timezone

from . import config


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def load_companies():
    """Return {company_id: company_dict}."""
    return _load(config.COMPANIES_FILE, {})


def save_companies(companies):
    _save(config.COMPANIES_FILE, companies)


def load_webcasts():
    """Return {webcast_id: webcast_dict}."""
    return _load(config.WEBCASTS_FILE, {})


def save_webcasts(webcasts):
    _save(config.WEBCASTS_FILE, webcasts)


def webcast_id(company_id, url):
    return hashlib.sha1(f"{company_id}|{url}".encode()).hexdigest()[:12]


def upsert_webcast(webcasts, company_id, url, title, date, source, source_url):
    """Insert or refresh one webcast; returns True when it is new."""
    wid = webcast_id(company_id, url)
    seen = now_iso()
    existing = webcasts.get(wid)
    if existing:
        existing["last_seen"] = seen
        # Prefer a descriptive title/date over a blank one from a later pass.
        if title and (not existing.get("title") or existing["source"] == "sec-8k"):
            existing["title"] = title
        if date and not existing.get("date"):
            existing["date"] = date
        return False
    webcasts[wid] = {
        "id": wid,
        "company_id": company_id,
        "url": url,
        "title": title or "",
        "date": date,
        "source": source,
        "source_url": source_url,
        "first_seen": seen,
        "last_seen": seen,
    }
    return True


def load_overrides():
    """Manual corrections keyed by ticker: ir_url, website, sector."""
    if not config.OVERRIDES_FILE.exists():
        return {}
    with open(config.OVERRIDES_FILE, encoding="utf-8") as f:
        rows = csv.DictReader(line for line in f if not line.startswith("#"))
        return {r["ticker"].strip().upper(): {k: v.strip() for k, v in r.items() if v and v.strip()}
                for r in rows if r.get("ticker")}


def companies_by_ticker(companies):
    return {t: c for c in companies.values() for t in c.get("tickers", [])}
