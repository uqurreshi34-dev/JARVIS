""""What did my report say about deep sleep?" -- saved reports, searched by meaning.

Checked:

- the ways of asking are recognised, with the topic and any report named;
- ordinary commands, and requests to write a new report, are left alone;
- reports are split by their own sections: no passage runs across a
  heading, tables and bullets are read, and the Sources list is left out;
- reports are found in the JARVIS folder and its Reports folder;
- each topic finds the right section of the right report, by meaning
  ("electricity" finds "Energy use"), and unrelated topics find nothing;
- naming a report ("my ESP32 report") searches only that one;
- the spoken answers, "which reports" answers and the not-found answers;
- without the model, the topic's own words still find passages;
- where commands.py can be imported, these questions are answered from
  the reports and never start a new research run.

Runs in a sandboxed JARVIS folder with reports written the way JARVIS
writes them: Markdown lines, one per Word paragraph.

    python tools/test_report_search.py
"""

import os
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import report_search, semantic_memory  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


try:
    from docx import Document
except ImportError:
    print("SKIP report checks (python-docx is not installed)")
    sys.exit(0)


def save(where, name, text):
    os.makedirs(where, exist_ok=True)
    document = Document()

    for line in text.split("\n"):
        document.add_paragraph(line)

    document.save(os.path.join(where, name))


ESP32 = """# ESP32 Power Consumption Research Report
## Executive summary
The ESP32-WROOM-32 draws between 160 and 260 milliamps while transmitting over Wi-Fi, which makes Wi-Fi the dominant cost in any battery design.
## Active and transmit current
During Wi-Fi transmission peaks can briefly exceed 300 milliamps, so the supply must deliver short bursts without the voltage sagging below 3.0 volts. Brownout resets are the most common symptom of an undersized regulator or a long thin USB cable.
## Deep sleep and wake sources
Deep sleep keeps only the RTC controller and RTC memory powered. The board can wake on a timer, a touch pad, or an external pin such as a PIR motion sensor output.
| Board | Deep sleep current |
|---|---|
| Bare WROOM-32 module | 10 microamps |
| DevKit board | 8 to 15 milliamps |
## Choosing a battery
A 2000 mAh lithium cell running a sensor that wakes every five minutes lasts roughly three to five weeks on a bare module.
## Recommendations
- Use a low quiescent current regulator such as the MCP1700.
- Remove or bypass the power LED.
## Sources
1. Espressif ESP32 datasheet - https://www.espressif.com/esp32"""

CRYPTO = """# Bitcoin vs Ethereum Research Report
## Summary
Bitcoin is designed primarily as a store of value with a fixed supply of 21 million coins, while Ethereum is a programmable platform.
## Supply and issuance
Bitcoin's issuance halves roughly every four years; the most recent halving reduced the block reward to 3.125 bitcoin.
## Energy use
Bitcoin's proof of work mining consumes a large amount of electricity. Ethereum's switch to proof of stake in 2022 cut its energy use by more than 99 percent.
## Fees and speed
Ethereum transaction fees rise sharply when the network is congested.
## Sources
1. Bitcoin whitepaper - https://bitcoin.org/bitcoin.pdf"""

DASHCAM = """# Dash Cam Comparison Research Report
**Summary**
For most drivers a front and rear dual-channel dash cam recording at 1440p offers the best balance of detail and storage.
**Storage and memory cards**
Dash cams overwrite old footage in a loop, so a high-endurance microSD card rated for continuous writing is essential.
**Insurance**
Some UK insurers offer a small discount for fitted dash cams.
**Sources**
1. Which? dash cam reviews - https://www.which.co.uk/reviews/dash-cams"""

reports = os.path.join(folder, "Reports")
save(reports, "ESP32 power consumption research report 2026-09-20-101500.docx", ESP32)
save(folder, "Bitcoin vs Ethereum research report 2026-09-22-201000.docx", CRYPTO)
save(reports, "Dash cams research report 2026-08-30-090000.docx", DASHCAM)
save(folder, "shopping list.docx", "# Shopping\nmilk, eggs")  # not a report
save(os.path.join(folder, "Documents"), "~$temp report.docx", "x")  # Word's lock file

TODAY = date(2026, 9, 25)

# ---- recognising the question ---------------------------------------------

ASKED = {
    "what did my report say about deep sleep": ("say", "deep sleep", None),
    "Jarvis, what did my research report say about deep sleep?": ("say", "deep sleep", None),
    "what does my esp32 report say about batteries": ("say", "batteries", "esp32"),
    "what did the bitcoin vs ethereum report say about fees": ("say", "fees", "bitcoin vs ethereum"),
    "what do my reports say about energy use": ("say", "energy use", None),
    "what did the research say about memory cards": ("say", "memory cards", None),
    "search my reports for insurance": ("say", "insurance", None),
    "find deep sleep in my reports": ("say", "deep sleep", None),
    "which reports mention insurance": ("which", "insurance", None),
    "which of my reports talk about electricity": ("which", "electricity", None),
    "do any of my reports mention brownouts": ("which", "brownouts", None),
}

for said, expected in ASKED.items():
    check(report_search.question(said) == expected, f"asked: {said!r} -> {expected} (got {report_search.question(said)})")

LEFT_ALONE = [
    "research esp32 power and write a report", "write me a research report on dash cams",
    "give me a market report on bitcoin", "open my reports folder", "what did i note about deep sleep",
    "when did i last ask about reports", "what did my report say about it", "read my notes",
]

for said in LEFT_ALONE:
    check(report_search.question(said) is None, f"left alone: {said!r}")

# ---- reading the reports -------------------------------------------------------

found = report_search.report_files()
names = sorted(os.path.basename(path) for path in found)
check(len(found) == 3 and all("research report" in name for name in names),
      f"the three reports are found, in the folder and in Reports: {names}")

label, day = report_search.describe_report(found[-1])
check((label, day) == ("ESP32 power consumption", date(2026, 9, 20)), f"a report's subject and date come from its name: {label}, {day}")

esp32 = [path for path in found if "ESP32" in path][0]
parts = report_search._read(esp32)
headings = [heading for heading, _passage in parts]
check("Deep sleep and wake sources" in headings and "Choosing a battery" in headings, f"sections keep their headings: {headings}")
check(all("Choosing a battery" not in passage and "Recommendations" not in passage for _h, passage in parts),
      "no passage runs across a heading")
check(not any("espressif.com" in passage for _h, passage in parts), "the Sources list is left out")
check(any("Board: DevKit board, Deep sleep current: 8 to 15 milliamps." in passage for _h, passage in parts),
      "table rows are read with their column names")
check(any(passage.startswith("Use a low quiescent current regulator") for _h, passage in parts), "bullets are read without their marks")

dashcam = [path for path in found if "Dash" in path][0]
check([h for h, _p in report_search._read(dashcam)] == ["Summary", "Storage and memory cards", "Insurance"],
      "bold lines are headings too, and Sources is still left out")

# ---- finding the passage -------------------------------------------------------

model = semantic_memory._encode(["probe"]) is not None

FINDS = {
    "deep sleep": ("ESP32", "Deep sleep and wake sources"),
    "battery life": ("ESP32", "Choosing a battery"),
    "how long will a battery last": ("ESP32", "Choosing a battery"),
    "brownouts": ("ESP32", "Active and transmit current"),
    "wifi current": ("ESP32", "Active and transmit current"),
    "energy use": ("Bitcoin", "Energy use"),
    "electricity": ("Bitcoin", "Energy use"),
    "halving": ("Bitcoin", "Supply and issuance"),
    "fees": ("Bitcoin", "Fees and speed"),
    "memory cards": ("Dash", "Storage and memory cards"),
    "sd cards": ("Dash", "Storage and memory cards"),
    "insurance": ("Dash", "Insurance"),
}

NOTHING = ["football", "recipes", "the weather", "my holiday", "quantum computing", "javascript", "tax returns"]

if not model:
    print("SKIP meaning checks (the local model is unavailable here)")
else:
    for topic, (report, heading) in FINDS.items():
        results = report_search.search(topic)
        got = (os.path.basename(results[0][1])[:7], results[0][2]) if results else None
        check(bool(results) and os.path.basename(results[0][1]).startswith(report) and results[0][2] == heading,
              f"finds: {topic!r} -> {report}, {heading!r} (got {got})")

    for topic in NOTHING:
        check(report_search.search(topic) == [], f"nothing about {topic!r}")

    results = report_search.search("energy use", subject="esp32")
    check(all("ESP32" in path for _s, path, _h, _p in results), "naming a report searches only that report")

    # ---- what is said ----------------------------------------------------------

    said = report_search.answer("what did my report say about deep sleep", today=TODAY)
    check(said.startswith("From your ESP32 power consumption report from 20 September, sir: Deep sleep keeps only"),
          f"say: {said!r}")

    said = report_search.answer("what does my dash cam report say about insurance", today=TODAY)
    check(said.startswith("From your Dash cams report from 30 August, sir: Some UK insurers"), f"a named report: {said!r}")

    said = report_search.answer("which reports mention electricity", today=TODAY)
    check(said.startswith("One report mentions electricity, sir: Bitcoin vs Ethereum, 22 September"), f"which: {said!r}")

    said = report_search.answer("what did my report say about football", today=TODAY)
    check(said == "I can't find anything about football in your reports, sir.", f"nothing found: {said!r}")

    said = report_search.answer("what does my tesla report say about range", today=TODAY)
    check(said == "I can't find a report on tesla, sir.", f"a report that does not exist: {said!r}")

# ---- a folder like yours: one Villa report among many football reports ---------

VILLA = """# Aston Villa FC vs. Birmingham City FC - Comparative Briefing
*Prepared for filing under: Football*
---
## Executive Summary
This report was commissioned as a head-to-head comparison of Aston Villa Football Club and Birmingham City Football Club, Birmingham's two long-established senior men's clubs.
**Aston Villa are the more successful club**, with seven league titles and the 1982 European Cup, while Birmingham City's major honour is the League Cup, won in 1963 and 2011.
---
## Aston Villa
**What the evidence supports**
- Villa Park holds around 42,000 spectators and the club returned to European football in 2024 under Unai Emery.
## Birmingham City
Birmingham City play at St Andrew's and were promoted back to the Championship in 2025 after one season in League One.
## Sources
1. Aston Villa official site - https://www.avfc.co.uk"""

PAKISTAN = """# Pakistan vs India
## Summary
This report was commissioned to research Pakistan, compare it with India, and file the result under the country folder. The report has been prepared and is presented below for the country folder.
Pakistan has a population of about 240 million, the fifth largest in the world, and its capital is Islamabad.
## History
*Interpretation (not stated by the source):* Several of these sites are conventionally located in territory that is today Pakistan.
## Sources
1. Britannica - https://www.britannica.com"""

football = os.path.join(folder, "Football")
save(football, "Aston Villa Football Club vs Birmingham City Football Club research report 2026-09-16-120000.docx", VILLA)
for club, fact in (("Manchester United Football Club", "Old Trafford is the largest club ground in England."),
                   ("Liverpool Football Club", "Anfield has been Liverpool's home since 1892."),
                   ("Arsenal Football Club", "Arsenal moved to the Emirates Stadium in 2006."),
                   ("Chelsea Football Club", "Stamford Bridge sits in Fulham, west London."),
                   ("Leeds United Football Club", "Leeds won the league title in 1992.")):
    save(football, f"{club} research report 2026-09-10-100000.docx",
         f"# {club}\n## Summary\nThis football club report covers the club and its history. {fact}\n## Sources\n1. x - https://example.com")
save(os.path.join(folder, "country"), "Pakistan vs India research report 2026-09-24-150000.docx", PAKISTAN)

if model:
    said = report_search.answer("what does my report say about aston villa football club", today=TODAY)
    check("other reports mention it" not in said and "Another report" not in said,
          f"sharing 'football' and 'club' does not make other reports mention Aston Villa: {said!r}")
    check("commissioned" not in said and "Prepared for filing" not in said,
          f"sentences about the report itself are left out: {said!r}")
    check("*" not in said, "no asterisks are read out")
    check(said.startswith("From your Aston Villa Football Club vs Birmingham City Football Club report from 16 September, sir: Aston Villa are")
          and "seven league titles" in said,
          f"one side of a comparison finds what the report found about it: {said[:160]!r}")

    said = report_search.answer("what does my report say about villa park", today=TODAY)
    check(said.startswith("From your Aston Villa") and "42,000" in said, f"a topic inside it finds the passage: {said[:140]!r}")

    said = report_search.answer("what does my report say about pakistan", today=TODAY)
    check("commissioned" not in said and "presented below" not in said and "*" not in said,
          f"Pakistan: no sentences about the report, no asterisks: {said!r}")
    check("240 million" in said or "Islamabad" in said or "territory that is today Pakistan" in said,
          f"Pakistan: what the report found is read: {said!r}")

    said = report_search.answer("what does my report say about aston villa vs birmingham city", today=TODAY)
    check(said.startswith("In short, from your Aston Villa Football Club vs Birmingham City Football Club report")
          and "seven league titles" in said and "commissioned" not in said,
          f"naming a report in full reads its summary of findings: {said!r}")

    said = report_search.answer("which reports mention anfield", today=TODAY)
    check(said.startswith("One report mentions anfield, sir: Liverpool Football Club"), f"which, among many: {said!r}")

    said = report_search.answer("what did my report say about football clubs", today=TODAY)
    check(said.startswith("From "), f"a general topic still finds something: {said[:80]!r}")

# ---- without the model -----------------------------------------------------------

real = semantic_memory.similarities
semantic_memory.similarities = lambda query, texts: None

try:
    results = report_search.search("halving")
    check(bool(results) and results[0][2] == "Supply and issuance", "without the model, the topic's own word still finds it")
    check(report_search.search("night vision") == [], "without the model, meaning alone finds nothing, and nothing crashes")
finally:
    semantic_memory.similarities = real

# ---- the real fast path, where it can be imported -----------------------------

try:
    import commands
except Exception as error:  # needs JARVIS's full Windows environment
    print(f"SKIP fast-path wiring (could not import commands: {error})")
else:
    result = commands._fast_path("Jarvis, what did my research report say about deep sleep?")
    check(bool(result) and result["intent"] == "search_reports" and result.get("text") == "deep sleep",
          "fast path: a question about reports goes to search_reports with its topic")

    result = commands.handle_command("what did my research report say about deep sleep")
    check(bool(result) and result.get("intent") == "search_reports",
          f"it is answered from the reports and never starts a new research run (got {result and result.get('intent')})")

    result = commands.handle_command("research esp32 power and write a report")
    check(bool(result) and result.get("intent") == "research_report", "asking for a new report still researches one")

for path in report_search.report_files():
    os.remove(path)

check(report_search.answer("what did my report say about deep sleep", today=TODAY) == "You have no saved reports yet, sir.",
      "no reports at all is said plainly")

sys.exit(1 if failures else 0)
