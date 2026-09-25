"""Provider-agnostic web research and report generation for JARVIS."""

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote_plus

import requests

import providers

from actions import browser, evidence, files


_MAX_QUERIES = 5
_MAX_INITIAL_SOURCES = 10
_MAX_SOURCES_PER_QUERY = 2
_MAX_FOLLOWUP_QUERIES = 2
# How much of each page is read. The whole of it, within reason: the part
# that answers the question is often well below the top.
_MAX_SOURCE_CHARS = 60000

# How much of each page the model is shown, after evidence.reduce has kept
# only the passages that bear on the request.
_REPORT_EVIDENCE_CHARS = 2800
_REFINE_EVIDENCE_CHARS = 1000
_SEARCH_TIMEOUT = 20
_SOURCE_TIMEOUT = 20

# Candidates looked at for each search, of which the most relevant
# _MAX_SOURCES_PER_QUERY are kept.
_CANDIDATES_PER_QUERY = 4

# A page is used only when its best passage is about what was searched.
# Measured with the local model: on-topic pages scored 0.67 to 0.87, home
# pages and dictionary pages 0.29 or less, and name collisions (Aston
# University for "Aston Villa", the city of Birmingham for "Birmingham
# City FC") 0.44 to 0.51 -- which the margin below the best page for the
# same search removes. Only the opening of each page is scored: enough to
# tell what it is about, and quick.
_RELEVANCE_FLOOR = 0.40
_RELEVANCE_MARGIN = 0.12
_RELEVANCE_PASSAGES = 40

# Tavily: a search API made for this, with an adult-content filter and the
# page text returned with each result. Used when TAVILY_API_KEY is set;
# otherwise, or once its monthly limit is reached, Bing's RSS feed is used.
_TAVILY_URL = "https://api.tavily.com/search"
_tavily_resting = False

# Why the last run wrote no report, for JARVIS to say instead of "that
# didn't work". None when there is nothing more specific to say.
_last_failure = None


def failure_message():
    """What to say about the last run that wrote no report, or None."""
    return _last_failure

_RESEARCH_WORDS = (
    "research",
    "compare",
    "comparison",
    "investigate",
    "analyse",
    "analyze",
    "look into",
    "find out about",
)

_REPORT_WORDS = (
    "report",
    "write up",
    "write a report",
    "save it",
    "save the",
)

_FOLDER_RE = re.compile(
    r"\b(?:save|store|put)\b.*?\b(?:in|inside|into|under|to)\s+"
    r"(?:the\s+)?([A-Za-z0-9][A-Za-z0-9 _-]{0,79}?)\s+folder\b",
    re.IGNORECASE,
)

_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "subjects": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 4,
        },
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": _MAX_QUERIES,
        },
    },
    "required": ["subjects", "queries"],
    "additionalProperties": False,
}

_REFINEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": _MAX_FOLLOWUP_QUERIES,
        },
    },
    "required": ["queries"],
    "additionalProperties": False,
}

_PLAN_SYSTEM = """
You are JARVIS's research planner.

Turn the user's request into a small set of high-value web-search queries.

Rules:
- Preserve the requested subjects exactly; do not invent new ones.
- Prefer primary and authoritative sources when appropriate: company investor
  relations, regulatory filings, official product pages, regulators, and
  reputable reporting.
- Cover the facts needed to answer the actual request, including current
  developments when relevant.
- Make queries specific enough to retrieve useful evidence, not generic.
- Do not answer the user's question. Return only the requested JSON.
"""

_REFINEMENT_SYSTEM = """
You are JARVIS's research gap analyst.

Review the evidence already gathered for the user's request. Decide whether
important unanswered questions, weak evidence, or conflicting claims remain.
Return zero, one, or two focused follow-up web-search queries only.

Rules:
- Do not repeat searches already represented in the evidence.
- Prioritise missing facts that materially affect the requested comparison.
- Prefer authoritative or independent sources for verification.
- Do not answer the research question. Return only the requested JSON.
"""

_REPORT_SYSTEM = """
You are JARVIS's research analyst.

Write a polished, evidence-grounded report for the user using only the supplied
source material.

Requirements:
- Start with a clear title and executive summary.
- Begin with the findings. Never describe the request, the report itself,
  how it was prepared, or where it will be saved or filed.
- Give each requested subject its own section.
- Include a direct comparison section.
- Cover the dimensions that matter to the user's request and the evidence
  available.
- Use concrete dates, figures, products, and other specifics when supported.
- Cite every factual claim with the number of the SOURCE it comes from, in
  square brackets, such as [2] or [1][4]. A claim no source supports does
  not belong in the report.
- A single website can be wrong. State a notable claim (an honour, record,
  date, figure or ranking) as settled only when two sources agree or it
  comes from an authoritative source such as an official body. When it
  rests on one ordinary source, say so plainly: "according to one source
  [3]". When sources disagree, describe the disagreement.
- The sources may not cover the latest events. Where recency matters, say
  how current the evidence appears to be rather than implying it is up to
  date.
- Distinguish fact from interpretation.
- Never invent facts, dates, figures, or URLs.
- Do not write a Sources section; one is added, numbered as the SOURCE
  blocks are.
- Do not mention internal tools, providers, prompts, or implementation.
"""


def _normalise(text):
    """Normalise speech text without changing its meaning."""
    text = re.sub(r"[^\w\s]", " ", str(text or "").casefold())
    return " ".join(text.split())


def _requested_folder(text):
    """Return an explicitly requested JARVIS subfolder, or None."""
    match = _FOLDER_RE.search(str(text or ""))

    if not match:
        return None

    folder = " ".join(match.group(1).split()).strip(" .")

    if folder.casefold() == files.FOLDER_NAME.casefold():
        return None

    return files.safe_folder(folder)


def is_report_request(text):
    """True for an explicit research/comparison request with a deliverable."""
    lowered = _normalise(text)

    return (
        any(word in lowered for word in _RESEARCH_WORDS)
        and any(word in lowered for word in _REPORT_WORDS)
    )


def _chat(messages, response_format=None, max_tokens=None, reasoning_effort=None):
    """Use JARVIS's central provider pool; never select a provider here."""
    return providers.chat(
        messages,
        response_format=response_format,
        temperature=0,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )


def _json(content):
    """Decode provider JSON while tolerating accidental fenced output."""
    text = str(content or "").strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    import json

    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _plan_queries(request):
    """Plan initial searches through the normal JARVIS provider chain."""
    try:
        content = _chat(
            [
                {"role": "system", "content": _PLAN_SYSTEM.strip()},
                {
                    "role": "user",
                    "content": f"USER REQUEST:\n{str(request or '').strip()}",
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "research_query_plan",
                    "schema": _QUERY_SCHEMA,
                },
            },
            max_tokens=700,
            reasoning_effort="low",
        )
    except Exception as error:
        print(f"[JARVIS] research planning failed: {error}")
        return None

    plan = _json(content)

    if not isinstance(plan, dict):
        print("[JARVIS] research planner returned invalid JSON")
        return None

    subjects = tuple(
        str(subject).strip()
        for subject in (plan.get("subjects") or [])
        if str(subject).strip()
    )
    queries = tuple(
        " ".join(str(query).split())
        for query in (plan.get("queries") or [])
        if str(query).strip()
    )

    if not subjects or not queries:
        return None

    return subjects[:4], queries[:_MAX_QUERIES]


def _search(query):
    """Search results for [query]: Tavily when it is set up, Bing otherwise."""
    results = _tavily_search(query)

    if results is None:
        results = _bing_search(query)

    return results


def _tavily_search(query):
    """Tavily results, or None when Tavily is not set up or not available."""
    global _tavily_resting

    key = (os.getenv("TAVILY_API_KEY") or "").strip()

    if not key or _tavily_resting:
        return None

    try:
        response = requests.post(
            _TAVILY_URL,
            timeout=_SEARCH_TIMEOUT + 10,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "query": str(query or "").strip(),
                "search_depth": "basic",
                "max_results": _CANDIDATES_PER_QUERY + 1,
                "include_raw_content": "text",
                "safe_search": True,
            },
        )
    except requests.RequestException as error:
        print(f"[JARVIS] Tavily search failed, using Bing: {error}")
        return None

    if response.status_code in (401, 429, 432, 433):
        # A bad key or a spent allowance will not recover mid-session.
        _tavily_resting = True
        print(f"[JARVIS] Tavily unavailable ({response.status_code}), using Bing for now")
        return None

    if response.status_code != 200:
        print(f"[JARVIS] Tavily search failed ({response.status_code}), using Bing")
        return None

    try:
        items = response.json().get("results") or []
    except ValueError:
        print("[JARVIS] Tavily returned invalid JSON, using Bing")
        return None

    results = []

    for item in items:
        title = str(item.get("title") or "").strip()
        link = str(item.get("url") or "").strip()

        if not title or not link:
            continue

        results.append({
            "title": title,
            "url": link,
            "snippet": " ".join(str(item.get("content") or "").split()),
            "query": query,
            "text": str(item.get("raw_content") or "").strip(),
        })

    return results


def _bing_search(query):
    """Return structured search results without scraping a search-page DOM."""
    # adlt=strict is Bing's own adult-content filter.
    url = (
        "https://www.bing.com/search?format=rss&adlt=strict&q="
        f"{quote_plus(str(query or '').strip())}"
    )

    try:
        response = requests.get(
            url,
            timeout=_SEARCH_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 JARVIS Research",
            },
        )
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"[JARVIS] research search failed: {error}")
        return []

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        print("[JARVIS] research search returned invalid RSS")
        return []

    results = []

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = re.sub(
            r"<[^>]+>",
            " ",
            item.findtext("description") or "",
        )
        description = " ".join(description.split())

        if not title or not link:
            continue

        results.append({
            "title": title,
            "url": link,
            "snippet": description,
            "query": query,
        })

    return results


def _gather(queries, seen=None):
    """Sources for [queries]: searched, read, and only those on topic kept."""
    seen = set() if seen is None else seen
    sources = []

    for query in queries:
        print(f"[JARVIS] research search: {query}", flush=True)

        read = []

        for result in _search(query):
            url = result["url"]

            if url in seen:
                continue

            seen.add(url)
            source = _from_result(result)

            if source:
                read.append(source)

            if len(read) >= _CANDIDATES_PER_QUERY:
                break

        sources.extend(_relevant(query, read)[:_MAX_SOURCES_PER_QUERY])

        if len(sources) >= _MAX_INITIAL_SOURCES:
            break

    return sources[:_MAX_INITIAL_SOURCES]


def _from_result(result):
    """A source from a search result, using its text when the search sent it."""
    text = str(result.get("text") or "").strip()

    if len(text) >= 200:
        return {
            "title": result.get("title", ""),
            "url": result["url"],
            "text": text[:_MAX_SOURCE_CHARS],
            "query": result.get("query", ""),
        }

    return _read_source(result)


def _relevant(query, sources):
    """The sources that are about [query], most relevant first.

    Search engines return pages that only share a name with what was asked
    (Aston University for "Aston Villa") or nothing to do with it at all
    (a news home page, a dictionary). Each page is judged by its best
    passage, with the local model: it must reach _RELEVANCE_FLOOR and be
    within _RELEVANCE_MARGIN of the best page for the same search. Without
    the model, every page is kept, as before.
    """
    if not sources:
        return []

    from actions import semantic_memory

    scores = []

    for source in sources:
        passages = evidence.passages(f"{source.get('title', '')}. {source['text']}")[:_RELEVANCE_PASSAGES]

        try:
            similarity = semantic_memory.similarities(query, passages) if passages else None
        except Exception as error:
            print(f"[JARVIS] research relevance check failed, keeping sources: {error}")
            return list(sources)

        if similarity is None:
            return list(sources)

        scores.append(max(similarity) if similarity else 0.0)

    best = max(scores)
    ranked = sorted(zip(scores, range(len(sources))), reverse=True)
    kept = [
        sources[index] for score, index in ranked
        if score >= _RELEVANCE_FLOOR and score >= best - _RELEVANCE_MARGIN
    ]
    dropped = [sources[index]["url"] for score, index in ranked if sources[index] not in kept]

    if dropped:
        print(f"[JARVIS] research left out {len(dropped)} off-topic source(s): {', '.join(dropped)}", flush=True)

    return kept


def _read_source(result):
    """Read a source through JARVIS's browser, with a direct HTTP fallback."""
    url = result.get("url")

    if not url:
        return None

    try:
        if browser.available() and browser.attached():
            browser.navigate(url)
            title, current_url, body = browser.page_text()

            body = str(body or "").strip()

            if body:
                return {
                    "title": str(title or result.get("title") or "").strip(),
                    "url": str(current_url or url).strip(),
                    "text": body[:_MAX_SOURCE_CHARS],
                    "query": result.get("query", ""),
                }
    except Exception as error:
        print(f"[JARVIS] browser source failed: {error}")

    try:
        response = requests.get(
            url,
            timeout=_SOURCE_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 JARVIS Research",
            },
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").casefold()

        if "text/html" not in content_type and "text/plain" not in content_type:
            return None

        body = re.sub(r"<script[\s\S]*?</script>", " ", response.text,
                      flags=re.IGNORECASE)
        body = re.sub(r"<style[\s\S]*?</style>", " ", body,
                      flags=re.IGNORECASE)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"&nbsp;", " ", body, flags=re.IGNORECASE)
        body = re.sub(r"&amp;", "&", body, flags=re.IGNORECASE)
        body = " ".join(body.split())

        if len(body) < 200:
            return None

        return {
            "title": result.get("title", ""),
            "url": url,
            "text": body[:_MAX_SOURCE_CHARS],
            "query": result.get("query", ""),
        }

    except requests.RequestException as error:
        print(f"[JARVIS] direct source fetch failed: {error}")
        return None


def _focus(request, subjects, source):
    """What a page is being read for: the request, its subjects, the search."""
    return " ".join(
        part for part in (
            str(request or "").strip(),
            " ".join(subjects),
            str(source.get("query") or "").strip(),
        ) if part
    )


def _refine_queries(request, subjects, sources):
    """Ask the normal provider chain whether important research gaps remain."""
    gathered = []
    seen = set()

    for source in sources[:8]:
        passages = evidence.reduce(
            source["text"], _focus(request, subjects, source),
            _REFINE_EVIDENCE_CHARS, seen,
        )

        if passages:
            gathered.append(
                f"TITLE: {source['title']}\n"
                f"URL: {source['url']}\n"
                f"CONTENT:\n{passages}"
            )

    if not gathered:
        return ()

    try:
        content = _chat(
            [
                {"role": "system", "content": _REFINEMENT_SYSTEM.strip()},
                {
                    "role": "user",
                    "content": (
                        f"USER REQUEST:\n{str(request or '').strip()}\n\n"
                        f"SUBJECTS:\n{', '.join(subjects)}\n\n"
                        "CURRENT EVIDENCE:\n"
                        + "\n\n---\n\n".join(gathered)
                    ),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "research_gap_plan",
                    "schema": _REFINEMENT_SCHEMA,
                },
            },
            max_tokens=350,
            reasoning_effort="low",
        )
    except Exception as error:
        print(f"[JARVIS] research refinement failed: {error}")
        return ()

    plan = _json(content)

    if not isinstance(plan, dict):
        return ()

    return tuple(
        " ".join(str(query).split())
        for query in (plan.get("queries") or [])
        if str(query).strip()
    )[:_MAX_FOLLOWUP_QUERIES]


def _report(request, subjects, sources):
    """Generate the final report through JARVIS's central provider chain."""
    gathered = []
    seen = set()

    for index, source in enumerate(sources, start=1):
        passages = evidence.reduce(
            source["text"], _focus(request, subjects, source),
            _REPORT_EVIDENCE_CHARS, seen,
        )

        if not passages:
            continue

        gathered.append(
            f"SOURCE {index}\n"
            f"TITLE: {source['title']}\n"
            f"URL: {source['url']}\n"
            f"SEARCH QUERY: {source['query']}\n"
            f"CONTENT:\n{passages}"
        )

    prompt = (
        f"USER REQUEST:\n{str(request or '').strip()}\n\n"
        f"SUBJECTS:\n{', '.join(subjects)}\n\n"
        "SOURCE MATERIAL:\n"
        + "\n\n---\n\n".join(gathered)
    )

    try:
        report = _chat(
            [
                {"role": "system", "content": _REPORT_SYSTEM.strip()},
                {"role": "user", "content": prompt},
            ],
            max_tokens=6000,
            reasoning_effort="medium",
        )
    except Exception as error:
        print(f"[JARVIS] research report generation failed: {error}")
        return None

    return str(report or "").strip() or None


# A citation as the writer is asked to give it: [2], [1][4], [1, 4], [2-3].
_CITATION = re.compile(r"\[(\d+(?:\s*[,\u2013-]\s*\d+)*)\]")

# Figures that must be in the source a sentence cites: years, amounts,
# counts, percentages. Single digits are left out; they are too often
# ordinals, list markers and words written as numbers.
_FIGURE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")

_UNVERIFIED = " (unverified: this figure is not in the cited source)"


def _cited(marks, count):
    """The source numbers a citation names, within 1..count."""
    numbers = set()

    for part in re.split(r"\s*,\s*", marks):
        bounds = re.split(r"\s*[\u2013-]\s*", part)

        try:
            low, high = int(bounds[0]), int(bounds[-1])
        except ValueError:
            continue

        if high - low > 20:
            continue

        numbers.update(n for n in range(low, high + 1) if 1 <= n <= count)

    return numbers


def _plain_number(figure):
    return figure.replace(",", "").rstrip(".")


def check_citations(report, sources):
    """The report with sentences whose figures their sources lack marked.

    Every figure in a sentence that cites sources must appear in at least
    one of them. The writer is told to cite every claim; this checks the
    ones with numbers in, where a slip does most harm. A figure missing
    from the cited source is marked in the report rather than removed, so
    nothing is silently changed. Returns (report, how many were marked).
    """
    texts = [
        " ".join(_plain_number(figure) for figure in _FIGURE.findall(str(source.get("text") or "")))
        for source in sources
    ]
    figures_in = [set(text.split()) for text in texts]
    marked = 0
    lines = []

    for line in str(report or "").split("\n"):
        pieces = re.split(r"(?<=[.!?])(\s+)", line)
        rebuilt = []

        for piece in pieces:
            citations = list(_CITATION.finditer(piece))

            if not citations or piece.isspace():
                rebuilt.append(piece)
                continue

            cited = set()

            for citation in citations:
                cited |= _cited(citation.group(1), len(sources))

            claimed = {
                _plain_number(figure) for figure in _FIGURE.findall(_CITATION.sub(" ", piece))
                if len(_plain_number(figure)) >= 2
            }
            supported = set().union(*(figures_in[n - 1] for n in cited)) if cited else set()

            if cited and claimed - supported:
                end = len(piece.rstrip())
                closing = end - 1 if piece.rstrip().endswith((".", "!", "?")) else end
                piece = piece[:closing] + _UNVERIFIED + piece[closing:]
                marked += 1

            rebuilt.append(piece)

        lines.append("".join(rebuilt))

    return "\n".join(lines), marked


def _sources_section(sources):
    """Append the exact URLs actually supplied to the report."""
    lines = ["", "Sources", ""]

    for index, source in enumerate(sources, start=1):
        lines.append(f"{index}. {source['title']} — {source['url']}")

    return "\n".join(lines)


def _safe_filename_part(text):
    """Keep arbitrary research text safe for a Windows filename."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(text or ""))
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:70] or "research"


def run(request):
    """Research a request and save a source-backed report in JARVIS's folder."""
    global _last_failure

    _last_failure = None
    planned = _plan_queries(request)

    if not planned:
        return None

    subjects, queries = planned
    seen = set()
    sources = _gather(queries, seen)

    if not sources:
        print("[JARVIS] research found no readable, on-topic sources")
        _last_failure = (
            "I couldn't find reliable sources on that, sir, so I haven't "
            "written a report. It may be worth wording it differently."
        )
        return None

    followups = _refine_queries(request, subjects, sources)

    if followups:
        sources.extend(_gather(followups, seen))

    report = _report(request, subjects, sources)

    if not report:
        return None

    report, marked = check_citations(report, sources)

    if marked:
        print(f"[JARVIS] research marked {marked} sentence(s) whose figures the cited source lacks", flush=True)

    report = report.rstrip() + "\n" + _sources_section(sources)

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    filename = (
        f"{_safe_filename_part(' vs '.join(subjects[:2]))} "
        f"research report {stamp}.docx"
    )
    destination = _requested_folder(request)

    path = files.write(
        filename,
        report,
        default_suffix=".docx",
        folder=destination,
    )

    if not path:
        return None

    print(
        f"[JARVIS] research report written to {path} "
        f"({len(sources)} sources)",
        flush=True,
    )

    return {
        "path": path,
        "subjects": subjects,
        "sources": len(sources),
        "folder": destination,
    }
