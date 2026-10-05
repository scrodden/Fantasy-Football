"""Map SEC SIC codes onto eleven GICS-style sectors.

SIC is what EDGAR publishes for every filer, so it's the one classification we
can get for free across the whole U.S. universe. The mapping is approximate;
fix individual companies with the `sector` column in data/overrides.csv.
"""

SECTORS = [
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Information Technology",
    "Materials",
    "Real Estate",
    "Utilities",
]
OTHER = "Other"

# (low, high, sector) — first match wins, so specific ranges come first.
_RANGES = [
    (1000, 1099, "Materials"),
    (1200, 1399, "Energy"),
    (1400, 1499, "Materials"),
    (1531, 1531, "Consumer Discretionary"),
    (1500, 1799, "Industrials"),
    (2000, 2199, "Consumer Staples"),
    (2200, 2399, "Consumer Discretionary"),
    (2400, 2499, "Materials"),
    (2500, 2599, "Consumer Discretionary"),
    (2600, 2699, "Materials"),
    (2700, 2799, "Communication Services"),
    (2830, 2836, "Health Care"),
    (2840, 2844, "Consumer Staples"),
    (2800, 2899, "Materials"),
    (2900, 2999, "Energy"),
    (3000, 3099, "Materials"),
    (3100, 3199, "Consumer Discretionary"),
    (3200, 3399, "Materials"),
    (3400, 3499, "Industrials"),
    (3570, 3579, "Information Technology"),
    (3500, 3599, "Industrials"),
    (3630, 3639, "Consumer Discretionary"),
    (3650, 3652, "Consumer Discretionary"),
    (3660, 3679, "Information Technology"),
    (3600, 3699, "Industrials"),
    (3710, 3716, "Consumer Discretionary"),
    (3750, 3751, "Consumer Discretionary"),
    (3790, 3799, "Consumer Discretionary"),
    (3700, 3799, "Industrials"),
    (3840, 3851, "Health Care"),
    (3870, 3873, "Consumer Discretionary"),
    (3800, 3899, "Information Technology"),
    (3900, 3999, "Consumer Discretionary"),
    (4000, 4799, "Industrials"),
    (4800, 4899, "Communication Services"),
    (4950, 4959, "Industrials"),
    (4900, 4999, "Utilities"),
    (5120, 5122, "Health Care"),
    (5140, 5149, "Consumer Staples"),
    (5000, 5199, "Industrials"),
    (5400, 5499, "Consumer Staples"),
    (5912, 5912, "Consumer Staples"),
    (5200, 5999, "Consumer Discretionary"),
    (6500, 6553, "Real Estate"),
    (6798, 6798, "Real Estate"),
    (6000, 6799, "Financials"),
    (7000, 7099, "Consumer Discretionary"),
    (7200, 7299, "Consumer Discretionary"),
    (7310, 7319, "Communication Services"),
    (7370, 7379, "Information Technology"),
    (7300, 7399, "Industrials"),
    (7500, 7599, "Consumer Discretionary"),
    (7800, 7899, "Communication Services"),
    (7900, 7999, "Consumer Discretionary"),
    (8000, 8099, "Health Care"),
    (8200, 8299, "Consumer Discretionary"),
    (8731, 8731, "Health Care"),
    (8100, 8999, "Industrials"),
    (100, 999, "Consumer Staples"),
]


def sector_for_sic(sic):
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return OTHER
    for low, high, sector in _RANGES:
        if low <= code <= high:
            return sector
    return OTHER


def slug(sector):
    return sector.lower().replace(" ", "-")
