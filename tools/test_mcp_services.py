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
  their calls, but not to code tasks or fix passes.

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

leaked = mcp_services.call("mcp_test_environment", {"name": "JARVIS_TEST_SECRET"})
check("should-never-leak" not in leaked, "the server does not see JARVIS's own secrets")

given = mcp_services.call("mcp_test_environment", {"name": "GIVEN"})
check("passed-on-purpose" in given, "variables the entry names reach the server, with ${NAME} filled in")

check(mcp_services.mentioned("what does the test service say") == "test", "a command naming the service is recognised")
check(mcp_services.mentioned("ask the checker about it") == "test", "an alias from mcp.json is recognised")
check(mcp_services.mentioned("what is the latest news") is None, "an ordinary command names no service")
check(mcp_services.mentioned("contest the result") is None, "a service name inside another word does not count")

check(any("connected, " in line for line in mcp_services.status()), "status reports the connected service")

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
