"""Research uses only sources that are about what was searched.

Real reports were written from junk: for "Pakistan GDP 2024" the search
returned an adult video site and the BBC News home page; for "Aston
Villa" it returned Aston University and holiday villas. Checked here, with
every web call faked (no network, no search credits spent):

- with TAVILY_API_KEY set, Tavily is used, with its adult filter on and the
  page text requested, and that text is used without fetching the page;
- without a key, Bing is used, with its strict adult filter;
- a spent or refused Tavily key falls back to Bing for the rest of the run;
- off-topic pages are left out: name collisions and home pages go, the
  page about the subject stays -- and when every page is junk, none stay
  (a lone name collision, with no better page beside it, can still pass:
  see the note below);
- without the local model, pages are kept as before;
- a research run whose sources are all off-topic writes no report;
- the writer is told to cite every claim, to say when a notable claim rests
  on one source, and to leave the Sources list to JARVIS;
- a figure that the cited source does not contain is marked unverified in
  the saved report, and source numbers are never read aloud.

    python tools/test_research_sources.py
"""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

os.environ.setdefault("GROQ_API_KEY", "test")

from actions import research, semantic_memory  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


VILLA = (
    "Aston Villa Football Club is a professional football club based in Aston, Birmingham, England. It was founded in "
    "1874. Its team compete in the Premier League and have played at their home ground, Villa Park, since 1897. Aston "
    "Villa is one of the oldest and most successful clubs in England, having won the Football League First Division "
    "seven times, the FA Cup seven times, the League Cup five times, as well as the European Cup, European Super Cup, "
    "and Europa League once. They became only the fourth English club to win the European Cup, in 1981-82."
)

ASTON_UNIVERSITY = (
    "Homepage | Aston University Skip to main content Study locations: Birmingham | London | Online Find a course "
    "Search Menu International students Clearing Applying to Aston Scholarships Student Portal Alumni Donate Courses "
    "Undergraduate Open Days Why choose Aston? Student life Welcome Week Accommodation Student Union Careers Research "
    "at Aston. Aston ranked 1st in the UK for PhD student satisfaction. Take a video tour of the Aston campus, located "
    "in the centre of Birmingham. University of the Year for Student Success. 21st in the UK."
)

BIRMINGHAM_CITY = (
    "Birmingham is a city and metropolitan borough in the centre of the West Midlands region of England. It is the "
    "largest local authority district in England by population and the second-largest city in Britain, with a "
    "population of 1.2 million people in 2024. Birmingham grew in the 18th century during the Industrial Revolution. "
    "Its five universities make it the largest centre of higher education outside London. Birmingham was the host "
    "city for the 2022 Commonwealth Games."
)

NEWS_HOME = (
    "BBC News Home. Skip to content. Home News Sport Weather iPlayer Sounds. Top stories. Chancellor sets out autumn "
    "budget plans as borrowing rises. Live: Storm Amy brings strong winds and heavy rain to northern England. Two men "
    "charged after shooting in south London. Man City beat Arsenal in dramatic late finish. Most read. Tesla shares "
    "fall after quarterly results. Floods hit Pakistan's Sindh province after monsoon rains. Royal Mail fined."
)

DICTIONARY = (
    "difference noun. UK US. the way in which two or more things which you are comparing are not the same: What's "
    "the difference between an ape and a monkey? a difference in amount: There's a big difference in price. a "
    "disagreement: We have our differences. Idioms: make a difference, split the difference. Synonyms: contrast."
)

PAKISTAN = (
    "Pakistan had a population of 247,541,947 according to the final results of the 2023 Pakistani census. Pakistan "
    "is the world's fifth-most populous country. Between 1951 and 2023, Pakistan's population expanded over sevenfold, "
    "going from 33.7 million to 247.5 million. Due to a high fertility rate, Pakistan has one of the world's youngest "
    "populations. The median age of the country was 19."
)


def source(url, text, query):
    return {"title": "", "url": url, "text": text, "query": query}


class Response:
    def __init__(self, status, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.headers = {"content-type": "text/html"}
        self.text = content.decode("utf-8", "ignore") if content else ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise research.requests.HTTPError(str(self.status_code))


calls = []


def fake_post(url, timeout=None, headers=None, json=None):
    calls.append(("post", url, headers, json))
    return next_post()


def fake_get(url, timeout=None, headers=None):
    calls.append(("get", url, headers, None))

    rss = (
        "<rss><channel><item><title>Aston Villa F.C.</title><link>https://example.org/villa</link>"
        "<description>club</description></item></channel></rss>"
    ).encode()

    if "bing.com" in url:
        return Response(200, content=rss)

    return Response(200, content=f"<html><body>{VILLA}</body></html>".encode())


def tavily_ok():
    return Response(200, {"results": [
        {"title": "Aston Villa F.C.", "url": "https://example.org/villa", "content": "club", "raw_content": VILLA},
        {"title": "Aston University", "url": "https://example.org/aston", "content": "uni", "raw_content": ASTON_UNIVERSITY},
    ]})


next_post = tavily_ok
research.requests.post = fake_post
research.requests.get = fake_get
research.browser.available = lambda: False

# ---- which search ------------------------------------------------------------

os.environ["TAVILY_API_KEY"] = "tvly-test"
research._tavily_resting = False
calls.clear()
results = research._search("Aston Villa FC honours history")

post = [call for call in calls if call[0] == "post"]
check(len(post) == 1 and post[0][1] == research._TAVILY_URL, "with a key, Tavily is searched")
check(post and post[0][2].get("Authorization") == "Bearer tvly-test", "the key is sent as a bearer token")
check(post and post[0][3].get("safe_search") is True, "Tavily's adult-content filter is on")
check(post and post[0][3].get("include_raw_content") == "text", "the page text is requested with the results")
check(post and post[0][3].get("search_depth") == "basic", "basic search, one credit each")

calls.clear()
read = research._from_result(results[0])
check(read and read["text"].startswith("Aston Villa Football Club") and not calls,
      "the text Tavily sent is used without fetching the page")

del os.environ["TAVILY_API_KEY"]
calls.clear()
results = research._search("Aston Villa FC honours history")
got = [call for call in calls if call[0] == "get"]
check(got and "bing.com" in got[0][1] and "adlt=strict" in got[0][1] and not [c for c in calls if c[0] == "post"],
      "without a key, Bing is used, with its strict adult filter")

os.environ["TAVILY_API_KEY"] = "tvly-spent"
research._tavily_resting = False
next_post = lambda: Response(432, {"detail": "limit"})
calls.clear()
research._search("first")
research._search("second")
posts = [call for call in calls if call[0] == "post"]
bings = [call for call in calls if call[0] == "get" and "bing.com" in call[1]]
check(len(posts) == 1 and len(bings) == 2, "a spent Tavily allowance falls back to Bing, and is not asked again this run")
del os.environ["TAVILY_API_KEY"]
next_post = tavily_ok

# ---- which sources are kept -------------------------------------------------------

model = semantic_memory._encode(["probe"]) is not None

if not model:
    print("SKIP relevance checks (the local model is unavailable here)")
else:
    query = "Aston Villa FC honours history"
    kept = research._relevant(query, [
        source("https://example.org/aston", ASTON_UNIVERSITY, query),
        source("https://example.org/villa", VILLA, query),
        source("https://example.org/birmingham", BIRMINGHAM_CITY, query),
    ])
    check([one["url"] for one in kept] == ["https://example.org/villa"],
          f"name collisions are left out and the club's page is kept: {[one['url'] for one in kept]}")

    query = "Pakistan GDP 2024 World Bank"
    kept = research._relevant(query, [
        source("https://example.org/news", NEWS_HOME, query),
        source("https://example.org/dictionary", DICTIONARY, query),
    ])
    check(kept == [], "when every page is junk, none is kept")

    query = "Pakistan population 2024"
    kept = research._relevant(query, [
        source("https://example.org/news", NEWS_HOME, query),
        source("https://example.org/pakistan", PAKISTAN, query),
    ])
    check([one["url"] for one in kept] == ["https://example.org/pakistan"], "a real page beside junk is kept alone")

    # A known limit, kept visible rather than tuned away: a name collision
    # that is the only page for its search scores 0.45 here, above the 0.40
    # floor, and has no better page to be measured against. Raising the
    # floor to catch it would risk dropping real sources. Tavily, which ranks
    # by meaning, is what keeps these out upstream.

real = semantic_memory.similarities
semantic_memory.similarities = lambda query, texts: None

try:
    everything = [source("https://example.org/news", NEWS_HOME, "x"), source("https://example.org/villa", VILLA, "x")]
    check(research._relevant("x", everything) == everything, "without the model, pages are kept as before")
finally:
    semantic_memory.similarities = real

# ---- a whole run -------------------------------------------------------------------

if model:
    asked = []

    def fake_chat(messages, **kwargs):
        asked.append(messages[0]["content"][:40])

        if "research planner" in messages[0]["content"]:
            return '{"subjects": ["Pakistan", "India"], "queries": ["Pakistan GDP 2024", "Pakistan population 2024"]}'

        return "a report"

    research._chat = fake_chat
    os.environ["TAVILY_API_KEY"] = "tvly-test"
    research._tavily_resting = False
    next_post = lambda: Response(200, {"results": [
        {"title": "BBC News", "url": f"https://example.org/news{len(calls)}", "content": "", "raw_content": NEWS_HOME},
        {"title": "Dictionary", "url": f"https://example.org/dict{len(calls)}", "content": "", "raw_content": DICTIONARY},
    ]})

    check(research.run("research Pakistan, compare it with India and write a report") is None,
          "a run whose sources are all off-topic writes no report")
    check(not any("research analyst" in one for one in asked), "and the report writer is never asked")
    check(research.failure_message() and "reliable sources" in research.failure_message(),
          f"and JARVIS can say why: {research.failure_message()!r}")

# ---- citations and figures ------------------------------------------------------

rules = research._REPORT_SYSTEM
check("[2]" in rules and "one source" in rules and "Do not write a Sources section" in rules,
      "the writer is told to cite claims, flag single-source claims, and leave the Sources list alone")

cited_sources = [
    {"text": "Aston Villa won the UEFA Europa Conference League in 2024. Founded 1874."},
    {"text": "Villa Park capacity is 42,640. Revenue in 2024/25 was 378 million pounds. European Cup 1982."},
]
draft = (
    "Villa were founded in 1874 [1] and won the European Cup in 1982 [2]. Revenue reached GBP 378m in 2024/25 [2].\n"
    "Villa Park holds 45,000 fans [2]. Villa won the League Cup in 1996 [1][2].\n"
    "| European Cup | 1982 |\n"
    "According to one source, Villa won the Conference League in 2024 [1]."
)
checked, marked = research.check_citations(draft, cited_sources)
check(marked == 2, f"two sentences with figures their sources lack are marked ({marked})")
check("45,000 fans [2]" + research._UNVERIFIED + "." in checked, "a wrong capacity is marked where it is claimed")
check("1996 [1][2]" + research._UNVERIFIED + "." in checked, "a figure neither cited source has is marked")
check("founded in 1874 [1] and won the European Cup in 1982 [2]. Revenue" in checked,
      "figures the cited sources contain are left as written")
check("| European Cup | 1982 |" in checked, "lines without citations are left alone")
check(research.check_citations("Nothing to see [9].", cited_sources)[1] == 0, "a citation to no real source marks nothing")

from actions import report_search  # noqa: E402

spoken = report_search._clean("Founded in 1874 [1] and European champions in 1982 [1][2], per [2-3].")
check(spoken == "Founded in 1874 and European champions in 1982, per.", f"source numbers are not read aloud: {spoken!r}")

if model:
    written = {}

    def cited_chat(messages, **kwargs):
        if "research planner" in messages[0]["content"]:
            return '{"subjects": ["Aston Villa"], "queries": ["Aston Villa FC honours history"]}'

        if "gap analyst" in messages[0]["content"]:
            return '{"queries": []}'

        return "# Aston Villa\n## Summary\nVilla were founded in 1874 [1]. Villa Park holds 45,000 fans [1]."

    real_write = research.files.write

    def keep(name, content, **kwargs):
        written["content"] = content
        return "/tmp/report.docx"

    research._chat = cited_chat
    research.files.write = keep
    next_post = lambda: Response(200, {"results": [
        {"title": "Aston Villa F.C.", "url": "https://example.org/villa-run", "content": "", "raw_content": VILLA},
    ]})

    try:
        research.run("research Aston Villa and write a report")
    finally:
        research.files.write = real_write

    saved = written.get("content", "")
    check("45,000 fans [1]" + research._UNVERIFIED in saved, "a real run marks the unsupported figure in the saved report")
    check("founded in 1874 [1]." in saved, "and leaves the supported one as written")

sys.exit(1 if failures else 0)
