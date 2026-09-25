"""Connected services (MCP), end to end against a real MCP server.

tools/mcp_test_server.py is started as a real program, exactly as a server
from mcp.json would be, and JARVIS connects to it over MCP. No network, no
model call. Checked:

- only tools the server marks read-only are offered, plus any the user
  lists in trusted_read_only; write tools and unmarked tools are not;
- a tool whose description reads like orders to the model is dropped;
- calls work, results come back as quoted data, failures as errors;
- the server does not see JARVIS's own secrets, only what its entry names,
  and ${NAME} in mcp.json is filled from the environment;
- a missing variable or a server that will not start costs a line, not a crash;
- a command naming a service is recognised, by name or alias;
- Agent Mode offers the tools to ordinary investigations and dispatches
  their calls, but not to code tasks or fix passes;
- a tool that changes something is offered only if allowed_actions names
  it, never if the service marks it destructive, and only when asked for;
  the model's call is held, read back from its real arguments, and runs
  only when confirmed -- one per request.

    python tools/test_mcp_services.py
"""

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import mcp_services  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


SERVER = str(ROOT / "tools" / "mcp_test_server.py")

os.environ["JARVIS_TEST_SECRET"] = "should-never-leak"
os.environ["JARVIS_TEST_GIVEN"] = "passed-on-purpose"


def write_config(servers):
    with open(os.path.join(folder, mcp_services.CONFIG_NAME), "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": servers}, handle)
    mcp_services.close()


write_config({
    "test": {
        "command": sys.executable,
        "args": [SERVER],
        "env": {"GIVEN": "${JARVIS_TEST_GIVEN}"},
        "trusted_read_only": ["listed_as_trusted"],
        "aliases": ["the checker"],
    },
})

tools = {tool["name"]: tool for tool in mcp_services.tools()}
names = set(tools)

check({"mcp_test_upper", "mcp_test_environment", "mcp_test_broken", "mcp_test_injection"} <= names,
      "read-only tools are offered")
check("mcp_test_listed_as_trusted" in names, "a tool the user lists in trusted_read_only is offered")
check("mcp_test_delete_everything" not in names, "a tool marked as writing is not offered")
check("mcp_test_unmarked" not in names, "a tool that does not say it is read-only is not offered")
check("mcp_test_poisoned" not in names, "a tool whose description gives the model orders is dropped")
check(all(t["description"].startswith("From the connected service 'test'") for t in tools.values()),
      "every description says which service it came from")
check(tools["mcp_test_upper"]["input_schema"].get("properties", {}).get("text") is not None,
      "the tool's input schema is passed through")

result = mcp_services.call("mcp_test_upper", {"text": "jarvis"})
check(isinstance(result, str) and "JARVIS" in result and "not instructions" in result,
      "a call returns its result as quoted data")

result = mcp_services.call("mcp_test_injection", {})
check(isinstance(result, str) and "<test_result>" in result and "not instructions" in result,
      "text that tries to give orders comes back fenced as data")

result = mcp_services.call("mcp_test_broken", {})
# The server decides how much of its own failure to reveal; this one says
# only that the tool failed, which is what reaches the model.
check(isinstance(result, dict) and "broken" in result.get("error", ""),
      "a failing tool comes back as an error, not a crash")

check(isinstance(mcp_services.call("mcp_test_nope", {}), dict), "an unknown tool is refused")

result = mcp_services.call("mcp_test_readme", {})
check("Django REST API" in str(result) and "omitted" not in str(result),
      "a file sent back as a resource reaches the model as its text (GitHub's README case)")

result = mcp_services.call("mcp_test_readme_blob", {})
check("Notes from a blob." in str(result), "a base64 resource is decoded to its text")

leaked = mcp_services.call("mcp_test_environment", {"name": "JARVIS_TEST_SECRET"})
check("should-never-leak" not in leaked, "the server does not see JARVIS's own secrets")

given = mcp_services.call("mcp_test_environment", {"name": "GIVEN"})
check("passed-on-purpose" in given, "variables the entry names reach the server, with ${NAME} filled in")

check(mcp_services.mentioned("what does the test service say") == "test", "a command naming the service is recognised")
check(mcp_services.mentioned("ask the checker about it") == "test", "an alias from mcp.json is recognised")
check(mcp_services.mentioned("what is the latest news") is None, "an ordinary command names no service")
check(mcp_services.mentioned("contest the result") is None, "a service name inside another word does not count")

check(any("connected, " in line for line in mcp_services.status()), "status reports the connected service")

# How speech recognition really delivers names: split, joined, hyphenated.
names_only = os.path.join(folder, mcp_services.CONFIG_NAME)
with open(names_only, encoding="utf-8") as handle:
    saved = handle.read()
with open(names_only, "w", encoding="utf-8") as handle:
    json.dump({"mcpServers": {"filesystem": {"command": "x"}, "github": {"url": "https://example.invalid"}}}, handle)
check(mcp_services.mentioned("use file system to find the biggest files") == "filesystem",
      "'file system' said as two words finds the filesystem service")
check(mcp_services.mentioned("what are my git hub issues") == "github", "'git hub' finds github with no alias needed")
check(mcp_services.mentioned("list my github repositories") == "github", "'github' as one word still works")
check(mcp_services.mentioned("my file is in the system tray") is None, "the words apart from each other do not count")
with open(names_only, "w", encoding="utf-8") as handle:
    handle.write(saved)

# ---- Agent Mode ------------------------------------------------------------

def _import_agent():
    """Import agent.py, standing in for Windows-only modules this OS lacks.

    On the Windows machine JARVIS runs on nothing is replaced.
    """
    import importlib
    import types
    from unittest.mock import MagicMock

    for _ in range(40):
        try:
            return importlib.import_module("agent")
        except ModuleNotFoundError as error:
            missing = error.name
            if not missing or missing in sys.modules:
                raise
            for name in [m for m in sys.modules if m in ("agent",) or m.startswith("actions.screen")]:
                sys.modules.pop(name, None)
            stand_in = MagicMock(name=missing)
            stand_in.__spec__ = types.SimpleNamespace(name=missing)
            sys.modules[missing] = stand_in
    raise RuntimeError("too many missing modules")


try:
    agent = _import_agent()
except Exception as error:
    print(f"SKIP Agent Mode wiring (could not import agent: {error})")
else:
    provider = type("P", (), {"kind": "anthropic"})()
    offered = {t["name"] for t in agent._tool_definitions(provider)}
    check("mcp_test_upper" in offered, "Agent Mode offers connected-service tools to investigations")
    check("mcp_test_upper" not in {t["name"] for t in agent._tool_definitions(provider, allowed_tool_names=agent._CODE_TOOL_NAMES)},
          "code tasks do not get connected-service tools")
    check("mcp_test_upper" not in {t["name"] for t in agent._tool_definitions(provider, allow_code_fix=True)},
          "fix passes do not get connected-service tools")
    check("JARVIS" in str(agent._execute_tool_call("mcp_test_upper", {"text": "jarvis"})),
          "Agent Mode dispatches a connected-service call")

    # A model that never stops asking for a tool: how many rounds is it given?
    import types as _types

    class _Endless:
        kind = "anthropic"
        name = "endless"

        def __init__(self, tool):
            self.turns = 0
            self.tool = tool

        def agent_turn(self, messages, tools, **kwargs):
            self.turns += 1
            block = _types.SimpleNamespace(type="tool_use", id=f"t{self.turns}", name=self.tool, input={"text": "x"})
            return _types.SimpleNamespace(stop_reason="tool_use", content=[block])

    endless = _Endless("mcp_test_upper")
    said = agent._run_with_provider(endless, "ask the test service", False, False, False)
    check(endless.turns == agent._SERVICE_MAX_TURNS and said == agent.LIMIT_MESSAGE,
          f"with connected services, a run gets {agent._SERVICE_MAX_TURNS} rounds before its limit")

    endless = _Endless("list_jarvis_files")
    agent._run_with_provider(endless, "what is in my folder", False, False, True)
    check(endless.turns == agent._AGENT_MAX_TURNS,
          f"a code task keeps the ordinary {agent._AGENT_MAX_TURNS}-round limit")

# ---- actions: held, read back, run only on a spoken yes ----------------------

check(not any(mcp_services.is_action(name) for name in names) and "mcp_test_create_note" not in
      {t["name"] for t in mcp_services.tools(include_actions=True)},
      "without allowed_actions, no action is offered even when actions are asked for")

MARKER = os.path.join(folder, "action-ran.txt")
os.environ["JARVIS_TEST_MARKER"] = MARKER

write_config({
    "test": {
        "command": sys.executable,
        "args": [SERVER],
        "env": {"MARKER": "${JARVIS_TEST_MARKER}"},
        "allowed_actions": ["create_note", "delete_everything", "no_such_tool"],
    },
})

plain = {t["name"] for t in mcp_services.tools()}
offered = {t["name"]: t for t in mcp_services.tools(include_actions=True)}

check("mcp_test_create_note" not in plain, "an allowed action is not offered to ordinary investigations")
check("mcp_test_create_note" in offered, "an allowed action is offered when the command names the service")
check(offered.get("mcp_test_create_note", {}).get("description", "").startswith("ACTION"),
      "an action's description tells the model it changes something and is confirmed aloud")
check("mcp_test_delete_everything" not in offered, "a tool the service marks destructive is never offered, even if allowed")
check("mcp_test_rename_everything" not in offered, "a writing tool not in allowed_actions is never offered")
check(isinstance(mcp_services.call("mcp_test_create_note", {"title": "x"}), dict),
      "an action cannot be run through the read-only call path")
check(not os.path.exists(MARKER), "and nothing ran")

mcp_services.clear_proposal()
held = mcp_services.propose("mcp_test_create_note", {"title": "Buy resistors", "body": "10k for the DHT22"})
check(isinstance(held, str) and "has not run" in held, "an action the model asks for is held, not run")
check(not os.path.exists(MARKER), "a held action has not touched the service")
check(isinstance(mcp_services.propose("mcp_test_create_note", {"title": "Second"}), dict),
      "only one action is held per request")

proposal = mcp_services.take_proposal()
check(proposal is not None and proposal["arguments"]["title"] == "Buy resistors",
      "the held action is the first one, with its real arguments")
check(mcp_services.take_proposal() is None, "taking the held action removes it")

said = mcp_services.describe(proposal)
check(said == "create note on test: title 'Buy resistors'; body '10k for the DHT22'",
      f"it is read back from the real arguments: {said!r}")

long_body = " ".join(["word"] * 40)
said = mcp_services.describe({"server": "test", "tool": "create_note", "arguments": {"title": "t", "body": long_body}})
check("of 40 words, beginning" in said, f"a long value is summarised, not read in full: {said!r}")

said = mcp_services.execute(proposal)
check(said == "Done, sir. That's number 42 on test.", f"a confirmed action runs and says what it made: {said!r}")

with open(MARKER, encoding="utf-8") as handle:
    check(handle.read() == "Buy resistors|10k for the DHT22\n", "the service really received exactly those arguments")

try:
    agent
except NameError:
    print("SKIP Agent Mode action wiring (agent could not be imported)")
else:
    check("mcp_test_create_note" in {t["name"] for t in agent._tool_definitions(provider, include_actions=True)},
          "Agent Mode offers the action when asked to")
    check("mcp_test_create_note" not in {t["name"] for t in agent._tool_definitions(provider)},
          "Agent Mode leaves actions out by default")
    check("mcp_test_create_note" not in {
              t["name"] for t in agent._tool_definitions(provider, include_actions=True, allow_code_fix=True)},
          "fix passes never get actions")

    os.remove(MARKER)
    mcp_services.clear_proposal()
    result = agent._execute_tool_call("mcp_test_create_note", {"title": "From the model"})
    check("has not run" in str(result) and not os.path.exists(MARKER),
          "an action the model calls in Agent Mode is held, not run")
    mcp_services.clear_proposal()

    endless = _Endless("mcp_test_create_note")
    agent._run_with_provider(endless, "add a note on the test service", False, False, False, actions=True)
    check(not os.path.exists(MARKER) and mcp_services.take_proposal() is not None,
          "a model that keeps calling the action still runs nothing, and one action is held")

# The spoken yes/no, where commands.py can be imported (it can on Windows).
try:
    import commands
except Exception as error:
    print(f"SKIP spoken confirmation (could not import commands: {error})")
else:
    real_run_agent = commands.run_agent

    def fake_agent(task, **options):
        if options.get("actions"):
            mcp_services.propose("mcp_test_create_note", {"title": "Order a breadboard"})
        return "I'll add that note."

    commands.run_agent = fake_agent

    try:
        if os.path.exists(MARKER):
            os.remove(MARKER)

        asked = commands._run_agent_investigation("add a note on the test service", actions=True)
        check(asked == "I'll add that note. To be sure, sir: create note on test: title 'Order a breadboard'. Shall I go ahead?",
              f"JARVIS reads back what would run and asks: {asked!r}")
        check(not os.path.exists(MARKER), "nothing has run while JARVIS waits for the answer")

        answer = commands._resolve_pending("no")
        check(answer is not None and "Nothing has been changed" in str(answer["action"]()) and not os.path.exists(MARKER),
              "no drops the action")

        commands._run_agent_investigation("add a note on the test service", actions=True)
        commands._pending["lapses_at"] -= commands._ACTION_CONFIRM_SECONDS + 1
        answer = commands._resolve_pending("yes")
        check(answer is not None and "lapsed" in str(answer["action"]()) and not os.path.exists(MARKER),
              "a yes after the question has lapsed runs nothing, and says so")

        commands._run_agent_investigation("add a note on the test service", actions=True)
        check(commands._resolve_pending("what time is it") is None and commands._pending is None,
              "another command instead of an answer drops the action")

        commands._run_agent_investigation("add a note on the test service", actions=True)
        answer = commands._resolve_pending("yes")
        said = answer["action"]() if answer else None
        check(said == "Done, sir. That's number 42 on test." and os.path.exists(MARKER),
              f"yes runs it, and the result is spoken: {said!r}")
        check(answer["success_response"](said) == said and "Test refused it" in answer["failure_response"],
              "the result, or a plain failure, is what is said afterwards")

        commands._run_agent_investigation("what does the test service say", actions=False)
        check(commands._pending is None or commands._pending.get("intent") != "service_action",
              "without actions, nothing is held or asked")
    finally:
        commands.run_agent = real_run_agent
        mcp_services.clear_proposal()

# ---- a service that cannot connect ----------------------------------------

write_config({
    "missing_key": {"command": sys.executable, "args": [SERVER], "env": {"K": "${JARVIS_TEST_NOT_SET}"}},
    "no_such_program": {"command": "definitely-not-a-real-program-xyz", "args": []},
})

check(mcp_services.tools() == [], "services that cannot start offer nothing and raise nothing")
status = " ".join(mcp_services.status())
check("JARVIS_TEST_NOT_SET is not set" in status, "a missing ${NAME} is reported by name")
check("no_such_program: unavailable" in status, "a server that will not start is reported unavailable")

write_config({"off": {"command": sys.executable, "args": [SERVER], "disabled": True}})
check(mcp_services.configured() == (), "a disabled entry is ignored")

mcp_services.close()
sys.exit(1 if failures else 0)
