"""Command line entry point: python -m irwebcasts <step> [options]

Steps, in pipeline order:
  universe   refresh the list of U.S.-listed companies (+ SIC sector) from SEC
  sec-mine   backfill: read each company's recent 8-Ks for IR URL / domain hints
  sec-recent daily: read every recent 8-K that mentions a webcast
  discover   find IR sites for companies that don't have one yet
  crawl      visit IR sites and collect webcast links
  build      regenerate the static website
  daily      sec-recent + discover + crawl + build, each time-boxed
"""
import argparse
import sys

from . import build, crawl, discover, sec_filings, universe


def main(argv=None):
    p = argparse.ArgumentParser(prog="irwebcasts", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="step", required=True)

    u = sub.add_parser("universe")
    u.add_argument("--include-otc", action="store_true")
    u.add_argument("--limit", type=int, help="only add the N largest new companies")
    u.add_argument("--tickers", nargs="*")

    m = sub.add_parser("sec-mine")
    m.add_argument("--limit", type=int)
    m.add_argument("--months", type=int, default=18)
    m.add_argument("--all", action="store_true", help="re-mine companies already mined")

    r = sub.add_parser("sec-recent")
    r.add_argument("--days", type=int, default=3)

    d = sub.add_parser("discover")
    d.add_argument("--limit", type=int)
    d.add_argument("--retry-failed", action="store_true")
    d.add_argument("--minutes", type=float)

    c = sub.add_parser("crawl")
    c.add_argument("--limit", type=int)
    c.add_argument("--minutes", type=float)
    c.add_argument("--render", action="store_true", help="use Playwright for JS-heavy IR sites")
    c.add_argument("--tickers", nargs="*")

    sub.add_parser("build")

    dl = sub.add_parser("daily")
    dl.add_argument("--crawl-minutes", type=float, default=200)
    dl.add_argument("--discover-minutes", type=float, default=45)
    dl.add_argument("--mine-limit", type=int, default=600)

    args = p.parse_args(argv)
    if args.step == "universe":
        n = universe.refresh(args.include_otc, args.limit, args.tickers)
        print(f"universe: {n} listed companies")
    elif args.step == "sec-mine":
        n, new = sec_filings.mine_companies(args.limit, args.months, only_missing=not args.all)
        print(f"sec-mine: {n} companies checked, {new} new webcasts")
    elif args.step == "sec-recent":
        n, new = sec_filings.scan_recent(args.days)
        print(f"sec-recent: {n} exhibits read, {new} new webcasts")
    elif args.step == "discover":
        n, found = discover.run(args.limit, args.retry_failed, args.minutes)
        print(f"discover: {found}/{n} IR sites found")
    elif args.step == "crawl":
        n, new = crawl.run(args.limit, args.minutes, args.render, args.tickers)
        print(f"crawl: {n} companies crawled, {new} new webcasts")
    elif args.step == "build":
        print("build:", build.build())
    elif args.step == "daily":
        print("sec-recent:", sec_filings.scan_recent(3))
        print("sec-mine:", sec_filings.mine_companies(args.mine_limit))
        print("discover:", discover.run(max_minutes=args.discover_minutes))
        print("crawl:", crawl.run(max_minutes=args.crawl_minutes))
        print("build:", build.build())
    return 0


if __name__ == "__main__":
    sys.exit(main())
