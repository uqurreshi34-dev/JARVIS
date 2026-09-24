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

- Only tools the server marks read-only are offered. Anything else --
  including a tool that says nothing either way -- is left out, because
  Agent Mode is read-only and a service's word that a tool is harmless is
  the most that can be checked. A server entry can name tools in
  "trusted_read_only" when its author forgot the mark and the user has
  checked them.
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
_loaded = False


class _Server:
    def __init__(self, name, entry):
        self.name = name
        self.entry = entry
        self.client = None
        self.tools = []       # agent tool dicts
        self.error = None
        self._stop = None
        self._ready = None


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


def configured():
    """Names of the servers in mcp.json, whether or not they are connected."""
    return tuple(_read_config())


def mentioned(text):
    """The configured service a command names, or None.

    Matched on the server's name in mcp.json and any aliases listed for it,
    as whole words, so "look at my github issues" finds "github" and nothing
    has to be written into JARVIS for a new service.
    """
    lowered = f" {' '.join(str(text or '').casefold().split())} "

    for name, entry in _read_config().items():
        spoken = {name.casefold().replace("_", " ").replace("-", " ")}
        spoken.update(
            str(alias).casefold() for alias in entry.get("aliases") or () if str(alias).strip()
        )

        for word in spoken:
            if f" {word} " in lowered:
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


def _convert(server, entry, listed):
    kept = []

    for tool in listed:
        if not _read_only(tool, entry):
            continue

        description = _describe(server, tool)

        if description is None:
            continue

        schema = getattr(tool, "input_schema", None) or {"type": "object", "properties": {}}

        if not isinstance(schema, dict):
            schema = json.loads(json.dumps(schema, default=lambda o: getattr(o, "__dict__", str(o))))

        kept.append({
            "name": _agent_name(server, tool.name),
            "description": description,
            "input_schema": schema,
            "mcp": (server, tool.name),
        })

    return kept


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
    from mcp import Client

    server._stop = asyncio.Event()

    try:
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
            server.tools = _convert(server.name, server.entry, listed)
            server._ready.set()

            await server._stop.wait()

    except KeyError as missing:
        server.error = f"{missing.args[0]} is not set in the environment or .env"
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
        return server

    print(f"[JARVIS] connected service {name!r}: {len(server.tools)} read-only tools")
    journal.write("mcp", f"connect {name}", f"{len(server.tools)} read-only tools")

    return server


def tools():
    """Agent tool definitions from every connected service. Connects on first use."""
    global _loaded

    with _lock:
        if not _loaded:
            _loaded = True

            for name, entry in _read_config().items():
                server = _connect(name, entry)
                _servers[name] = server

                for tool in server.tools:
                    _tools[tool["name"]] = tool["mcp"]

        return [tool for server in _servers.values() if server.client for tool in server.tools]


def owns(tool_name):
    return tool_name in _tools


def _result_text(result):
    parts = []

    for item in getattr(result, "content", None) or ():
        text = getattr(item, "text", None)

        if text is not None:
            parts.append(str(text))
        else:
            parts.append(f"[{getattr(item, 'type', 'non-text')} content omitted]")

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

    try:
        future = asyncio.run_coroutine_threadsafe(
            server.client.call_tool(tool, arguments or {}), _loop
        )
        result = future.result(CALL_SECONDS + 5)
    except Exception as error:
        journal.write("mcp", summary, f"failed: {error}")
        return {"error": f"The connected service '{server_name}' failed: {error}"}

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
        _loaded = False


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
