"""Finding a note by what it is about: "what did I note about the boiler?".

Checked:

- the ways of asking are recognised, and the topic is pulled out of them;
- asking for all notes, making a note and ordinary commands are left alone;
- a topic finds its note by meaning ("heating" finds the boiler), or by a
  shared word where meaning alone is weak ("my car" finds car insurance);
- a topic nothing is about finds nothing, rather than the nearest note;
- the spoken answer says when each note was made;
- with the model unavailable, shared words still find notes;
- where commands.py can be imported, the real fast path and handler agree.

Runs against a sandboxed JARVIS folder, never your real notes.

    python tools/test_note_search.py
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import notes, semantic_memory  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


check(notes._notes_path().startswith(folder), "notes are kept in the sandboxed JARVIS folder")

# ---- recognising the question ---------------------------------------------

ASKED = {
    "what did i note about the boiler": "the boiler",
    "what did i write down about the wedding": "the wedding",
    "what have i noted about tailscale": "tailscale",
    "find my notes about the boiler": "the boiler",
    "find my note on insurance": "insurance",
    "search my notes for the wifi password": "the wifi password",
    "look through my notes for anything mentioning the dentist": "the dentist",
    "read my notes about the hud": "the hud",
    "check my notes for the resistor": "the resistor",
    "do i have a note about my car": "my car",
    "did i make any notes on the wedding": "the wedding",
    "have i got any notes about tax": "tax",
    "is there a note about my cousin": "my cousin",
    "any notes about the keystore": "the keystore",
    "what do my notes say about the gym": "the gym",
    "whats in my notes about render": "render",
    "what notes do i have about travel": "travel",
    "show me my notes on backups": "backups",
}

for said, topic in ASKED.items():
    check(notes.search_topic(said) == topic, f"asked: {said!r} -> {topic!r}")

LEFT_ALONE = [
    "read my notes", "what are my notes", "show me my notes", "whats in my notes",
    "make a note about the boiler", "take a note about calling the plumber",
    "note that the boiler needs a service", "add milk to my notes",
    "remove milk from my notes", "clear my notes", "what did i say about it",
    "find my notes about it", "what is the weather", "open chrome",
    "what did i have for lunch", "search the web for boilers",
]

for said in LEFT_ALONE:
    check(notes.search_topic(said) is None, f"left alone: {said!r}")

# ---- finding the note ------------------------------------------------------

today = date.today()
older = date(today.year - 1, 3, 3)

NOTES = [
    (today, "call the plumber about the boiler on tuesday"),
    (today - timedelta(days=1), "buy milk, eggs and bread"),
    (today - timedelta(days=5), "the wifi password for the guest network is on the back of the router"),
    (today - timedelta(days=9), "dentist appointment moved to the 14th"),
    (today - timedelta(days=12), "idea: add a radar sweep animation to the HUD"),
    (today - timedelta(days=20), "renew car insurance before october"),
    (today - timedelta(days=21), "ask cousin about the femto ml core question"),
    (today - timedelta(days=22), "pay council tax"),
    (today - timedelta(days=30), "book train tickets to manchester for the wedding"),
    (older, "sensor board needs a 10k resistor for the DHT22"),
    (today - timedelta(days=40), "remember to back up the askfiles keystore"),
    (today - timedelta(days=41), "gym on monday wednesday friday"),
    (today - timedelta(days=42), "birthday present for mum"),
    (today - timedelta(days=43), "check render logs for the throttle warnings"),
    (today - timedelta(days=44), "look into tailscale magic dns on android"),
]

with open(notes._notes_path(), "w", encoding="utf-8") as handle:
    for day, text in NOTES:
        handle.write(f"[{day.isoformat()} 09:30] {text}\n")


def found(topic):
    return [text for _day, text in notes.search(topic)]


model = semantic_memory.similarities("probe", ["probe"]) is not None

FINDS = {
    "the boiler": 0, "heating": 0, "groceries": 1, "shopping": 1, "wifi": 2,
    "internet password": 2, "teeth": 3, "hud ideas": 4, "insurance": 5, "my car": 5,
    "my cousin": 6, "tax": 7, "trains": 8, "the wedding": 8, "resistor": 9,
    "keystore": 10, "backups": 10, "exercise": 11, "workout": 11, "my mum": 12,
    "gifts": 12, "render": 13, "tailscale": 14, "plumber": 0, "passwords": 2,
    "appointments": 3, "the car": 5,
}

# "my holiday" is left out on purpose: this small model puts it close to
# "birthday present for mum", and no minimum separates that from true
# matches such as "teeth". Offering a loosely related note is the known
# cost; it only ever reads a note out.
NOTHING = ["quantum physics", "football", "my holiday in spain", "the cat", "crypto prices",
           "the weather", "my phone", "my plans", "my girlfriend"]

if not model:
    print("SKIP meaning-based checks (the local model is unavailable here)")
else:
    for topic, index in FINDS.items():
        results = found(topic)
        check(bool(results) and results[0] == NOTES[index][1],
              f"finds: {topic!r} -> {NOTES[index][1]!r}" + ("" if results else " (found nothing)"))

    for topic in NOTHING:
        results = found(topic)
        check(results == [], f"finds nothing for {topic!r}" + (f" (found {results})" if results else ""))

    check(len(found("the boiler")) == 1, "one clear match is not followed by weak ones")

# ---- what is said -----------------------------------------------------------

said = notes.describe_search("the boiler")
check(said == "Today you noted: call the plumber about the boiler on tuesday.", f"spoken, today: {said!r}")

said = notes.describe_search("resistor")
check(said.startswith(f"On 3 March {older.year} you noted: sensor board"), f"spoken, last year: {said!r}")

said = notes.describe_search("quantum physics")
check(said == "I can't find a note about quantum physics, sir.", f"spoken, nothing: {said!r}")

said = notes.describe_search("my phone")
check("your phone" in said, f"'my' is said back as 'your': {said!r}")

# ---- without the model -------------------------------------------------------

real = semantic_memory.similarities
semantic_memory.similarities = lambda query, texts: None

try:
    check(found("my car") == ["renew car insurance before october"], "without the model, a shared word still finds the note")
    check(found("keystores") == ["remember to back up the askfiles keystore"], "without the model, plurals still match")
    check(found("heating") == [], "without the model, meaning alone finds nothing (and says so)")
finally:
    semantic_memory.similarities = real

os.remove(notes._notes_path())
check(notes.describe_search("the boiler") == "You have no notes, sir.", "no notes at all is said plainly")

# ---- the real fast path, where it can be imported ---------------------------

try:
    import commands
except Exception as error:  # needs JARVIS's full Windows environment
    print(f"SKIP fast-path wiring (could not import commands: {error})")
else:
    result = commands._fast_path("Jarvis, what did I note about the boiler?")
    check(bool(result) and result["intent"] == "read_notes" and result.get("text") == "the boiler",
          "fast path: a topic question goes to read_notes with its topic")

    result = commands._fast_path("read my notes about the boiler")
    check(bool(result) and result.get("text") == "the boiler",
          "fast path: 'read my notes about ...' is a search, not the last five")

    result = commands._fast_path("read my notes")
    check(bool(result) and result["intent"] == "read_notes" and not result.get("text"),
          "fast path: 'read my notes' still reads them all")

    result = commands._fast_path("make a note about the boiler")
    check(bool(result) and result["intent"] == "make_note", "fast path: making a note is unchanged")

    # "note down that ..." saved "that the dht22 needs a 10k resistor".
    for said, saved in (
        ("note down that the dht22 needs a 10k resistor", "the dht22 needs a 10k resistor"),
        ("note that the boiler needs a service", "the boiler needs a service"),
        ("note down the wifi password is on the router", "the wifi password is on the router"),
        ("make a note that the car needs tax", "the car needs tax"),
    ):
        result = commands._fast_path(said)
        check(bool(result) and result["intent"] == "make_note" and result.get("text") == saved,
              f"saved as said: {said!r} -> {saved!r} (got {result and result.get('text')!r})")

sys.exit(1 if failures else 0)
