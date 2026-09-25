"""Connected services: tools from MCP servers, offered to Agent Mode.

MCP (Model Context Protocol) is a common plug for AI tools. A service that
speaks it -- GitHub, Notion, a database, Home Assistant, a folder on disk --
describes its own tools, and any MCP client can use them without code
written for that service. This makes JARVIS one of those clients: list a
server in the JARVIS folder's mcp.json and its tools join Agent Mode, the
same loop that already reads folders and code.

    {
      "mcpServers": {
        "time":   {"command": "uvx", "args": ["mcp-server-time"]},
        "github": {"url": "https://api.githubcopilot.com/mcp/",
                   "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
                   "aliases": ["git hub"]}
      }
    }

The file uses the same "mcpServers" shape as Claude Desktop and Cursor, so
a server's published setup can be pasted in. ${NAME} is filled from the
environment or JARVIS's .env, so keys never have to be written into it.

What a server may do is decided here, not by the server:

- Only tools the server marks read-only are offered freely. Anything else
  -- including a tool that says nothing either way -- is left out, because
  a service's word that a tool is harmless is the most that can be
  checked. A server entry can name tools in "trusted_read_only" when its
  author forgot the mark and the user has checked them.
- A tool that changes something is offered only when its entry names it
  in "allowed_actions", only for a command that names the service, never
  when the server marks it destructive, and never run by the model: the
  call is held, JARVIS reads back exactly what would be done, and only a
  spoken yes runs it. One action per request, and each one is journalled.
- An entry with "confirm_actions": false lets its allowed actions run as
  soon as the model calls them, with no question: for a service whose
  actions are routine, such as starting a recording. Only what
  allowed_actions names, and never what the server marks destructive.
- A service that could not be reached is tried again on a later request,
  after a minute at first and up to ten, so one opened after JARVIS
  starts -- OBS, say -- is picked up without a restart.
- A tool's name and description are the server's words, and they reach
  the model, so they are treated like any outside text: cleaned, and a
  tool whose description reads like an instruction to the model is
  dropped rather than offered.
- What a tool returns is handed to the model as quoted data, never as
  instructions, and every call is written to the journal.
- A server started as a program sees only the basics it needs to run and
  the variables its entry names, never the API keys JARVIS itself holds.

Nothing connects until Agent Mode first needs a tool, and a server that
fails to start costs a printed line, not a broken JARVIS.
"""

import asyncio
import hashlib
import json
import os
import re
import threading
import time

from actions import files, journal, safety


CONFIG_NAME = "mcp.json"

# A server that has not answered by now is treated as unavailable.
CONNECT_SECONDS = 20
CALL_SECONDS = 45

# Longest tool description kept, and longest result handed to the model.
DESCRIPTION_CHARS = 600
RESULT_CHARS = 12_000

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

_lock = threading.Lock()
_loop = None
_servers = {}     # server name -> _Server
_tools = {}       # agent tool name -> (server name, tool name)
_actions = {}     # agent tool name -> (server name, tool name), for actions
_proposal = None  # the one action held for a spoken yes, or None
_immediate = set()  # agent names of actions that run without confirmation

# A service that failed is retried after this, doubling to the ceiling.
RETRY_SECONDS = 60.0
MAX_RETRY_SECONDS = 600.0
_loaded = False


class _Server:
    def __init__(self, name, entry):
        self.name = name
        self.entry = entry
        self.client = None
        self.tools = []       # agent tool dicts, read-only
        self.actions = []     # agent tool dicts that change something
        self.error = None
        self._stop = None
        self._ready = None
        self.retry_at = 0.0   # monotonic time a failed service may be tried again
        self.backoff = 0.0


# ---- configuration --------------------------------------------------------

def config_path():
    return os.path.join(files.root(), CONFIG_NAME)


def _expand(value):
    """${NAME} replaced from the environment. A missing name is an error."""
    if isinstance(value, str):
        def replace(match):
            found = os.getenv(match.group(1))
            if found is None:
                raise KeyError(match.group(1))
            return found
        return _ENV_REFERENCE.sub(replace, value)

    if isinstance(value, list):
        return [_expand(item) for item in value]

    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}

    return value


def _read_config():
    path = config_path()

    if not os.path.exists(path):
        return {}

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        print(f"[JARVIS] could not read {CONFIG_NAME}: {error}")
        return {}

    servers = data.get("mcpServers") if isinstance(data, dict) else None

    if not isinstance(servers, dict):
        return {}

    return {
        str(name): entry
        for name, entry in servers.items()
        if isinstance(entry, dict) and not entry.get("disabled")
    }


# ---- what the HUD is told ----------------------------------------------------

# Told what each service is doing as it happens, for the HUD's services strip:
# (server, state), where state is one of the words below. Only real events
# are reported; nothing here animates on its own.
CONNECTED = "connected"
UNAVAILABLE = "unavailable"
BUSY = "busy"
IDLE = "idle"
HELD = "held"
DONE = "done"
REFUSED = "refused"

_activity_listener = None


def set_activity_listener(listener):
    """Register a callable taking (server name, state)."""
    global _activity_listener
    _activity_listener = listener


def _report(server, state):
    if _activity_listener:
        try:
            _activity_listener(server, state)
        except Exception as error:
            print(f"[JARVIS] could not show service activity: {error}")


def configured():
    """Names of the servers in mcp.json, whether or not they are connected."""
    return tuple(_read_config())


_SPOKEN_WORD = re.compile(r"[a-z0-9]+")

# Speech recognition splits one written name into up to this many words:
# "filesystem" arrives as "file system", "askfiles" as "ask files".
_MAX_NAME_WORDS = 4


def _squash(text):
    """Letters and digits only: 'file system', 'file-system' and 'filesystem' agree."""
    return "".join(_SPOKEN_WORD.findall(str(text or "").casefold()))


def mentioned(text):
    """The configured service a command names, or None.

    Matched on the server's name in mcp.json and any aliases listed for it,
    ignoring spaces and punctuation, against whole words of the command: so
    "use file system to list my documents" finds "filesystem" and "git hub"
    finds "github", while "contest" does not find "test". Nothing has to be
    written into JARVIS for a new service.
    """
    words = _SPOKEN_WORD.findall(str(text or "").casefold())

    runs = {
        "".join(words[start:start + size])
        for size in range(1, _MAX_NAME_WORDS + 1)
        for start in range(len(words) - size + 1)
    }

    for name, entry in _read_config().items():
        spoken = {_squash(name)}
        spoken.update(_squash(alias) for alias in entry.get("aliases") or ())
        spoken.discard("")

        if spoken & runs:
            return name

    return None


# ---- tools ----------------------------------------------------------------

def _agent_name(server, tool):
    """A tool name the model APIs accept: letters, digits, _ and -, 64 at most."""
    name = _NAME_UNSAFE.sub("_", f"mcp_{server}_{tool}").strip("_")

    if len(name) > 64:
        digest = hashlib.sha1(name.encode()).hexdigest()[:8]
        name = f"{name[:55]}_{digest}"

    return name


def _read_only(tool, entry):
    trusted = set(entry.get("trusted_read_only") or ())

    if tool.name in trusted:
        return True

    annotations = getattr(tool, "annotations", None)
    return bool(annotations and getattr(annotations, "read_only_hint", None) is True)


def _describe(server, tool):
    """The server's description, cleaned, or None if it reads like orders."""
    text = " ".join(
        part for part in (getattr(tool, "title", None), getattr(tool, "description", None)) if part
    )

    cleaned = safety.clean(text, DESCRIPTION_CHARS)

    if safety.looks_like_instruction(cleaned):
        print(f"[JARVIS] {server}: dropping tool {tool.name!r}; its description reads like an instruction")
        return None

    return f"From the connected service '{server}': {cleaned or tool.name}"


def _destructive(tool):
    annotations = getattr(tool, "annotations", None)
    return bool(annotations and getattr(annotations, "destructive_hint", None) is True)


def _schema(tool):
    schema = getattr(tool, "input_schema", None) or {"type": "object", "properties": {}}

    if not isinstance(schema, dict):
        schema = json.loads(json.dumps(schema, default=lambda o: getattr(o, "__dict__", str(o))))

    return schema


def _convert(server, entry, listed):
    """(read-only tools, actions) a server may offer, as agent tool dicts."""
    kept = []
    actions = []
    allowed = set(entry.get("allowed_actions") or ())

    for tool in listed:
        read_only = _read_only(tool, entry)

        if not read_only and tool.name not in allowed:
            continue

        if not read_only and _destructive(tool):
            print(f"[JARVIS] {server}: not offering {tool.name!r}; the service marks it destructive")
            continue

        description = _describe(server, tool)

        if description is None:
            continue

        if not read_only and entry.get("confirm_actions") is False:
            description = (
                "ACTION, changes something, and runs as soon as it is called, "
                "with no confirmation. " + description
            )
        elif not read_only:
            description = (
                "ACTION, changes something: JARVIS holds this call and asks the "
                "user to confirm it aloud before it runs. " + description
            )

        (kept if read_only else actions).append({
            "name": _agent_name(server, tool.name),
            "description": description,
            "input_schema": _schema(tool),
            "mcp": (server, tool.name),
        })

    unknown = allowed - {tool.name for tool in listed}

    if unknown:
        print(f"[JARVIS] {server}: allowed_actions names tools it does not have: {', '.join(sorted(unknown))}")

    return kept, actions


# ---- the background loop ---------------------------------------------------

def _ensure_loop():
    global _loop

    if _loop is not None:
        return _loop

    ready = threading.Event()

    def run():
        global _loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _loop = loop
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, name="mcp-services", daemon=True).start()
    ready.wait()

    return _loop


def _transport(entry):
    """What mcp.Client should connect to: a URL, a transport, or a command."""
    from mcp.client.stdio import StdioServerParameters

    entry = _expand(entry)

    if entry.get("url"):
        headers = entry.get("headers") or {}

        if not headers:
            return entry["url"]

        # Headers (an API key, say) need an HTTP client of our own. Timeouts
        # match the SDK's own defaults: a server may hold a stream open.
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        http = httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30, read=300))
        return streamable_http_client(entry["url"], http_client=http)

    # Only the basics a program needs to run (PATH, the user's folders), plus
    # what the entry names. Never JARVIS's whole environment: that holds
    # every API key in .env, and a server is someone else's program.
    from mcp.client.stdio import get_default_environment

    env = get_default_environment()
    env.update(entry.get("env") or {})

    return StdioServerParameters(
        command=entry["command"],
        args=list(entry.get("args") or ()),
        env=env,
        cwd=entry.get("cwd"),
    )


async def _hold(server):
    """Open one server and keep it open until asked to close."""
    server._stop = asyncio.Event()

    try:
        # Inside the try: a missing package used to escape the coroutine
        # before _ready was set, and surfaced 20 seconds later as "did not
        # answer in time".
        from mcp import Client

        target = _transport(server.entry)

        async with Client(target, read_timeout_seconds=CALL_SECONDS) as client:
            listed = []
            cursor = None

            while True:
                page = await client.list_tools(cursor=cursor)
                listed.extend(page.tools)
                cursor = getattr(page, "next_cursor", None)
                if not cursor:
                    break

            server.client = client
            server.tools, server.actions = _convert(server.name, server.entry, listed)
            server._ready.set()

            await server._stop.wait()

    except KeyError as missing:
        server.error = f"{missing.args[0]} is not set in the environment or .env"
    except ModuleNotFoundError as missing:
        server.error = (
            f"the {missing.name or 'mcp'} package is not installed in this Python; "
            "run pip install -r requirements.txt"
        )
    except Exception as error:
        server.error = str(error) or type(error).__name__
    finally:
        server.client = None
        server._ready.set()


def _connect(name, entry):
    server = _Server(name, entry)
    server._ready = threading.Event()

    asyncio.run_coroutine_threadsafe(_hold(server), _ensure_loop())

    if not server._ready.wait(CONNECT_SECONDS):
        server.error = "did not answer in time"

    if server.error or server.client is None:
        print(f"[JARVIS] connected service {name!r} unavailable: {server.error}")
        journal.write("mcp", f"connect {name}", f"unavailable: {server.error}")
        _report(name, UNAVAILABLE)
        return server

    offered = f"{len(server.tools)} read-only tools"

    if server.actions:
        offered += f", {len(server.actions)} action(s) with spoken confirmation"

    print(f"[JARVIS] connected service {name!r}: {offered}")
    journal.write("mcp", f"connect {name}", offered)
    _report(name, CONNECTED)

    return server


def tools(include_actions=False):
    """Agent tool definitions from every connected service. Connects on first use.

    Actions are included only when asked for: a command that names the
    service, never an ordinary investigation.
    """
    global _loaded

    with _lock:
        if not _loaded:
            _loaded = True

            for name, entry in _read_config().items():
                _register(_connect(name, entry))

        _retry_failed()

        offered = [tool for server in _servers.values() if server.client for tool in server.tools]

        if include_actions:
            offered += [tool for server in _servers.values() if server.client for tool in server.actions]

        return offered


def _register(server, previous=None):
    """Put a server's tools on offer, and schedule a retry if it failed."""
    _servers[server.name] = server

    if server.client is None:
        wait = RETRY_SECONDS if previous is None or not previous.backoff else min(previous.backoff * 2, MAX_RETRY_SECONDS)
        server.backoff = wait
        server.retry_at = time.monotonic() + wait
        return

    for tool in server.tools:
        _tools[tool["name"]] = tool["mcp"]

    for tool in server.actions:
        _actions[tool["name"]] = tool["mcp"]

        if server.entry.get("confirm_actions") is False:
            _immediate.add(tool["name"])


def _retry_failed():
    """Try again any service that could not be reached, once its wait is up."""
    now = time.monotonic()

    for name, server in list(_servers.items()):
        # Never connected, or connected and since gone (OBS closed, say).
        if server.client is None and now >= server.retry_at:
            entry = _read_config().get(name)

            if entry is None:
                continue

            _register(_connect(name, entry), previous=server)


def owns(tool_name):
    return tool_name in _tools or tool_name in _actions


def is_action(tool_name):
    return tool_name in _actions


def needs_confirmation(tool_name):
    """False for an action its service lets run without asking."""
    return tool_name not in _immediate


def run_now(tool_name, arguments):
    """Run an action whose service does not ask first. Returns what the model sees."""
    target = _actions.get(tool_name)

    if not target or tool_name not in _immediate:
        return {"error": f"Not an action that runs without confirmation: {tool_name}"}

    proposal = {"name": tool_name, "server": target[0], "tool": target[1], "arguments": dict(arguments or {})}

    if execute(proposal) is None:
        return {"error": proposal.get("failure") or "The service did not do it."}

    return safety.quote(f"{target[0]}_result", proposal.get("result_text", "Done."), RESULT_CHARS)


# ---- actions: held, read back, run only on a spoken yes ---------------------

def clear_proposal():
    global _proposal

    with _lock:
        _proposal = None


def propose(tool_name, arguments):
    """Hold an action the model asked for, instead of running it."""
    global _proposal

    target = _actions.get(tool_name)

    if not target:
        return {"error": f"Unknown connected-service action: {tool_name}"}

    with _lock:
        if _proposal is not None:
            return {"error": "One action per request, and one is already held for the user to confirm."}

        _proposal = {"name": tool_name, "server": target[0], "tool": target[1], "arguments": dict(arguments or {})}

    journal.write("mcp", f"held {target[0]}.{target[1]}", "awaiting a spoken yes")
    _report(target[0], HELD)

    return (
        f"Held for the user's spoken confirmation: {describe(_proposal)}. It has not run. "
        "Do not call it again. JARVIS reads the action back to the user itself, so do not "
        "describe it or ask for confirmation. Finish with one short sentence only if the "
        "user should know something before agreeing, such as a surprise you found; "
        f"otherwise reply with exactly: {NOTHING_TO_ADD}"
    )


# What the model answers after holding an action when it has nothing to add.
NOTHING_TO_ADD = "NOTHING TO ADD"


def take_proposal():
    """The held action, removed, or None."""
    global _proposal

    with _lock:
        proposal, _proposal = _proposal, None

    return proposal


def _spoken_value(name, value):
    label = name.replace("_", " ")

    if isinstance(value, bool):
        return f"{label} {'yes' if value else 'no'}"

    if isinstance(value, (int, float)):
        return f"{label} {value}"

    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
        shown = ", ".join(items[:5]) + (f" and {len(items) - 5} more" if len(items) > 5 else "")
        return f"{label} {shown}"

    if isinstance(value, dict):
        return f"{label} with {len(value)} field(s)"

    text = " ".join(str(value).split())
    words = text.split()

    # Short values are read in full: they are what is being agreed to.
    if len(text) <= 80:
        return f"{label} '{text}'"

    return f"{label} of {len(words)} words, beginning '{' '.join(words[:12])}'"


def describe(proposal):
    """Exactly what an action would do, from its real arguments, for reading back."""
    tool = proposal["tool"].replace("_", " ")
    details = [
        _spoken_value(name, value)
        for name, value in proposal["arguments"].items()
        if value not in (None, "", [], {})
    ]

    said = f"{tool} on {proposal['server']}"

    return f"{said}: {'; '.join(details)}" if details else said


def execute(proposal):
    """Run an action the user said yes to. Returns what to say, or None on failure."""
    server = _servers.get(proposal["server"])

    if not server or not server.client:
        proposal["failure"] = f"{_spoken_server(proposal)} couldn't be reached."
        journal.action(f"mcp_{proposal['tool']}", proposal["server"], False)
        _report(proposal["server"], REFUSED)
        return None

    summary = f"{proposal['server']}.{proposal['tool']} {json.dumps(proposal['arguments'], ensure_ascii=False)[:200]}"

    _report(proposal["server"], BUSY)

    try:
        future = asyncio.run_coroutine_threadsafe(
            server.client.call_tool(proposal["tool"], proposal["arguments"]), _loop
        )
        result = future.result(CALL_SECONDS + 5)
    except Exception as error:
        proposal["failure"] = f"{_spoken_server(proposal)} couldn't be reached."
        journal.write("mcp action", summary, f"failed: {error}")
        journal.action(f"mcp_{proposal['tool']}", proposal["server"], False)
        _report(proposal["server"], REFUSED)
        return None

    text = _result_text(result)

    if getattr(result, "is_error", False):
        proposal["failure"] = _refusal(proposal, text)
        journal.write("mcp action", summary, f"refused: {safety.clean(text, 300)}")
        journal.action(f"mcp_{proposal['tool']}", proposal["server"], False)
        _report(proposal["server"], REFUSED)
        return None

    proposal["result_text"] = text
    journal.write("mcp action", summary, "done")
    journal.action(f"mcp_{proposal['tool']}", proposal["server"], True,
                   spoken=f"{proposal['tool'].replace('_', ' ')} on {proposal['server']}")
    _report(proposal["server"], DONE)

    return _outcome(proposal, text)


def _spoken_server(proposal):
    return proposal["server"].replace("_", " ").capitalize()


# Why a service refused, from the HTTP status its error carries: the status
# is the standard, service-independent part of an error. Anything else is
# reported as a plain refusal; the full text is in the journal.
_REFUSALS = {
    "401": "didn't accept your key or token; it may have expired",
    "403": "says your token isn't allowed to do that",
    "404": "couldn't find that; the name or number may be wrong",
    "409": "says that clashes with its current state",
    "410": "says that no longer exists",
    "422": "rejected the details",
    "429": "is limiting requests; try again in a minute",
}


def _refusal(proposal, text):
    """What to say when a service refused an action."""
    # A status is a 4xx followed by its reason ("403 Forbidden"), never a
    # number inside a URL such as issues/403/comments.
    statuses = re.findall(r"(?<![\w/.#-])(4\d\d)(?=\s+[A-Za-z])", str(text or ""))
    reason = next((_REFUSALS[s] for s in reversed(statuses) if s in _REFUSALS), "refused it")

    return f"{_spoken_server(proposal)} {reason}."


def failure_message(proposal):
    """The spoken line for an action that did not go through."""
    reason = proposal.get("failure") or f"{_spoken_server(proposal)} refused it or couldn't be reached."

    return f"That didn't go through, sir. {reason}"


def _outcome(proposal, text):
    """A short spoken result: what was made and where, when the service says."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None

    if isinstance(data, dict):
        number = data.get("number")
        link = data.get("html_url") or data.get("url")

        if number is not None:
            return f"Done, sir. That's number {number} on {proposal['server']}."

        if link:
            return f"Done, sir. It's on {proposal['server']} now."

    return "Done, sir."


def _resource_text(resource):
    """The text of an embedded resource, such as a file GitHub sends back."""
    text = getattr(resource, "text", None)

    if text is not None:
        return str(text)

    blob = getattr(resource, "blob", None)

    if blob:
        import base64

        try:
            return base64.b64decode(blob).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass

    mime = getattr(resource, "mime_type", None) or "binary"
    return f"[{mime} file at {getattr(resource, 'uri', '')} not shown]"


def _result_text(result):
    parts = []

    for item in getattr(result, "content", None) or ():
        kind = getattr(item, "type", None)
        text = getattr(item, "text", None)

        if text is not None:
            parts.append(str(text))
        elif kind == "resource":
            # Servers send files this way: GitHub's get_file_contents
            # answers with the README as a resource, not as text. Showing
            # the model a placeholder instead had it retry until it ran
            # out of turns.
            parts.append(_resource_text(getattr(item, "resource", None)))
        elif kind == "resource_link":
            parts.append(f"[link: {getattr(item, 'name', '')} {getattr(item, 'uri', '')}]")
        else:
            parts.append(f"[{kind or 'non-text'} content omitted]")

    structured = getattr(result, "structured_content", None)

    if structured and not parts:
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))

    return "\n".join(parts)


def call(tool_name, arguments):
    """Run one connected-service tool. Returns quoted data, or an error dict."""
    target = _tools.get(tool_name)

    if not target:
        return {"error": f"Unknown connected-service tool: {tool_name}"}

    server_name, tool = target
    server = _servers.get(server_name)

    if not server or not server.client:
        return {"error": f"The connected service '{server_name}' is not available."}

    summary = f"{server_name}.{tool} {json.dumps(arguments or {}, ensure_ascii=False)[:200]}"

    _report(server_name, BUSY)

    try:
        future = asyncio.run_coroutine_threadsafe(
            server.client.call_tool(tool, arguments or {}), _loop
        )
        result = future.result(CALL_SECONDS + 5)
    except Exception as error:
        journal.write("mcp", summary, f"failed: {error}")
        _report(server_name, IDLE)
        return {"error": f"The connected service '{server_name}' failed: {error}"}

    _report(server_name, IDLE)

    text = _result_text(result)

    if getattr(result, "is_error", False):
        journal.write("mcp", summary, "tool reported an error")
        return {"error": safety.clean(text, 2000) or "The tool reported an error."}

    journal.write("mcp", summary, f"{len(text)} characters")

    return safety.quote(f"{server_name}_result", text, RESULT_CHARS)


def close():
    """Close every connection. For shutdown and tests."""
    global _loaded

    with _lock:
        for server in _servers.values():
            if server._stop is not None and _loop is not None:
                _loop.call_soon_threadsafe(server._stop.set)

        _servers.clear()
        _tools.clear()
        _actions.clear()
        _immediate.clear()
        _loaded = False

    clear_proposal()


def status():
    """One line per configured service, for 'what services are connected'."""
    lines = []

    for name in configured():
        server = _servers.get(name)

        if server is None:
            lines.append(f"{name}: not connected yet")
        elif server.client:
            lines.append(f"{name}: connected, {len(server.tools)} read-only tools")
        else:
            lines.append(f"{name}: unavailable ({server.error})")

    return lines
