"""Protocols: one sentence, a run of things JARVIS can already do.

"Jarvis, initiate startup protocol" opens the morning's sites and your
project; "stream protocol" starts OBS, unmutes the mic and starts recording;
"clean slate protocol" undoes whichever of them are engaged, and only what
they did -- the tabs JARVIS opened, never the ones you had.

Nothing is written into the code. Protocols are yours, in protocols.json in
the JARVIS folder:

    {
      "protocols": {
        "startup": {
          "engage": [{"open": "https://www.skysports.com"}, {"project": "JARVIS"}],
          "say": "Startup protocol engaged, sir.",
          "clear": [{"close_tabs": true},
                    {"ask": "Shall I close the JARVIS project, sir?",
                     "yes": {"close_project": "JARVIS"}}],
          "cleared": "Clean slate protocol applied, sir."
        }
      },
      "clean_slate": {"say": "Clean slate protocol applied, sir. All clear."}
    }

A step is one of a few things JARVIS already does, so a protocol can never
do more than asking for each step in turn could:

    {"open": url}                      a tab in the JARVIS Chrome
    {"project": name}                  a Cursor project
    {"app": name}                      a program, by its Start-menu name
    {"service": name, "wait": secs}    wait for a connected service to connect
    {"service": s, "tool": t, "arguments": {...}, "then": t2}
                                       one of that service's allowed actions
                                       that runs without asking (mcp.json)
    {"close_tabs": true}               the tabs this protocol opened
    {"close_project": name}            that project's Cursor window
    {"close_app": name}                that program
    {"ask": question, "yes": step, "no": step}
                                       a yes or no, with the countdown ring;
                                       an unanswered question does neither.
                                       Word it so yes does the thing ("Shall
                                       I close ...?"), as every other
                                       question JARVIS asks does.

Any step may carry "optional": true, so its failing goes unmentioned. What
is engaged, and which tabs each opened, is kept in .jarvis-protocols.json
so a clean slate still knows after JARVIS restarts.
"""

import json
import os
import re
import threading
import time

from actions import files, journal


CONFIG_NAME = "protocols.json"
STATE_NAME = ".jarvis-protocols.json"

# How long the clean slate's question stands, with the countdown ring.
ASK_SECONDS = 20

_lock = threading.Lock()
_flash_listener = None


def set_flash_listener(listener):
    """Register a callable that flashes a word on the HUD ("STARTUP PROTOCOL")."""
    global _flash_listener
    _flash_listener = listener


def _flash(word):
    if _flash_listener:
        try:
            _flash_listener(str(word).upper()[:24])
        except Exception as error:
            print(f"[JARVIS] could not flash on the HUD: {error}")


# ---- configuration and state --------------------------------------------------

def _path(name):
    root = files.root()
    return os.path.join(root, name) if root else None


def _load_json(name, default):
    path = _path(name)

    if not path or not os.path.exists(path):
        return default

    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as error:
        print(f"[JARVIS] could not read {name}: {error}")
        return default


def config():
    data = _load_json(CONFIG_NAME, {})
    return data if isinstance(data, dict) else {}


def _protocols():
    listed = config().get("protocols")
    return {str(key): value for key, value in listed.items() if isinstance(value, dict)} if isinstance(listed, dict) else {}


def _state():
    data = _load_json(STATE_NAME, {})
    engaged = data.get("engaged") if isinstance(data, dict) else None
    return [item for item in engaged if isinstance(item, dict) and item.get("name")] if isinstance(engaged, list) else []


def _save_state(engaged):
    path = _path(STATE_NAME)

    if not path:
        return

    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"engaged": engaged}, handle, indent=2)
    except OSError as error:
        print(f"[JARVIS] could not save {STATE_NAME}: {error}")


def engaged():
    """The names of the protocols engaged now, in the order they were."""
    with _lock:
        return [item["name"] for item in _state()]


# ---- what was said ---------------------------------------------------------------

_SQUASH = re.compile(r"[^a-z0-9]+")


def _squash(text):
    return _SQUASH.sub("", str(text or "").casefold())


def _words(text):
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", str(text or "").casefold().replace("'", "")).split())


_ENGAGE = re.compile(
    r"^(?:(?:jarvis|please|ok|okay)\s+)*(?:(?:initiate|engage|activate|start|begin|run|launch|execute|commence)\s+)?"
    r"(?:the\s+|my\s+)?(?P<name>.+?)\s+protocols?(?:\s+(?:please|now|jarvis))*$"
)
_CLEAR = re.compile(
    r"^(?:(?:jarvis|please|ok|okay)\s+)*(?:clear|end|stop|disengage|cancel|close|finish|undo)\s+"
    r"(?:the\s+|my\s+)?(?P<name>.+?)\s+protocols?(?:\s+(?:please|now|jarvis))*$"
)
_CLEAN_SLATE = re.compile(
    r"^(?:(?:jarvis|please|ok|okay)\s+)*(?:(?:initiate|engage|activate|run|apply|execute|start)\s+)?(?:the\s+|a\s+)?"
    r"clean\s+(?:slate|state|slates)(?:\s+protocols?)?(?:\s+(?:please|now|jarvis))*$"
)


def _named(spoken):
    """The protocol a spoken name means, by its name or one of its aliases."""
    wanted = _squash(spoken)

    for key, protocol in _protocols().items():
        aliases = protocol.get("aliases") if isinstance(protocol.get("aliases"), list) else []

        if wanted in {_squash(key)} | {_squash(alias) for alias in aliases}:
            return key

    return None


def asked(text):
    """What a command asks of the protocols, or None if it is not about them.

    ("engage", name), ("clear", name), ("clean_slate", None), or
    ("unknown", what was said) for a protocol that is not in protocols.json.
    """
    said = _words(text)

    if not said:
        return None

    if _CLEAN_SLATE.match(said):
        return "clean_slate", None

    cleared = _CLEAR.match(said)

    if cleared:
        name = _named(cleared.group("name"))
        return ("clear", name) if name else ("unknown", cleared.group("name"))

    engage = _ENGAGE.match(said)

    if engage:
        name = _named(engage.group("name"))

        if name:
            return "engage", name

        # "protocol" said with a verb in front is a protocol command, even for
        # one that does not exist; bare "<something> protocol" may not be.
        if said.split()[0] in ("initiate", "engage", "activate", "commence", "execute"):
            return "unknown", engage.group("name")

    return None


# ---- the hands: what each step does ---------------------------------------------

class Hands:
    """What steps are done with: the parts of JARVIS that already do each thing.

    Kept apart so the tests can watch what a protocol does without a
    Chrome, a Cursor or an OBS; imported late, since they need Windows.
    The program and project lists are read once and kept: reading the
    Start menu runs PowerShell, which is slow, and each new read flashed a
    console window from the .exe.
    """

    _apps = None
    _projects = None

    def _applications(self):
        if Hands._apps is None:
            from actions.applications import ApplicationManager
            Hands._apps = ApplicationManager()
        return Hands._apps

    def _project_manager(self):
        if Hands._projects is None:
            from actions.projects import ProjectManager
            Hands._projects = ProjectManager()
        return Hands._projects

    def open_tabs(self, urls):
        from actions import chrome_tabs
        return chrome_tabs.open_tabs(urls)

    def close_tabs(self, ids):
        from actions import chrome_tabs
        return chrome_tabs.close_tabs(ids)

    def open_project(self, name):
        return self._project_manager().open(name) is not None

    def close_project(self, name):
        return self._project_manager().close(name) is not None

    def launch_app(self, name):
        return bool(self._applications().launch(name))

    def close_app(self, name):
        return bool(self._applications().close(name))

    def still_open(self, record):
        """Whether anything a protocol opened is still there.

        Closing the tabs and the project by hand, instead of saying "clean
        slate", leaves the protocol recorded as engaged; that record alone
        is not believed. Anything unknowable counts as still there.
        """
        from actions import chrome_tabs

        if record.get("tabs") and chrome_tabs.running():
            alive = {tab["id"] for tab in chrome_tabs.tabs()}

            if alive & set(record["tabs"]):
                return True

        for name in record.get("projects") or ():
            if self._project_manager().is_open(name):
                return True

        for name in record.get("apps") or ():
            if self._applications().is_running(name):
                return True

        return False

    def wait_service(self, name, seconds):
        from actions import mcp_services
        return mcp_services.connect_now(name, wait_seconds=seconds)

    def act(self, service, tool, arguments, then):
        from actions import mcp_services
        return mcp_services.act(service, tool, arguments, then=then)


hands = Hands()


def _do(step, record):
    """Do one step. Returns None when it went through, or why it did not."""
    if "open" in step:
        urls = step["open"] if isinstance(step["open"], list) else [step["open"]]
        ids, reason = hands.open_tabs([str(url) for url in urls])
        record.setdefault("tabs", []).extend(ids)
        return reason

    if "project" in step:
        record.setdefault("projects", []).append(str(step["project"]))
        return None if hands.open_project(str(step["project"])) else f"I couldn't open the {step['project']} project"

    if "app" in step:
        record.setdefault("apps", []).append(str(step["app"]))
        return None if hands.launch_app(str(step["app"])) else f"I couldn't start {step['app']}"

    if "close_tabs" in step:
        hands.close_tabs(record.get("tabs") or [])
        return None

    if "close_project" in step:
        return None if hands.close_project(str(step["close_project"])) else f"the {step['close_project']} project didn't close"

    if "close_app" in step:
        return None if hands.close_app(str(step["close_app"])) else f"{step['close_app']} didn't close"

    if "service" in step and "tool" not in step:
        seconds = float(step.get("wait") or 30)
        return None if hands.wait_service(str(step["service"]), seconds) else f"{step['service']} never connected"

    if "service" in step and "tool" in step:
        arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        then = step.get("then") if isinstance(step.get("then"), str) else None
        done, detail = hands.act(str(step["service"]), str(step["tool"]), arguments, then)
        return None if done else detail

    return f"a step I don't recognise ({', '.join(sorted(step))})"


def _run(steps, record):
    """Do each step in turn. Returns the reasons the ones that mattered failed."""
    problems = []

    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict) or "ask" in step:
            continue

        try:
            problem = _do(step, record)
        except Exception as error:
            problem = f"something went wrong ({error})"

        if problem and not step.get("optional"):
            problems.append(problem)

    return problems


def _with_problems(sentence, problems):
    if not problems:
        return sentence

    if len(problems) == 1:
        return f"{sentence} One step didn't go through: {problems[0]}."

    return f"{sentence} {len(problems)} steps didn't go through: {'; '.join(problems)}."


def _spoken(name):
    return name.replace("_", " ")


# ---- engaging ------------------------------------------------------------------------

def engage(name):
    """Engage a protocol. Returns what to say."""
    protocol = _protocols().get(name)

    if protocol is None:
        return f"I don't have a {_spoken(name)} protocol, sir."

    with _lock:
        earlier = next((item for item in _state() if item["name"] == name), None)

    # Engaged only if something it opened is still open: everything closed by
    # hand is a protocol that has ended, and it simply engages again.
    try:
        still_there = earlier is not None and hands.still_open(earlier)
    except Exception as error:
        print(f"[JARVIS] could not check the {name} protocol: {error}")
        still_there = earlier is not None

    if still_there:
        return f"The {_spoken(name)} protocol is already engaged, sir. Say clean slate to clear it."

    _flash(f"{_spoken(name)} protocol")

    record = {"name": name, "at": time.time()}
    problems = _run(protocol.get("engage"), record)

    with _lock:
        state = [item for item in _state() if item["name"] != name]
        state.append(record)
        _save_state(state)

    journal.action(f"protocol_{name}", "engaged", not problems,
                   spoken=f"the {_spoken(name)} protocol")

    return _with_problems(protocol.get("say") or f"{_spoken(name).capitalize()} protocol engaged, sir.", problems)


# ---- clearing ------------------------------------------------------------------------

class Plan:
    """A clean slate, worked out before anything is done."""

    def __init__(self, records, question, message):
        self.records = records          # engaged protocols to clear, last engaged first
        self.question = question        # (protocol name, the ask step) or None
        self.message = message          # what to say when it is done
        self.problems = []


def plan(name=None):
    """What clearing [name] -- or, with none, every engaged protocol -- involves.

    Returns a Plan, or None when there is nothing to clear.
    """
    protocols = _protocols()

    with _lock:
        state = _state()

    records = [item for item in reversed(state) if name is None or item["name"] == name]

    if not records:
        return None

    question = None

    for record in records:
        for step in protocols.get(record["name"], {}).get("clear") or []:
            if isinstance(step, dict) and step.get("ask") and question is None:
                question = (record["name"], step)

    if len(records) == 1:
        only = protocols.get(records[0]["name"], {})
        message = only.get("cleared") or f"{_spoken(records[0]['name']).capitalize()} protocol cleared, sir."
    else:
        message = (config().get("clean_slate") or {}).get("say") or "Clean slate protocol applied, sir. All clear."

    return Plan(records, question, message)


def clear(the_plan):
    """Do every clear step but the question. Returns what to say, or the question."""
    _flash("clean slate" if len(the_plan.records) > 1 or the_plan.question else f"{_spoken(the_plan.records[0]['name'])} cleared")

    protocols = _protocols()

    for record in the_plan.records:
        the_plan.problems += _run(protocols.get(record["name"], {}).get("clear"), record)

    with _lock:
        cleared = {record["name"] for record in the_plan.records}
        _save_state([item for item in _state() if item["name"] not in cleared])

    for record in the_plan.records:
        journal.action(f"protocol_{record['name']}", "cleared", not the_plan.problems,
                       spoken=f"the {_spoken(record['name'])} protocol cleared")

    if the_plan.question:
        return the_plan.question[1]["ask"]

    return _with_problems(the_plan.message, the_plan.problems)


def answer(the_plan, yes):
    """Finish a clean slate once its question is answered. Returns what to say."""
    name, step = the_plan.question
    chosen = step.get("yes" if yes else "no")
    record = next((item for item in the_plan.records if item["name"] == name), {})

    if isinstance(chosen, dict):
        the_plan.problems += _run([chosen], record)

    return _with_problems(the_plan.message, the_plan.problems)
