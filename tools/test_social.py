"""Thanks, praise and "you good?" are answered locally, and nothing else is.

The risk runs both ways. Missing a pleasantry costs a model call and gets
"I'm not equipped for that". Wrongly matching a command is worse: "thanks,
now open chrome" answered with "you're welcome" and chrome never opened.
So this checks a spread of real ways of saying each, and a spread of
commands that merely start or end the same way.

Where commands.py can be imported (JARVIS's own PC), the same sentences go
through the real fast path too, so the wiring is checked as well as the
shapes.

    python tools/test_social.py
"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import social  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


GRATITUDE = [
    "thanks", "thank you", "thank you jarvis", "thanks jarvis", "thanks for that",
    "thank you so much", "thanks a lot", "cheers", "cheers mate", "ta",
    "much appreciated", "I appreciate it", "I really appreciate that",
    "excellent, thank you", "great thanks", "perfect, thanks for that jarvis",
    "thanks for the help", "thank you for your help", "okay thanks", "brilliant cheers",
    "good job, thanks", "thanks again", "yeah thanks", "well thank you", "Jarvis, thank you very much", "thanks for the update",
]

PRAISE = [
    "good job", "great job jarvis", "well done", "nice one", "you're the best",
    "you are brilliant", "legend", "what a legend", "well done jarvis", "Well done!", "nicely done", "brilliant work",
]

WELLBEING = [
    "you good", "you good?", "Jarvis, you good?", "are you ok", "are you okay jarvis",
    "you alright", "how are you", "how are you doing", "how are you today",
    "how's it going", "hows it going mate", "what's up", "how are things",
    "you doing okay", "are you okay?", "hey jarvis, how are you", "all good jarvis", "how are you getting on",
]

COMMANDS = [
    "no thanks", "thanks, now open chrome", "thank you, set a reminder for 5 minutes",
    "open chrome", "what's up with my github issues", "how are you getting the data",
    "are you able to open chrome", "you good at maths", "good job opening chrome",
    "whats up john", "how are the markets", "thanks for the reminder set another one",
    "tell me a joke", "are you there", "what time is it", "how is the weather",
    "thank god", "good morning jarvis", "is everything ok with my computer",
    "how are my projects doing", "set a timer for ten minutes, thanks",
]

for said in GRATITUDE:
    check(social.kind(said) == "gratitude", f"thanks: {said!r}")

for said in PRAISE:
    check(social.kind(said) == "praise", f"praise: {said!r}")

for said in WELLBEING:
    check(social.kind(said) == "wellbeing", f"wellbeing: {said!r}")

for said in COMMANDS:
    check(social.kind(said) is None, f"left alone: {said!r}")

check(social.reply("gratitude") and "welcome" in social.reply("gratitude").lower()
      or social.reply("gratitude").endswith("sir."), "thanks gets a reply")

replies = {social.reply("gratitude") for _ in range(12)}
check(len(replies) > 1, "the reply to thanks varies")

# ---- the real fast path, where it can be imported ---------------------------

try:
    import commands
except Exception as error:  # needs JARVIS's full Windows environment
    print(f"SKIP fast-path wiring (could not import commands: {error})")
else:
    for said, intent in (("thank you jarvis", "gratitude"), ("you good?", "wellbeing_check"),
                         ("well done", "praise"), ("are you okay", "wellbeing_check")):
        result = commands._fast_path(said)
        check(bool(result) and result["intent"] == intent, f"fast path: {said!r} -> {intent}")

    result = commands._fast_path("thanks, now open chrome")
    check(not result or result["intent"] not in ("gratitude", "praise", "wellbeing_check"),
          "fast path: a command with thanks in it is not answered as thanks")

sys.exit(1 if failures else 0)
