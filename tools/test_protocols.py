"""Protocols: "initiate startup protocol", "clean slate protocol".

Runs in a sandboxed JARVIS folder with pretend hands (no Chrome, Cursor or
OBS is touched) and a pretend Chrome debugging endpoint. Checked:

- what is said: engage, clear one, clean slate, aliases, "start up" as two
  words; and what is not a protocol command is left alone;
- engaging does each step in order, keeps which tabs it opened, says so,
  and says which steps failed (an optional one silently);
- a clean slate undoes the most recent protocol first, closes only the tabs
  its protocol opened, asks its question last, and does the "yes" step only
  on a yes; clearing one protocol leaves the other engaged;
- what is engaged survives a restart;
- the Chrome tabs are opened with PUT and closed by id, only those;
- through commands: no model call, "Initiating..." then the result, the
  question with its countdown, and a spoken yes closing the project.

    python tools/test_protocols.py
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import chrome_tabs, protocols  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


CONFIG = {
    "protocols": {
        "startup": {
            "aliases": ["morning"],
            "engage": [
                {"open": "https://www.skysports.com"},
                {"open": "https://www.bbc.co.uk/news"},
                {"open": "https://github.com"},
                {"project": "JARVIS"},
            ],
            "say": "Startup protocol engaged, sir.",
            "clear": [
                {"close_tabs": True},
                {"ask": "Shall I close the JARVIS project, sir?", "yes": {"close_project": "JARVIS"}},
            ],
            "cleared": "Clean slate protocol applied, sir.",
        },
        "stream": {
            "engage": [
                {"app": "OBS Studio"},
                {"service": "obs", "wait": 30},
                {"service": "obs", "tool": "obs-set-input-mute", "arguments": {"inputName": "Mic/Aux", "inputMuted": False}},
                {"service": "obs", "tool": "obs-start-replay-buffer", "optional": True},
                {"service": "obs", "tool": "obs-start-record"},
            ],
            "say": "Stream protocol engaged, sir.",
            "clear": [
                {"service": "obs", "tool": "obs-stop-record"},
                {"service": "obs", "tool": "obs-save-replay-buffer", "then": "obs-get-last-replay-buffer-replay", "optional": True},
                {"service": "obs", "tool": "obs-stop-replay-buffer", "optional": True},
                {"close_app": "OBS Studio"},
            ],
            "cleared": "All clear, sir.",
        },
    },
    "clean_slate": {"say": "Clean slate protocol applied, sir. All clear."},
}

with open(os.path.join(folder, protocols.CONFIG_NAME), "w", encoding="utf-8") as handle:
    json.dump(CONFIG, handle)


class Pretend:
    """Hands that remember what they were asked to do."""

    def __init__(self):
        self.done = []
        self.fail = set()
        self.next_tab = 100

    def open_tabs(self, urls):
        ids = []
        for url in urls:
            self.next_tab += 1
            ids.append(f"tab{self.next_tab}")
            self.done.append(("open", url))
        return ids, None

    def close_tabs(self, ids):
        self.done.append(("close_tabs", tuple(ids)))
        return len(ids)

    def open_project(self, name):
        self.done.append(("project", name))
        return "project" not in self.fail

    def close_project(self, name):
        self.done.append(("close_project", name))
        return True

    def launch_app(self, name):
        self.done.append(("app", name))
        return True

    def close_app(self, name):
        self.done.append(("close_app", name))
        return True

    def wait_service(self, name, seconds):
        self.done.append(("wait", name))
        return True

    def act(self, service, tool, arguments, then):
        self.done.append(("act", tool, then))
        if tool in self.fail:
            return False, f"OBS has no input called {arguments.get('inputName', '?')}"
        return True, None


hands = Pretend()
protocols.hands = hands
flashed = []
protocols.set_flash_listener(flashed.append)

# ---- what is said ---------------------------------------------------------------

SAID = {
    "Jarvis, initiate startup protocol": ("engage", "startup"),
    "initiate start up protocol": ("engage", "startup"),
    "engage the stream protocol": ("engage", "stream"),
    "stream protocol": ("engage", "stream"),
    "run the morning protocol": ("engage", "startup"),
    "clean slate protocol": ("clean_slate", None),
    "initiate clean slate protocol": ("clean_slate", None),
    "clean slate": ("clean_slate", None),
    "clear the stream protocol": ("clear", "stream"),
    "end startup protocol": ("clear", "startup"),
    "initiate party protocol": ("unknown", "party"),
}

for said, meant in SAID.items():
    check(protocols.asked(said) == meant, f"{said!r} -> {protocols.asked(said)}")

for said in ("start recording", "what is a protocol", "the startup is slow", "clean the kitchen",
             "tell me about the protocol", "party protocol"):
    check(protocols.asked(said) is None, f"left alone: {said!r}")

# ---- engaging --------------------------------------------------------------------

said = protocols.engage("startup")
check(said == "Startup protocol engaged, sir.", f"engaging says so ({said!r})")
check(hands.done == [("open", "https://www.skysports.com"), ("open", "https://www.bbc.co.uk/news"),
                     ("open", "https://github.com"), ("project", "JARVIS")],
      "each step in order: the three sites, then the project")
check(flashed[-1:] == ["STARTUP PROTOCOL"], f"and the HUD flashes it ({flashed})")
check(protocols.engaged() == ["startup"], "it is engaged")
check(protocols.engage("startup") == "The startup protocol is already engaged, sir.", "and is not engaged twice")

hands.done.clear()
hands.fail = {"obs-set-input-mute", "obs-start-replay-buffer"}
said = protocols.engage("stream")
check([step[0:2] for step in hands.done] == [("app", "OBS Studio"), ("wait", "obs"), ("act", "obs-set-input-mute"),
                                             ("act", "obs-start-replay-buffer"), ("act", "obs-start-record")],
      "stream: OBS started, waited for, mic unmuted, replay buffer and recording started")
check(said == "Stream protocol engaged, sir. One step didn't go through: OBS has no input called Mic/Aux.",
      f"a failed step is said, an optional one is not ({said!r})")
hands.fail = set()

# What is engaged is kept on disk, so a restart still knows.
with open(os.path.join(folder, protocols.STATE_NAME), encoding="utf-8") as handle:
    kept = json.load(handle)["engaged"]
check([item["name"] for item in kept] == ["startup", "stream"] and kept[0]["tabs"] == ["tab101", "tab102", "tab103"],
      "what is engaged, and the tabs it opened, survive a restart")

# ---- clearing --------------------------------------------------------------------

check(protocols.plan("startup").question is not None and protocols.plan("stream").question is None,
      "startup's clean slate has a question; stream's does not")

hands.done.clear()
plan = protocols.plan()
check([record["name"] for record in plan.records] == ["stream", "startup"], "a clean slate clears the latest first")
said = protocols.clear(plan)
check(hands.done == [("act", "obs-stop-record", None),
                     ("act", "obs-save-replay-buffer", "obs-get-last-replay-buffer-replay"),
                     ("act", "obs-stop-replay-buffer", None), ("close_app", "OBS Studio"),
                     ("close_tabs", ("tab101", "tab102", "tab103"))],
      f"recording stopped, replay saved, OBS closed, then only its own tabs ({hands.done})")
check(said == "Shall I close the JARVIS project, sir?", f"and the question comes last ({said!r})")
check(protocols.engaged() == [], "nothing is engaged afterwards")

said = protocols.answer(plan, yes=False)
check(("close_project", "JARVIS") not in hands.done and said == "Clean slate protocol applied, sir. All clear.",
      f"no leaves the project open ({said!r})")

protocols.engage("startup")
hands.done.clear()
plan = protocols.plan()
protocols.clear(plan)
said = protocols.answer(plan, yes=True)
check(("close_project", "JARVIS") in hands.done and said == "Clean slate protocol applied, sir.",
      f"yes closes it, and one protocol says its own words ({said!r})")

protocols.engage("startup")
protocols.engage("stream")
hands.done.clear()
said = protocols.clear(protocols.plan("stream"))
check(said == "All clear, sir." and protocols.engaged() == ["startup"] and ("close_tabs", ("tab107", "tab108", "tab109")) not in hands.done,
      f"clearing one protocol leaves the other engaged, its tabs untouched ({said!r})")
protocols.clear(protocols.plan("startup"))
check(protocols.plan() is None, "with nothing engaged, there is nothing to clear")

# ---- the Chrome tabs: PUT to open, closed by id ---------------------------------------

class FakeChrome(BaseHTTPRequestHandler):
    pages = {"old1": "https://mine.example"}
    counter = [0]

    def log_message(self, *args):
        pass

    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/json/list":
            return self.reply([{"id": key, "url": url, "title": url, "type": "page"} for key, url in self.pages.items()])
        if self.path.startswith("/json/close/"):
            self.pages.pop(self.path.rsplit("/", 1)[1], None)
            return self.reply("Target is closing")
        if self.path.startswith("/json/activate/"):
            return self.reply("Target activated")
        if self.path.startswith("/json/new"):
            self.send_response(405)
            self.end_headers()
            return None
        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        if self.path.startswith("/json/new?"):
            self.counter[0] += 1
            key = f"new{self.counter[0]}"
            self.pages[key] = self.path.split("?", 1)[1]
            return self.reply({"id": key, "url": self.pages[key], "type": "page"})
        self.send_response(404)
        self.end_headers()


server = HTTPServer(("127.0.0.1", 0), FakeChrome)
threading.Thread(target=server.serve_forever, daemon=True).start()
chrome_tabs.DEBUG_HOST = f"127.0.0.1:{server.server_port}"

ids, reason = chrome_tabs.open_tabs(["https://www.skysports.com", "https://www.bbc.co.uk/news"])
check(ids == ["new1", "new2"] and reason is None, f"tabs open in a running JARVIS Chrome, with PUT ({ids})")
check(FakeChrome.pages["new2"] == "https://www.bbc.co.uk/news", "each to its own site")
check(chrome_tabs.close_tabs(ids + ["gone"]) == 2 and set(FakeChrome.pages) == {"old1"},
      "closing closes those tabs only; your own stays, and one already gone is no failure")
server.shutdown()
check(chrome_tabs.close_tabs(["new1"]) == 0, "with Chrome closed, there is nothing to close and nothing fails")

# ---- through commands -------------------------------------------------------------------

try:
    import commands
except Exception as error:   # a machine without JARVIS's full set of packages
    print(f"SKIP commands routing (could not import commands: {error})")
else:
    rings = []
    commands.set_confirmation_listener(lambda seconds, outcome: rings.append((seconds, outcome)))
    real_agent = commands.run_agent
    commands.run_agent = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called"))

    try:
        result = commands.handle_command("Jarvis initiate startup protocol")
        check(result["intent"] == "protocol" and result["kind"] == "action"
              and result["response"] == "Initiating startup protocol, sir.",
              f"commands: said at once ({result.get('response')!r})")
        made = result["action"]()
        check(result["success_response"](made) == "Startup protocol engaged, sir.", "then the result, with no model call")

        result = commands.handle_command("stream protocol")
        result["success_response"](result["action"]())

        hands.done.clear()
        result = commands.handle_command("clean slate protocol")
        check(result["response"] == "Initiating clean slate protocol, sir.", "a clean slate says it is starting")
        question = result["success_response"](result["action"]())
        check(question == "Shall I close the JARVIS project, sir?" and rings[-1:] and rings[-1][0] == protocols.ASK_SECONDS,
              f"then asks, with the countdown ring ({rings[-1:]})")

        answer = commands.handle_command("yes")
        check(answer is not None and answer["kind"] == "action", "a spoken yes answers it")
        said = answer["success_response"](answer["action"]())
        check(("close_project", "JARVIS") in hands.done and said == "Clean slate protocol applied, sir. All clear.",
              f"and closes the project ({said!r})")

        result = commands.handle_command("clean slate")
        check(result["action"]() == "Nothing to clear, sir. No protocol is engaged.", "nothing engaged, nothing to clear")
    finally:
        commands.run_agent = real_agent
        commands.set_confirmation_listener(None)

# ---- the files are JARVIS's own -----------------------------------------------------------

from actions import folder_organizer  # noqa: E402

check(folder_organizer.is_protected("protocols.json") and folder_organizer.is_protected(".jarvis-protocols.json"),
      "the folder guard never moves protocols.json or what is engaged")

sys.exit(1 if failures else 0)
