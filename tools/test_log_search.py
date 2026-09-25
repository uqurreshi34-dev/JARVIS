""""When did I last ask about bitcoin?" -- the command log, searched by meaning.

Checked:

- the ways of asking are recognised, with their topic and time window,
  and ordinary commands are left alone;
- the vector store encodes each text once, keeps it across restarts, and
  never mixes one model's vectors with another's;
- over a realistic log of 600 commands, each topic is counted exactly:
  everything about it, nothing that is not -- by meaning ("crypto" finds
  bitcoin) and by its own word ("chrome" finds "close chrome");
- questions about the log itself are never counted as asking about a topic;
- time windows (today, yesterday, this week ...) narrow the search;
- the spoken answers, including when nothing is found;
- without the model, the topic's own word still finds commands;
- where commands.py can be imported, the fast path sends these here.

Runs in a sandboxed JARVIS folder, never your real log.

    python tools/test_log_search.py
"""

import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import log_search, semantic_memory, vector_store  # noqa: E402

import numpy as np  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


# ---- recognising the question ---------------------------------------------

ASKED = {
    "when did i last ask about bitcoin": ("last", "bitcoin", None),
    "Jarvis, when did I last ask you about the weather?": ("last", "the weather", None),
    "when was the last time i asked you about github": ("last", "github", None),
    "when did i last ask you to open chrome": ("last", "open chrome", None),
    "when did i ask about the markets": ("last", "the markets", None),
    "have i asked you about bitcoin before": ("ever", "bitcoin", None),
    "have i ever asked about football": ("ever", "football", None),
    "did i ask you about the weather yesterday": ("ever", "the weather", "yesterday"),
    "how many times have i asked about bitcoin": ("count", "bitcoin", None),
    "how often do i ask you about the weather": ("count", "the weather", None),
    "how many times did i ask about github this week": ("count", "github", "this week"),
    "what did i ask you about the markets today": ("list", "the markets", "today"),
    "have i mentioned my car to you": None,  # "to you" is not a topic word order we claim
}

for said, expected in ASKED.items():
    check(log_search.question(said) == expected, f"asked: {said!r} -> {expected}")

LEFT_ALONE = [
    "whats the bitcoin price", "ask chatgpt about bitcoin", "when is my dentist appointment",
    "what did i note about the boiler", "what do you know about me", "open chrome",
    "have i got any reminders", "how many files do i have", "when did i last ask about it",
    "tell me about bitcoin", "what is the weather", "read the log",
]

for said in LEFT_ALONE:
    check(log_search.question(said) is None, f"left alone: {said!r}")

# ---- the vector store --------------------------------------------------------

encoded = []


def counting(texts):
    encoded.extend(texts)
    return semantic_memory._encode(texts)


if semantic_memory._encode(["probe"]) is None:
    print("SKIP vector store and meaning checks (the local model is unavailable here)")
    model = False
else:
    model = True
    first = vector_store.vectors(["open chrome", "whats the weather", "open chrome"], counting, "test-model")
    check(encoded == ["open chrome", "whats the weather"], "each new text is encoded once, duplicates once")
    check(first.shape[0] == 3 and np.allclose(first[0], first[2]), "vectors come back in the order asked")

    encoded.clear()
    vector_store.close()  # as if JARVIS restarted
    again = vector_store.vectors(["whats the weather", "open chrome"], counting, "test-model")
    check(encoded == [] and np.allclose(again[1], first[0]), "after a restart, saved vectors are read, not encoded")
    check(os.path.exists(os.path.join(folder, vector_store.FILENAME)), "the store is a file in the JARVIS folder")

    vector_store.vectors(["open chrome"], counting, "another-model")
    check(encoded == ["open chrome"], "another model's vectors are never reused")

    many = [f"command number {n}" for n in range(200)]
    encoded.clear()
    batches = []

    def batching(texts):
        batches.append(len(texts))
        return semantic_memory._encode(texts)

    vector_store.vectors(many, batching, "test-model")
    check(max(batches) <= vector_store.BATCH, f"a long list is encoded in batches of {vector_store.BATCH} at most")

    check(vector_store.vectors(["new text"], lambda texts: None, "test-model") is None,
          "no model means no vectors, not a crash")

# ---- a realistic log ---------------------------------------------------------

GROUPS = {
    "bitcoin": ["whats the bitcoin price", "how is bitcoin doing", "bitcoin price please",
                "give me a market report on bitcoin"],
    "chrome": ["open chrome", "close chrome", "open google chrome"],
    "weather": ["whats the weather like", "will it rain tomorrow", "whats the weather in london"],
    "github": ["what are my github issues", "list my github repositories", "any new pull requests on github"],
    "music": ["play the next song", "pause the music", "close spotify"],
    "other": ["what time is it", "take a screenshot", "whats in my clipboard", "whats on my calendar",
              "how much battery do i have", "show me the news", "what planes are overhead",
              "set a timer for ten minutes", "recite surah al fatiha", "turn the volume up",
              "model a chair in blender", "proofread this document", "read my notes"],
}

TODAY = date(2026, 9, 25)
NOW = datetime(2026, 9, 25, 20, 0)
random.seed(7)
log = []

for step in range(600):
    group = random.choice(list(GROUPS))
    log.append((NOW - timedelta(minutes=37 * step), random.choice(GROUPS[group]), "x"))

# Questions about the log are logged too, and must never count.
log.append((NOW - timedelta(minutes=5), "when did i last ask about bitcoin", "search_log"))
log.append((NOW - timedelta(minutes=4), "how many times have i asked about github", "x"))
log.sort()


def write_log(entries):
    with open(os.path.join(folder, "jarvis-log.txt"), "w", encoding="utf-8") as handle:
        handle.write("2026-09-01 08:00:00  action           make_note: x  -> ok\n")
        for moment, said, intent in entries:
            handle.write(f"{moment:%Y-%m-%d %H:%M:%S}  command          {said!r} -> {intent} (local)\n")


write_log(log)


def counted(topic, window=None):
    return log_search.search(topic, window=window, today=TODAY)


def expected(group, window=None):
    return [item for item in log if item[1] in GROUPS[group] and log_search._in_window(item[0], window, TODAY)]


if model:
    for topic, group in (("bitcoin", "bitcoin"), ("crypto", "bitcoin"), ("chrome", "chrome"),
                         ("the browser", "chrome"), ("github", "github"), ("music", "music")):
        found = counted(topic)
        check(found == expected(group), f"counted exactly: {topic!r} -> {len(found)} of {len(expected(group))}"
              + ("" if found == expected(group) else f" (wrong: {sorted({i[1] for i in found} - set(GROUPS[group]))},"
                 f" missed: {sorted(set(GROUPS[group]) - {i[1] for i in found})})"))

    for topic in ("football", "my code", "quantum physics"):
        check(counted(topic) == [], f"nothing about {topic!r}")

    check(all(item[2] != "search_log" and "when did i" not in item[1] and "how many times" not in item[1]
              for item in counted("bitcoin") + counted("github")),
          "questions about the log are never counted")

    found = counted("bitcoin", "yesterday")
    check(found == expected("bitcoin", "yesterday") and all(i[0].date() == TODAY - timedelta(days=1) for i in found),
          f"'yesterday' narrows the search ({len(found)})")
    check(counted("bitcoin", "this week") == expected("bitcoin", "this week"), "'this week' narrows the search")
    check(counted("bitcoin", "last month") == [], "a window with nothing in it finds nothing")

# ---- what is said ------------------------------------------------------------

short = [
    (datetime(2026, 9, 20, 9, 0), "open chrome", "x"),
    (datetime(2026, 9, 24, 21, 15), "whats the bitcoin price", "x"),
    (datetime(2026, 9, 25, 8, 30), "how is bitcoin doing", "x"),
    (datetime(2026, 9, 25, 8, 31), "when did i last ask about bitcoin", "search_log"),
]
write_log(short)

said = log_search.answer("when did I last ask about bitcoin", today=TODAY)
check(said == "Today at eight thirty AM, sir. You said: how is bitcoin doing.", f"last: {said!r}")

said = log_search.answer("how many times have I asked about bitcoin", today=TODAY)
check(said == "Twice, sir. Most recently today at eight thirty AM: how is bitcoin doing.", f"count: {said!r}")

said = log_search.answer("have I asked you about bitcoin before", today=TODAY)
check(said.startswith("Yes, sir, twice."), f"ever: {said!r}")

said = log_search.answer("what did I ask you about bitcoin", today=TODAY)
check("how is bitcoin doing, today at eight thirty AM; whats the bitcoin price, yesterday at nine fifteen PM" in said,
      f"list: {said!r}")

# Read the way the voice says it: "four oh five PM", never "four five".
check(log_search._spoken_moment(datetime(2026, 9, 23, 16, 5), TODAY) == "on 23 September at four oh five PM",
      "a time a few minutes past the hour keeps its 'oh'")

said = log_search.answer("did I ask about chrome yesterday", today=TODAY)
check(said == "I can't find you asking about chrome yesterday, sir.", f"nothing in the window: {said!r}")

said = log_search.answer("have I ever asked about football", today=TODAY)
check(said == "I can't find you asking about football, sir. The log goes back to 20 September.",
      f"nothing at all says how far back the log goes: {said!r}")

said = log_search.answer("when did I last ask about my car", today=TODAY)
check("your car" in said, f"'my' is said back as 'your': {said!r}")

said = log_search.answer("when's the last time I spoke to you regarding bitcoin", topic="bitcoin", today=TODAY)
check(said.startswith("Today at eight thirty AM"), f"a question worded differently, via the model: {said!r}")

said = log_search.answer("how often do I bring up bitcoin", topic="bitcoin", today=TODAY)
check(said.startswith("Twice"), f"the wording still picks the answer's kind: {said!r}")

# ---- without the model ---------------------------------------------------------

real = semantic_memory.similarities
semantic_memory.similarities = lambda query, texts: None

try:
    check([i[1] for i in log_search.search("bitcoin", today=TODAY)] == ["whats the bitcoin price", "how is bitcoin doing"],
          "without the model, the topic's own word still finds commands")
    check(log_search.search("crypto", today=TODAY) == [], "without the model, meaning alone finds nothing")
finally:
    semantic_memory.similarities = real

os.remove(os.path.join(folder, "jarvis-log.txt"))
check(log_search.answer("when did I last ask about bitcoin", today=TODAY)
      == "There's nothing in the log to search yet, sir.", "an empty log is said plainly")

# ---- the real fast path, where it can be imported -----------------------------

try:
    import commands
except Exception as error:  # needs JARVIS's full Windows environment
    print(f"SKIP fast-path wiring (could not import commands: {error})")
else:
    result = commands._fast_path("Jarvis, when did I last ask about bitcoin?")
    check(bool(result) and result["intent"] == "search_log" and result.get("text") == "bitcoin",
          "fast path: a question about the log goes to search_log with its topic")

    # The topic names another skill; the question about it must still win.
    for said, topic in (("when did i last ask about planes overhead", "planes overhead"),
                        ("when did i last ask you to close the radar", "close the radar")):
        result = commands._fast_path(said)
        check(bool(result) and result["intent"] == "search_log" and result.get("text") == topic,
              f"fast path: {said!r} searches the log rather than acting")

    # Naming a connected service inside a question about the log must not
    # send it to that service's tools (it went to GitHub's, slowly and wrongly).
    import json
    from actions import mcp_services

    with open(os.path.join(folder, mcp_services.CONFIG_NAME), "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": {"github": {"url": "https://example.invalid"}}}, handle)
    mcp_services.close()

    for said in ("how many times have i asked about github", "what did i note about github"):
        result = commands.handle_command(said)
        check(bool(result) and result.get("intent") in ("search_log", "read_notes"),
              f"answered locally, not by the service: {said!r} -> {result and result.get('intent')}")

    result = commands.handle_command("list my github repositories")
    check(not result or result.get("intent") == "agent_mode",
          f"a real request for the service still goes to it -> {result and result.get('intent')}")

    os.remove(os.path.join(folder, mcp_services.CONFIG_NAME))
    mcp_services.close()

    result = commands._fast_path("whats the bitcoin price")
    check(not result or result["intent"] != "search_log", "fast path: asking about bitcoin itself is unchanged")

vector_store.close()
sys.exit(1 if failures else 0)
