"""The economic calendar the gold trader stands aside for (actions/gold_news.py).

No network: the calendar is handed in as the feed sends it. Checked:

- times with their own offsets ("-04:00", New York) are read as UTC, and
  malformed entries are skipped rather than stopping the rest;
- only the currencies and impacts chosen count;
- the window: from 30 minutes before to 30 minutes after, both edges in;
- the calendar is read at most hourly, and an unreadable or empty one is
  an error, never an all-clear.

    python tools/test_gold_news.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import gold_news  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


FEED = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High", "forecast": "0.2%"},
    {"title": "German ZEW", "country": "EUR", "date": "2026-10-14T05:00:00-04:00", "impact": "High"},
    {"title": "Business optimism", "country": "USD", "date": "2026-10-14T06:00:00-04:00", "impact": "Medium"},
    {"title": "No date", "country": "USD", "impact": "High"},
    {"title": "No zone", "country": "USD", "date": "2026-10-14T09:00:00", "impact": "High"},
]

SETTINGS = {"news_currencies": ["USD"], "news_impact": ["High"], "news_minutes_before": 30, "news_minutes_after": 30}

found = gold_news.parse(FEED)
check(len(found) == 3 and found[-1] == (utc(2026, 10, 14, 12, 30), "USD", "High", "CPI m/m"),
      "New York's 08:30 is 12:30 UTC; an entry with no date or no zone is skipped, the rest kept")
check(gold_news.parse({"not": "a list"}) == [], "a feed that is not a list is nothing")

check(gold_news.blocking(utc(2026, 10, 14, 12, 0), SETTINGS, found)[3] == "CPI m/m", "30 minutes before: out")
check(gold_news.blocking(utc(2026, 10, 14, 13, 0), SETTINGS, found)[3] == "CPI m/m", "30 minutes after: out")
check(gold_news.blocking(utc(2026, 10, 14, 11, 59), SETTINGS, found) is None
      and gold_news.blocking(utc(2026, 10, 14, 13, 1), SETTINGS, found) is None, "a minute beyond either side: clear")
check(gold_news.blocking(utc(2026, 10, 14, 9, 0), SETTINGS, found) is None
      and gold_news.blocking(utc(2026, 10, 14, 10, 0), SETTINGS, found) is None,
      "the euro's release and the medium one do not count")
check(gold_news.blocking(utc(2026, 10, 14, 10, 0), dict(SETTINGS, news_impact=["High", "Medium"]), found)[3]
      == "Business optimism", "unless the settings say they do")
check([event[3] for event in gold_news.today(utc(2026, 10, 14), utc(2026, 10, 15), SETTINGS, found)] == ["CPI m/m"],
      "the day's matching events, for the morning's word")

# ---- the morning's word: releases that share a time are one event, windows are the real edges ----------
CPI = [(utc(2026, 10, 14, 12, 30), "USD", "High", title)
       for title in ("CPI m/m", "CPI y/y", "Core CPI m/m", "Core CPI y/y")]
check(gold_news.brief(CPI, SETTINGS) ==
      "Gold today, sir: four figures at 13:30 (CPI m/m, CPI y/y, Core CPI m/m and Core CPI y/y)."
      " No new gold trades from 13:00 to 14:00.",
      f"four figures at one time are one event, one window ({gold_news.brief(CPI, SETTINGS)})")
check(gold_news.brief(CPI[:1], SETTINGS) ==
      "Gold today, sir: CPI m/m at 13:30. No new gold trades from 13:00 to 14:00.",
      "a lone release is named on its own")
two = CPI[:1] + [(utc(2026, 10, 14, 17, 30), "USD", "High", "Retail sales")]
check(gold_news.brief(two, SETTINGS) ==
      "Gold today, sir: CPI m/m at 13:30 and Retail sales at 18:30."
      " No new gold trades 13:00 to 14:00 and 18:00 to 19:00.",
      f"two separate times give two windows ({gold_news.brief(two, SETTINGS)})")
near = CPI[:1] + [(utc(2026, 10, 14, 13, 10), "USD", "High", "Fed speaks")]
check(gold_news.brief(near, SETTINGS) ==
      "Gold today, sir: CPI m/m at 13:30 and Fed speaks at 14:10. No new gold trades from 13:00 to 14:40.",
      f"windows that touch merge into one span ({gold_news.brief(near, SETTINGS)})")
check(gold_news.brief([], SETTINGS) == "", "nothing due, nothing said")
many = [(utc(2026, 10, 14, 12, 30), "USD", "High", f"Figure {n}") for n in range(6)]
check("6 releases at 13:30" in gold_news.brief(many, SETTINGS)
      and "Figure" not in gold_news.brief(many, SETTINGS),
      "beyond a handful, the count stands in for the list")

asked = []
clock = {"now": 1000.0}


def feed(url):
    asked.append(url)
    return FEED


gold_news.events("https://calendar.test/a.json", fetch=feed, clock=lambda: clock["now"])
gold_news.events("https://calendar.test/a.json", fetch=feed, clock=lambda: clock["now"])
check(len(asked) == 1, "read once, then kept")
clock["now"] += gold_news.REFRESH_SECONDS + 1
gold_news.events("https://calendar.test/a.json", fetch=feed, clock=lambda: clock["now"])
check(len(asked) == 2, "and read again after an hour")


def broken(url):
    raise ConnectionError("down")


for name, fetch in (("unreachable", broken), ("empty", lambda url: [])):
    try:
        gold_news.events(f"https://calendar.test/{name}.json", fetch=fetch, clock=lambda: clock["now"])
        refused = False
    except gold_news.CalendarError:
        refused = True

    check(refused, f"an {name} calendar is an error, never an all-clear")

sys.exit(1 if failures else 0)
