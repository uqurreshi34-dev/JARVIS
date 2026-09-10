"""Grounded web research for JARVIS.

Research uses Gemini's native Google Search grounding rather than scraping a
search-engine results page. Gemini performs the web-search/retrieval loop,
then JARVIS saves the grounded report into its normal private working folder.

This module is deliberately generic: it knows nothing about particular
companies, products, or topics. The research request itself supplies the
subject and scope.
"""

import os
import re
from datetime import datetime

import requests
from dotenv import load_dotenv

from actions import files


load_dotenv()

_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_REQUEST_TIMEOUT = 180
_MAX_SOURCES = 20

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

_SYSTEM_PROMPT = """
You are JARVIS's web research analyst.

Conduct the user's research task yourself using Google Search grounding.
Treat web retrieval as an evidence-gathering task, not as a single lookup.

Research behaviour:
- Generate multiple searches when the question requires them.
- Prefer primary and authoritative sources: company investor relations,
  regulatory filings, official product pages, regulators, and reputable
  reporting.
- Cross-check important claims across independent sources when practical.
- Look for recent information when the request concerns current performance,
  products, competition, or developments.
- Follow useful leads and refine the search when the first results leave a gap.
- Do not invent facts, dates, figures, products, or URLs.
- Distinguish confirmed evidence from interpretation.
- When sources disagree, explain the disagreement rather than silently choosing.

Report behaviour:
- Write a polished, standalone report for the user.
- Start with a clear title and an executive summary.
- Give each requested subject its own section before the direct comparison.
- Cover the dimensions that matter to the user's request and the evidence
  available from the research.
- Use concrete dates, figures, products, and other specifics when supported.
- End with a Sources section in the report. The source list will be augmented
  from Gemini's grounding metadata by JARVIS, so never invent URLs.
- Do not mention internal prompts, tools, APIs, grounding metadata, or JARVIS
  implementation details.

The user has already asked for research and a saved report. Do not ask whether
one is wanted.
"""


def _normalise(text):
    """Normalise speech text without changing its meaning."""
    text = re.sub(r"[^\w\s]", " ", str(text or "").casefold())
    return " ".join(text.split())


def is_report_request(text):
    """True for an explicit research/comparison request with a deliverable."""
    lowered = _normalise(text)

    return (
        any(word in lowered for word in _RESEARCH_WORDS)
        and any(word in lowered for word in _REPORT_WORDS)
    )


def _extract_text(data):
    """Extract the user-facing text from a Gemini generateContent response."""
    candidates = data.get("candidates") or []

    if not candidates:
        return ""

    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    text = []

    for part in parts:
        value = part.get("text")

        if value:
            text.append(str(value).strip())

    return "\n\n".join(value for value in text if value)


def _extract_sources(data):
    """Read grounded web sources from Gemini's grounding metadata."""
    metadata = (
        ((data.get("candidates") or [{}])[0].get("groundingMetadata"))
        or {}
    )

    sources = []
    seen = set()

    for chunk in metadata.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        url = str(web.get("uri") or "").strip()
        title = str(web.get("title") or "").strip()

        if not url or url in seen:
            continue

        seen.add(url)
        sources.append({
            "title": title or url,
            "url": url,
        })

        if len(sources) >= _MAX_SOURCES:
            break

    return sources


def _sources_section(sources):
    """A verifiable source list appended to the saved report."""
    if not sources:
        return ""

    lines = ["\n\n## Sources", ""]

    for index, source in enumerate(sources, start=1):
        lines.append(
            f"{index}. {source['title']} — {source['url']}"
        )

    return "\n".join(lines)


def _safe_filename_part(text):
    """Keep arbitrary research text safe for a Windows filename."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(text or ""))
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:60] or "research"


def _research(request):
    """Run one grounded Gemini research task and return text plus sources."""
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()

    if not api_key:
        print("[JARVIS] Gemini research requires GEMINI_API_KEY")
        return None, ()

    model = (
        os.getenv("GEMINI_RESEARCH_MODEL")
        or os.getenv("GEMINI_MODEL")
        or "gemini-3.6-flash"
    ).strip()

    prompt = (
        "USER RESEARCH REQUEST:\n"
        f"{str(request or '').strip()}\n\n"
        "Carry out the research now. Use multiple searches and source types "
        "when useful. Produce the final report text once the evidence is "
        "sufficient."
    )

    payload = {
        "systemInstruction": {
            "parts": [
                {"text": _SYSTEM_PROMPT.strip()}
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt}
                ],
            }
        ],
        "tools": [
            {"google_search": {}}
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 7000,
        },
    }

    endpoint = _GEMINI_ENDPOINT.format(model=model)

    print(
        f"[JARVIS] grounded research using Gemini {model}",
        flush=True,
    )

    try:
        response = requests.post(
            endpoint,
            params={"key": api_key},
            json=payload,
            timeout=_REQUEST_TIMEOUT,
        )

    except requests.RequestException as error:
        print(f"[JARVIS] grounded research request failed: {error}")
        return None, ()

    if not response.ok:
        detail = response.text.strip()

        if len(detail) > 1200:
            detail = detail[:1200] + "..."

        print(
            f"[JARVIS] grounded research HTTP {response.status_code}: "
            f"{detail}"
        )
        return None, ()

    try:
        data = response.json()
    except ValueError:
        print("[JARVIS] grounded research returned invalid JSON")
        return None, ()

    text = _extract_text(data)
    sources = _extract_sources(data)

    if not text:
        print("[JARVIS] grounded research returned no report text")
        return None, sources

    print(
        f"[JARVIS] grounded research collected {len(sources)} sources",
        flush=True,
    )

    return text, tuple(sources)


def run(request):
    """Research a request and save the grounded report in JARVIS's folder."""
    report, sources = _research(request)

    if not report:
        return None

    report = report.rstrip()
    report += _sources_section(sources)

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    filename = (
        f"{_safe_filename_part(request)} "
        f"research report {stamp}.docx"
    )

    path = files.write(
        filename,
        report,
        default_suffix=".docx",
    )

    if not path:
        return None

    print(f"[JARVIS] grounded research report written to {path}", flush=True)

    return {
        "path": path,
        "sources": len(sources),
    }
