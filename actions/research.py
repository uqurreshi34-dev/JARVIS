"""Provider-agnostic web research and report generation for JARVIS."""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote_plus

import requests

import providers

from actions import browser, files


_MAX_QUERIES = 5
_MAX_INITIAL_SOURCES = 10
_MAX_FOLLOWUP_QUERIES = 2
_MAX_SOURCE_CHARS = 7000
_SEARCH_TIMEOUT = 20
_SOURCE_TIMEOUT = 20

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
- Give each requested subject its own section.
- Include a direct comparison section.
- Cover the dimensions that matter to the user's request and the evidence
  available.
- Use concrete dates, figures, products, and other specifics when supported.
- Distinguish fact from interpretation.
- When sources disagree, describe the disagreement.
- Never invent facts, dates, figures, or URLs.
- End with a Sources section containing the supplied source titles and URLs.
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


def _bing_search(query):
    """Return structured search results without scraping a search-page DOM."""
    url = (
        "https://www.bing.com/search?format=rss&q="
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


def _search_queries(queries):
    """Gather unique structured results from all planned searches."""
    results = []
    seen_urls = set()

    for query in queries:
        print(f"[JARVIS] research search: {query}", flush=True)

        for result in _bing_search(query):
            url = result["url"]

            if url in seen_urls:
                continue

            seen_urls.add(url)
            results.append(result)

            if len(results) >= _MAX_INITIAL_SOURCES:
                return results

    return results


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


def _read_sources(results):
    """Read the most useful candidate pages and discard empty ones."""
    sources = []

    for result in results:
        source = _read_source(result)

        if source:
            sources.append(source)

    return sources


def _refine_queries(request, subjects, sources):
    """Ask the normal provider chain whether important research gaps remain."""
    evidence = []

    for source in sources[:8]:
        evidence.append(
            f"TITLE: {source['title']}\n"
            f"URL: {source['url']}\n"
            f"CONTENT:\n{source['text'][:2500]}"
        )

    if not evidence:
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
                        + "\n\n---\n\n".join(evidence)
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
    evidence = []

    for index, source in enumerate(sources, start=1):
        evidence.append(
            f"SOURCE {index}\n"
            f"TITLE: {source['title']}\n"
            f"URL: {source['url']}\n"
            f"SEARCH QUERY: {source['query']}\n"
            f"CONTENT:\n{source['text']}"
        )

    prompt = (
        f"USER REQUEST:\n{str(request or '').strip()}\n\n"
        f"SUBJECTS:\n{', '.join(subjects)}\n\n"
        "SOURCE MATERIAL:\n"
        + "\n\n---\n\n".join(evidence)
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
    planned = _plan_queries(request)

    if not planned:
        return None

    subjects, queries = planned
    candidates = _search_queries(queries)
    sources = _read_sources(candidates)

    if not sources:
        print("[JARVIS] research found no readable sources")
        return None

    followups = _refine_queries(request, subjects, sources)

    if followups:
        followup_candidates = _search_queries(followups)
        followup_sources = _read_sources(followup_candidates)

        seen = {source["url"] for source in sources}

        for source in followup_sources:
            if source["url"] not in seen:
                sources.append(source)
                seen.add(source["url"])

    report = _report(request, subjects, sources)

    if not report:
        return None

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
