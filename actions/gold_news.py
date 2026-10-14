"""The economic calendar, so the gold trader stands aside around market-moving news.

Gold is priced in dollars, and a big US release -- the jobs report, inflation,
an interest-rate decision -- can move it $20 or more in a minute, through
any stop. Professional practice is not to be entering trades then. So the
trader asks: is a high-impact release for the dollar due within half an
hour, or did one come out within the last half hour? If so, no new trade.

The calendar is the free weekly one the trading community uses (Forex
Factory's), read as data: the time, the currency, the impact. No model, no
headlines to interpret. Its address and the rules are in gold-trader.json:

    calendar_url          the weekly calendar (JSON)
    news_currencies       ["USD"]
    news_impact           ["High"]
    news_minutes_before   30
    news_minutes_after    30

It is fetched at most once an hour. If it cannot be read, the trader does
not guess that the coast is clear: it stands aside, and says so once a day.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from actions import gold_strategy


CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

REFRESH_SECONDS = 3600

TIMEOUT = 20


class CalendarError(Exception):
    """The calendar could not be read; said in words."""


_lock = threading.Lock()
_cache = {"url": None, "at": 0.0, "events": None}


def parse(entries):
    """The calendar's entries as (UTC time, currency, impact, title), oldest first; malformed ones skipped."""
    events = []

    for entry in entries if isinstance(entries, list) else []:
        try:
            when = datetime.fromisoformat(str(entry["date"]))
        except (KeyError, TypeError, ValueError):
            continue

        if when.tzinfo is None:
            continue

        events.append((when.astimezone(timezone.utc), str(entry.get("country", "")).upper(),
                       str(entry.get("impact", "")).capitalize(), str(entry.get("title", "")).strip()))

    return sorted(events)


def events(url=CALENDAR_URL, fetch=None, clock=time.monotonic):
    """This week's events, from the calendar at [url], read again at most hourly."""
    with _lock:
        fresh = _cache["url"] == url and _cache["events"] is not None and clock() - _cache["at"] < REFRESH_SECONDS

        if fresh:
            return list(_cache["events"])

    try:
        if fetch is None:
            import requests

            response = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "JARVIS gold trader"})

            if response.status_code != 200:
                raise CalendarError(f"the calendar answered {response.status_code}")

            entries = response.json()
        else:
            entries = fetch(url)
    except CalendarError:
        raise
    except Exception as error:
        raise CalendarError(f"the calendar could not be read ({type(error).__name__})") from None

    found = parse(entries)

    if not found:
        raise CalendarError("the calendar came back empty")

    with _lock:
        _cache.update(url=url, at=clock(), events=found)

    return list(found)


def _matching(found, settings):
    currencies = {currency.upper() for currency in settings["news_currencies"]}
    impacts = {impact.capitalize() for impact in settings["news_impact"]}
    return [event for event in found if event[1] in currencies and event[2] in impacts]


def blocking(now, settings, found):
    """The event that keeps the trader out at [now], or None."""
    before = timedelta(minutes=settings["news_minutes_before"])
    after = timedelta(minutes=settings["news_minutes_after"])

    for event in _matching(found, settings):
        if event[0] - before <= now <= event[0] + after:
            return event

    return None


def today(day_start, day_end, settings, found):
    """The matching events between [day_start] and [day_end] (UTC), for the morning's word."""
    return [event for event in _matching(found, settings) if day_start <= event[0] < day_end]


def _and_join(parts):
    """['a', 'b', 'c'] -> 'a, b and c'; two -> 'a and b'; one -> 'a'."""
    parts = list(parts)

    if len(parts) <= 1:
        return parts[0] if parts else ""

    return f"{', '.join(parts[:-1])} and {parts[-1]}"


# How many figures a group lists in full before it is just called a count: four CPI lines
# read out in full are useful, a dozen would not be.
_NAME_LIMIT = 5

# Small counts as words, so the voice says "four figures", not "4 figures".
_COUNTS = {2: "two", 3: "three", 4: "four", 5: "five"}


def _count(n):
    return _COUNTS.get(n, str(n))


def brief(ahead, settings, clock=gold_strategy.uk_time):
    """The morning's word about [ahead] (today()'s events): what is due, and the windows it keeps the trader out.

    Several releases at one time -- CPI's four figures all land at 13:30 -- are one event with one window, said
    as a group, not four separate lines. Windows that touch are merged, so a run of releases is one span, and
    the times spoken are the real edges of the blackout, not "30 minutes before each". Returns "" when nothing
    is due. [clock] turns a UTC time into the clock shown, UK by default.
    """
    if not ahead:
        return ""

    before = timedelta(minutes=settings["news_minutes_before"])
    after = timedelta(minutes=settings["news_minutes_after"])

    # Group by the moment they land, in the order they come.
    groups = []

    for when, _currency, _impact, title in ahead:
        if groups and groups[-1][0] == when:
            groups[-1][1].append(title)
        else:
            groups.append((when, [title]))

    # What is due: each time, and what lands then.
    due = []

    for when, titles in groups:
        at = f"{clock(when):%H:%M}"

        if len(titles) == 1:
            due.append(f"{titles[0]} at {at}")
        elif len(titles) <= _NAME_LIMIT:
            due.append(f"{_count(len(titles))} figures at {at} ({_and_join(titles)})")
        else:
            due.append(f"{_count(len(titles))} releases at {at}")

    # The blackout windows, merged where they touch, as the real edges of the pause.
    spans = sorted((when - before, when + after) for when, _titles in groups)
    merged = [list(spans[0])]

    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    windows = _and_join(f"{clock(start):%H:%M} to {clock(end):%H:%M}" for start, end in merged)
    trades = "No new gold trades " + ("from " if len(merged) == 1 else "")

    return f"Gold today, sir: {_and_join(due)}. {trades}{windows}."
