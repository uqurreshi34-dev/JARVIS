"""Headlines from free RSS feeds.

No key, no quota, no account: the same shape as the weather lookup. Feeds are
fetched on demand and cached briefly, since asking twice in a minute should
not mean two round trips.
"""

import re
import time
import xml.etree.ElementTree as ElementTree
from html import unescape

import requests


TIMEOUT = 6

# Headlines are cached for this long, so repeated questions are instant.
CACHE_SECONDS = 300

HEADLINE_COUNT = 10

# Free, keyless feeds. Each region may list several, and they are merged.
FEEDS = {
    "uk": [
        ("BBC", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
        ("Sky News", "https://feeds.skynews.com/feeds/rss/uk.xml"),
    ],
    "world": [
        ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Sky News", "https://feeds.skynews.com/feeds/rss/world.xml"),
    ],
    "america": [
        ("BBC", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
        ("Sky News", "https://feeds.skynews.com/feeds/rss/us.xml"),
    ],
    "europe": [
        ("BBC", "https://feeds.bbci.co.uk/news/world/europe/rss.xml"),
    ],
    "technology": [
        ("BBC", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
        ("Sky News", "https://feeds.skynews.com/feeds/rss/technology.xml"),
    ],
    "business": [
        ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ],
    "sport": [
        ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
    ],
    "science": [
        ("BBC", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    ],
    "politics": [
        ("BBC", "https://feeds.bbci.co.uk/news/politics/rss.xml"),
    ],
    "health": [
        ("BBC", "https://feeds.bbci.co.uk/news/health/rss.xml"),
    ],
}

DEFAULT_REGION = "uk"

# Spoken words mapped to a region above.
REGIONS = {
    "uk": "uk", "britain": "uk", "british": "uk", "england": "uk",
    "home": "uk", "local": "uk", "united kingdom": "uk",
    "world": "world", "global": "world", "international": "world",
    "america": "america", "american": "america", "us": "america",
    "usa": "america", "united states": "america", "states": "america",
    "europe": "europe", "european": "europe", "eu": "europe",
    "technology": "technology", "tech": "technology",
    "business": "business", "finance": "business", "money": "business",
    "sport": "sport", "sports": "sport", "football": "sport",
    "science": "science", "environment": "science",
    "politics": "politics", "political": "politics",
    "health": "health", "nhs": "health", "medical": "health",
}

_TAGS = re.compile(r"<[^>]+>")

_cache = {}


def region_for(word):
    """Map a spoken word to a feed region, or None."""
    return REGIONS.get((word or "").strip().casefold())


def _clean(text):
    """Strip markup and tidy whitespace out of a feed value."""
    if not text:
        return ""

    return " ".join(unescape(_TAGS.sub(" ", text)).split())


def _parse(xml_text, source):
    """Turn feed XML into a list of headline dictionaries."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        print(f"[JARVIS] could not read the {source} feed: {error}")
        return []

    items = []

    # RSS puts items under channel; Atom uses entry at the top level.
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]

        if tag not in ("item", "entry"):
            continue

        title = ""
        summary = ""

        for child in item:
            name = child.tag.rsplit("}", 1)[-1]

            if name == "title" and not title:
                title = _clean(child.text)
            elif name in ("description", "summary") and not summary:
                summary = _clean(child.text)

        if title:
            items.append({
                "title": title,
                "summary": summary,
                "source": source,
            })

    return items


def _fetch(url, source):
    try:
        response = requests.get(
            url,
            timeout=TIMEOUT,
            headers={"User-Agent": "JARVIS/1.0"},
        )
        response.raise_for_status()

    except requests.RequestException as error:
        print(f"[JARVIS] could not reach {source}: {error}")
        return []

    return _parse(response.text, source)


def headlines(region=DEFAULT_REGION, limit=HEADLINE_COUNT, refresh=False):
    """Top headlines for a region, newest feeds merged."""
    region = region if region in FEEDS else DEFAULT_REGION

    cached = _cache.get(region)

    if cached and not refresh and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1][:limit]

    collected = []
    seen = set()

    for source, url in FEEDS[region]:
        for item in _fetch(url, source):
            key = item["title"].casefold()

            if key in seen:
                continue

            seen.add(key)
            collected.append(item)

    if collected:
        _cache[region] = (time.monotonic(), collected)
    elif cached:
        # The network failed, so stale headlines beat none at all.
        return cached[1][:limit]

    return collected[:limit]


def describe(region=DEFAULT_REGION, spoken=2):
    """A short spoken summary; the panel shows the full list."""
    items = headlines(region)

    if not items:
        return "I couldn't reach the news, sir."

    label = "" if region == DEFAULT_REGION else f"{region} "

    if len(items) == 1:
        return f"One {label}headline, sir: {items[0]['title']}."

    lead = ". ".join(item["title"] for item in items[:spoken])

    return (
        f"Here are the top {len(items)} {label}stories, sir. {lead}."
    )
