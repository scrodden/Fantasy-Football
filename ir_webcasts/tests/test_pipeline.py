import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from irwebcasts import conferences, sectors  # noqa: E402
from irwebcasts.crawl import crawl_company  # noqa: E402
from irwebcasts.dates import find_date  # noqa: E402
from irwebcasts.discover import pick_domain  # noqa: E402
from irwebcasts.extract import extract_webcasts  # noqa: E402
from irwebcasts.sec_filings import parse_exhibit  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class ExtractTests(unittest.TestCase):
    def setUp(self):
        html = (FIXTURES / "notified_events.html").read_text()
        self.found = {w["url"]: w for w in extract_webcasts(html, "https://investors.acme.com/news-events/events")}

    def test_finds_webcasts_and_skips_noise(self):
        self.assertEqual(set(self.found), {
            "https://wsw.com/webcast/jpm44/acme/",
            "https://edge.media-server.com/mmc/p/abc123",
            "https://kvgo.com/morgan-stanley/acme-sept-2025",
            "https://www.virtualshareholdermeeting.com/ACME2025",
        })

    def test_titles_and_dates_come_from_the_event_block(self):
        jpm = self.found["https://wsw.com/webcast/jpm44/acme/"]
        self.assertEqual(jpm["title"], "J.P. Morgan 44th Annual Healthcare Conference")
        self.assertEqual(jpm["date"], "2026-01-13")
        q3 = self.found["https://edge.media-server.com/mmc/p/abc123"]
        self.assertEqual(q3["title"], "Q3 2025 Earnings Conference Call")
        self.assertEqual(q3["date"], "2025-11-04")
        ms = self.found["https://kvgo.com/morgan-stanley/acme-sept-2025"]
        self.assertEqual(ms["date"], "2025-09-08")
        self.assertIn("Morgan Stanley", ms["title"])


class FakeResponse:
    def __init__(self, url, text):
        self.url, self.text = url, text


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.requested = pages, []

    def get(self, url):
        self.requested.append(url)
        return FakeResponse(url, self.pages[url]) if url in self.pages else None


class CrawlTests(unittest.TestCase):
    def test_follows_events_and_detail_pages_on_the_ir_site(self):
        events = (FIXTURES / "notified_events.html").read_text().replace(
            "</body>", '<a href="/news-events/event-details/goldman-2025">Goldman Sachs Communacopia</a></body>')
        fetcher = FakeFetcher({
            "https://investors.acme.com": (FIXTURES / "ir_home.html").read_text(),
            "https://investors.acme.com/news-events/events": events,
            "https://investors.acme.com/news-events/event-details/goldman-2025": (FIXTURES / "event_detail.html").read_text(),
        })
        found = {w["url"]: (w, page) for w, page in crawl_company({"ir_url": "https://investors.acme.com"}, fetcher)}
        self.assertIn("https://edge.media-server.com/mmc/p/abc123", found)
        gs, page = found["https://wsw.com/webcast/gsc25/acme/"]
        self.assertEqual(gs["title"], "Acme Robotics at the Goldman Sachs Communacopia + Technology Conference")
        self.assertEqual(gs["date"], "2025-09-09")
        self.assertTrue(page.endswith("goldman-2025"))
        self.assertNotIn("https://www.otherbank.com/events", fetcher.requested)


class SecExhibitTests(unittest.TestCase):
    def test_press_release(self):
        parsed = parse_exhibit((FIXTURES / "press_release.html").read_text())
        self.assertEqual([w["url"] for w in parsed["webcasts"]], ["https://event.webcasts.com/starthere.jsp?ei=1234"])
        w = parsed["webcasts"][0]
        self.assertEqual(w["title"], "Presentation at Wells Fargo 2026 Industrials Conference")
        self.assertEqual(w["date"], "2026-06-09")
        self.assertIn("https://investors.acmerobotics.com", parsed["ir_urls"])
        self.assertEqual(parsed["domains"].most_common(1)[0][0], "acmerobotics.com")
        company = {"name": "Acme Robotics Inc", "tickers": ["ACME"], "hints": {"domains": dict(parsed["domains"])}}
        self.assertEqual(pick_domain(company), "acmerobotics.com")


class ConferenceTests(unittest.TestCase):
    def test_same_conference_written_differently_groups_together(self):
        companies = {"a": {"sector": "Health Care"}, "b": {"sector": "Health Care"},
                     "c": {"sector": "Information Technology"}, "d": {"sector": "Financials"}}
        webcasts = [
            {"id": "1", "company_id": "a", "url": "https://x.com/1", "title": "J.P. Morgan 44th Annual Healthcare Conference", "date": "2026-01-13"},
            {"id": "2", "company_id": "b", "url": "https://x.com/2", "title": "Fireside Chat at the 2026 JPMorgan Healthcare Conference", "date": "2026-01-14"},
            {"id": "3", "company_id": "c", "url": "https://wsw.com/webcast/jpm44/ccc/", "title": "Fireside chat", "date": "2026-01-15"},
            {"id": "4", "company_id": "a", "url": "https://wsw.com/webcast/jpm44/aaa/", "title": "J.P. Morgan Healthcare Conference", "date": "2026-01-13"},
            {"id": "5", "company_id": "d", "url": "https://x.com/5", "title": "Q4 2025 Earnings Conference Call", "date": "2026-02-01"},
            {"id": "6", "company_id": "a", "url": "https://x.com/6", "title": "J.P. Morgan Healthcare Conference", "date": "2025-01-14"},
        ]
        confs = {c["id"]: c for c in conferences.build(webcasts, companies)}
        cid = webcasts[0]["conference_id"]
        self.assertEqual({w["id"] for w in webcasts if w.get("conference_id") == cid}, {"1", "2", "3", "4"})
        self.assertEqual(confs[cid]["company_count"], 3)
        self.assertEqual(confs[cid]["host"], "J.P. Morgan")
        self.assertEqual((confs[cid]["start"], confs[cid]["end"]), ("2026-01-13", "2026-01-15"))
        self.assertNotIn("conference_id", webcasts[4])
        self.assertEqual(webcasts[4]["event_type"], "Earnings")
        self.assertNotEqual(webcasts[5]["conference_id"], cid)  # previous year's edition

    def test_not_a_conference(self):
        for text in ("Q3 2026 Earnings Conference Call",
                     "log on at https://edge.media-server.com/mmc/p/65yp7xh9/ Conference ID 1234",
                     "dial the Conference Operator"):
            self.assertIsNone(conferences.extract_conference_name(text), text)

    def test_classify(self):
        self.assertEqual(conferences.classify("2026 Annual Meeting of Stockholders"), "Shareholder Meeting")
        self.assertEqual(conferences.classify("Investor Day 2026"), "Investor Day")
        self.assertEqual(conferences.classify("Third Quarter 2026 Results"), "Earnings")


class MiscTests(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(find_date("Tuesday, Sept. 9th, 2026 at 9am"), "2026-09-09")
        self.assertEqual(find_date("13 March 2026"), "2026-03-13")
        self.assertIsNone(find_date("no date here"))

    def test_sectors(self):
        self.assertEqual(sectors.sector_for_sic("2834"), "Health Care")
        self.assertEqual(sectors.sector_for_sic("3674"), "Information Technology")
        self.assertEqual(sectors.sector_for_sic("6798"), "Real Estate")
        self.assertEqual(sectors.sector_for_sic(None), "Other")


if __name__ == "__main__":
    unittest.main()
