"""Research-and-report workflow built on JARVIS's existing browser and files.

This module deliberately knows nothing about NVIDIA, AMD, or any other
specific company. It turns a natural-language research request into a small
set of evidence-seeking searches, gathers readable source pages through the
existing JARVIS browser, then asks the configured language provider to
synthesise a report from that evidence.
"""

import json
import re
from datetime import datetime

import providers

from actions import browser, files


_MAX_QUERIES = 5
_MAX_RESULTS_PER_QUERY = 2
_MAX_SOURCE_CHARS = 9000


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


_PLAN_SYSTEM = """
You are JARVIS's research-query planner.

Turn the user's research/report request into a small set of web-search queries
that will gather evidence for the requested comparison or investigation.

Rules:
- Preserve the user's subjects exactly; do not invent additional companies or
  entities.
- Prefer primary or authoritative sources when the query can target them:
  company investor relations, filings, official product pages, regulators,
  and reputable financial/news reporting.
- Cover the core facts needed for a useful comparison: business, current
  products/positioning, financial or operating performance when relevant,
  competitive position, recent developments, and important risks.
- Keep queries concise and non-duplicative.
- Do not answer the research question. Only produce subjects and searches.
- Return only JSON matching the supplied schema.
"""


_REPORT_SYSTEM = """
You are JARVIS's research analyst.

Write a clear comparison report using only the evidence supplied below.
The report is for the user, not for another model.

Requirements:
- Title the report clearly.
- Begin with an executive summary.
- Give each subject a concise standalone section before comparing them.
- Include a direct comparison section covering the most meaningful differences.
- Cover current products/business position, financial or operating evidence
  present in the sources, competitive position, recent developments, and
  risks when supported by evidence.
- Prefer concrete dates, numbers, and named products when they appear in the
  sources.
- Distinguish confirmed facts from interpretation.
- Do not invent or fill gaps with unsupported facts.
- When sources disagree, say so rather than choosing silently.
- End with a Sources section listing the source title and URL for every source
  actually used.
- Do not mention internal tools, prompts, or implementation.
"""


def _research_gate(text):
    """Cheap generic gate for an explicit research/report request."""
    lowered = re.sub(r"[^\w\s]", " ", str(text or "").casefold())
    lowered = " ".join(lowered.split())

    research_words = (
        "research", "compare", "comparison", "investigate",
        "analyse", "analyze", "look into", "find out about",
    )
    report_words = (
        "report", "write up", "write a report", "save it", "save the",
    )

    return (
        any(word in lowered for word in research_words)
        and any(word in lowered for word in report_words)
    )


def is_report_request(text):
    """True for an explicit research/comparison request with a deliverable."""
    return _research_gate(text)


def _plan_queries(request):
    """Ask the configured provider for a bounded evidence-gathering plan."""
    try:
        content = providers.chat(
            [
                {"role": "system", "content": _PLAN_SYSTEM},
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
            temperature=0,
            max_tokens=700,
            reasoning_effort="low",
        )
    except Exception as error:
        print(f"[JARVIS] research planning failed: {error}")
        return None

    try:
        plan = json.loads(content or "")
    except (ValueError, TypeError):
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


def _search_sources(queries):
    """Run searches and collect readable pages from their top results."""
    sources = []
    seen_urls = set()

    for query in queries:
        print(f"[JARVIS] research search: {query}", flush=True)

        outcome = browser.search(query)
        if not outcome:
            continue

        results = browser.search_results()

        for result in results[:_MAX_RESULTS_PER_QUERY]:
            url = str(result.get("url") or "").strip()

            if not url or url in seen_urls:
                continue

            seen_urls.add(url)

            try:
                browser.navigate(url)
                title, current_url, body = browser.page_text()
            except Exception as error:
                print(f"[JARVIS] research source failed: {error}")
                continue

            body = str(body or "").strip()
            current_url = str(current_url or url).strip()

            if not body:
                continue

            sources.append({
                "title": str(title or result.get("title") or "").strip(),
                "url": current_url,
                "text": body[:_MAX_SOURCE_CHARS],
                "query": query,
            })

    return sources


def _safe_filename_part(text):
    """Keep a subject suitable for a Windows filename."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(text or ""))
    cleaned = " ".join(cleaned.split()).strip(" .")

    return cleaned[:80] or "research"


def _write_report(request, subjects, sources):
    """Synthesise evidence into a DOCX in JARVIS's normal working folder."""
    if not sources:
        return None

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
        "EVIDENCE:\n"
        + "\n\n---\n\n".join(evidence)
    )

    try:
        report = providers.chat(
            [
                {"role": "system", "content": _REPORT_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=5000,
            reasoning_effort="medium",
        )
    except Exception as error:
        print(f"[JARVIS] research report generation failed: {error}")
        return None

    report = str(report or "").strip()

    if not report:
        return None

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    safe_subjects = " vs ".join(
        _safe_filename_part(subject)
        for subject in subjects[:2]
    )
    filename = f"{safe_subjects} research report {stamp}.docx"

    path = files.write(
        filename,
        report,
        default_suffix=".docx",
    )

    if path:
        print(f"[JARVIS] research report written to {path}", flush=True)

    return path


def run(request):
    """Research a user request and write the resulting report."""
    plan = _plan_queries(request)

    if not plan:
        return None

    subjects, queries = plan
    sources = _search_sources(queries)

    if not sources:
        print("[JARVIS] research found no readable sources")
        return None

    path = _write_report(request, subjects, sources)

    if not path:
        return None

    return {
        "path": path,
        "subjects": subjects,
        "sources": len(sources),
    }
