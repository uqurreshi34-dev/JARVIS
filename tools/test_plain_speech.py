"""A model's formatting is never read aloud or shown on the HUD.

Asked for his largest document, JARVIS said "the largest is **AskFiles
...pdf**" -- the voice reading the asterisks the model wrote for a screen.
phrases.plain() takes them out. Checked:

- **bold**, *emphasis*, _emphasis_, `code`, headings, quotes, bullets and
  [links](...) lose their marks and keep their words;
- what is not formatting is left alone: "5 * 3", my_notes_file.txt,
  __init__.py, a lone asterisk or underscore, numbered steps;
- the voice (speak, and the phone's audio), the HUD and the phone's reply
  all pass through it.

    python tools/test_plain_speech.py
"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import phrases  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


CLEANED = {
    "Your largest document is **AskFiles_1781024633344.pdf** at 4.5 MB, sir.":
        "Your largest document is AskFiles_1781024633344.pdf at 4.5 MB, sir.",
    "It is *really* big.": "It is really big.",
    "It is _really_ big.": "It is really big.",
    "Run `python main.py` first.": "Run python main.py first.",
    "See [the docs](https://example.com/docs) for more.": "See the docs for more.",
    "## Summary\n- one\n* two\n+ three": "Summary\none\ntwo\nthree",
    "> quoted words": "quoted words",
    "**Spoken summary:** all clear": "Spoken summary: all clear",
}

for written, said in CLEANED.items():
    got = phrases.plain(written)
    check(got == said, f"{written[:40]!r} -> {got!r}")

LEFT = [
    "5 * 3 is 15",
    "my_notes_file.txt",
    "__init__.py and __main__",
    "a lone * and a lone _",
    "1. first\n2. second",
    "x ** y",
    "",
]

for text in LEFT:
    check(phrases.plain(text) == text, f"left alone: {text!r}")

check(phrases.plain(None) is None, "nothing stays nothing")

# Every way a reply reaches you passes through it.
speech_source = (ROOT / "speech.py").read_text(encoding="utf-8")
main_source = (ROOT / "main.py").read_text(encoding="utf-8")

speak_body = speech_source[speech_source.index("    def speak(self, text):"):]
speak_body = speak_body[:speak_body.index("\n    def ", 10)]
audio_body = speech_source[speech_source.index("    def audio_bytes(self, text):"):]
audio_body = audio_body[:audio_body.index("\n    def ", 10)]
reply_body = main_source[main_source.index("    def _reply(self, text):"):]
reply_body = reply_body[:reply_body.index("\n    def ", 10)]

check("_plain(text)" in speak_body, "the voice speaks plain words")
check("_plain(text)" in audio_body, "and so does the phone's audio")
check("phrases.plain(text)" in reply_body, "the HUD shows plain words")
check("spoken = phrases.plain(spoken)" in main_source, "and so does the phone's reply")

sys.exit(1 if failures else 0)
