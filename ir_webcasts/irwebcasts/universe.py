"""Build the company universe from SEC EDGAR.

company_tickers_exchange.json lists every ticker EDGAR knows with its exchange;
the per-company submissions endpoint adds the SIC code we turn into a sector.
"""
import re

from . import config, sectors, store
from .http import Fetcher

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def refresh(include_otc=False, limit=None, tickers=None, fetcher=None):
    fetcher = fetcher or Fetcher()
    payload = fetcher.get_json(TICKERS_URL)
    if not payload:
        raise SystemExit("Could not download the SEC ticker list (is IRW_CONTACT set?)")
    fields = payload["fields"]
    exchanges = set(config.US_EXCHANGES) | ({"OTC"} if include_otc else set())
    wanted = {t.upper() for t in tickers} if tickers else None

    companies = store.load_companies()
    overrides = store.load_overrides()
    seen = set()
    added = 0
    # Rows are ordered by market cap, and a company's first ticker is its primary.
    for row in payload["data"]:
        rec = dict(zip(fields, row))
        if rec["exchange"] not in exchanges or not rec["ticker"]:
            continue
        ticker = rec["ticker"].upper()
        cid = f"us-{rec['cik']}"
        if wanted is not None and ticker not in wanted and cid not in seen:
            continue
        company = companies.get(cid)
        if company is None:
            if limit is not None and added >= limit:
                continue
            company = companies[cid] = {
                "id": cid, "country": "US", "cik": rec["cik"], "tickers": [],
            }
            added += 1
        if cid not in seen:
            company["tickers"] = []
            seen.add(cid)
        if ticker not in company["tickers"]:
            company["tickers"].append(ticker)
        company["name"] = company.get("name") or _tidy_name(rec["name"])
        company["exchange"] = company.get("exchange") or rec["exchange"]
        company["listed"] = True

    for cid, company in companies.items():
        if company.get("country") == "US" and cid not in seen and wanted is None:
            company["listed"] = False  # delisted; keep history but hide by default
        if cid in seen and "sic" not in company:
            sub = fetcher.get_json(SUBMISSIONS_URL.format(cik=company["cik"]))
            if sub:
                company["sic"] = sub.get("sic") or None
                company["industry"] = sub.get("sicDescription") or None
                company["name"] = sub.get("name") and _tidy_name(sub["name"]) or company["name"]
                if sub.get("website"):
                    company.setdefault("website", sub["website"])
                if sub.get("investorWebsite"):
                    company.setdefault("ir_url", sub["investorWebsite"])
                    company.setdefault("ir_source", "sec-submissions")
        company["sector"] = sectors.sector_for_sic(company.get("sic"))
        for ticker in company.get("tickers", []):
            if ticker in overrides:
                _apply_override(company, overrides[ticker])

    store.save_companies(companies)
    return len(seen)


def _apply_override(company, override):
    if override.get("sector"):
        company["sector"] = override["sector"]
    if override.get("website"):
        company["website"] = override["website"]
    if override.get("ir_url"):
        company["ir_url"] = override["ir_url"]
        company["ir_source"] = "override"


_KEEP_UPPER = {"AG", "SA", "NV", "PLC", "SE", "ASA", "LP", "LLC", "II", "III", "IV", "USA", "US", "REIT", "ETF", "AB"}


def _tidy_name(name):
    """EDGAR names are often ALL CAPS ("NVIDIA CORP") with a state suffix
    ("WATERS CORP /DE/"); make them readable."""
    if not name:
        return name
    name = re.sub(r"\s*[/\\][A-Za-z]{2,3}[/\\]?\s*$", "", name).strip()
    words = []
    for w in name.split():
        core = w.strip(".,/()")
        if core in ("CO", "INC", "LTD", "CORP", "BANCORP"):
            words.append(w.capitalize())
        elif not core.isupper() or core in _KEEP_UPPER or len(core) <= 3 or not core.isalpha():
            words.append(w)
        elif len(core) == 4 and not any(ch in "AEIOU" for ch in core):
            words.append(w)  # likely an acronym: NVR, CSX
        else:
            words.append(w.capitalize())
    return " ".join(words)
