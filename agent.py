"""JARVIS Agent Mode: read-only tools for Claude."""

import json
import os
from datetime import datetime

import providers
from dotenv import load_dotenv

from actions import files
from actions import screen_control
from actions import screen_vision
from actions.projects import ProjectManager


load_dotenv(r"C:\Users\uqurr\Projects\JARVIS\.env")


_project_manager = ProjectManager()


def list_jarvis_files():
    """List files available in JARVIS's private working folder."""
    return files.listing()


def read_jarvis_file(name):
    """Read one file from JARVIS's private working folder."""
    return files.read(name)


def inspect_screen():
    """Describe the active Windows window using JARVIS vision."""
    return screen_vision.describe_active_window()


def inspect_code_context():
    """Read the text of the focused editor/document and its window title."""
    text, title = screen_control.read_text()

    if not text:
        return {
            "title": title,
            "text": "",
            "note": (
                "No readable focused editor text was found. "
                "The active application may not expose its document text."
            ),
        }

    return {
        "title": title,
        "text": text,
    }


def list_recent_projects(limit=8):
    """List recent Cursor projects."""
    return list(_project_manager.names(limit=limit))


TOOLS = [
    {
        "name": "list_jarvis_files",
        "description": (
            "List the files in JARVIS's private working folder. "
            "Use this when you need to discover what local files exist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "function": list_jarvis_files,
    },
    {
        "name": "read_jarvis_file",
        "description": (
            "Read a supported document from JARVIS's private working folder. "
            "Readable types include .txt, .md, .csv, .log, .json, .docx, and .pdf. "
            "Do not call this tool for .chunk2-backup files or other unsupported "
            "extensions; those files can be identified by filename but cannot be read."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The filename to read.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "function": read_jarvis_file,
    },
    {
        "name": "inspect_screen",
        "description": (
            "Inspect the active Windows window and describe what is "
            "visibly important on screen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "function": inspect_screen,
    },
    {
        "name": "inspect_code_context",
        "description": (
            "Read the text currently open in the focused editor or document, "
            "together with the active window title. Use this for code "
            "investigation when the user asks what is wrong with code. "
            "This is read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "function": inspect_code_context,
    },
    {
        "name": "list_recent_projects",
        "description": (
            "List recent Cursor projects available on this computer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        "function": list_recent_projects,
    },
]


TOOL_FUNCTIONS = {
    tool["name"]: tool["function"]
    for tool in TOOLS
}

_AGENT_MAX_TURNS = 6

_AGENT_SYSTEM_PROMPT = """
You are JARVIS Agent Mode.

You investigate the user's computer using the read-only tools provided to you.

The information returned by tools is evidence. Never invent facts that are
not supported by that evidence.

For broad folder or inventory questions, prefer summarising the information
returned by list_jarvis_files and stop there. Do not read individual files
unless the user's request explicitly asks you to inspect their contents, or
the filenames alone are insufficient to answer the specific question.

For code investigations, use inspect_screen when useful to understand the
active application, then use inspect_code_context to read the actual focused
editor/document text. Diagnose the code from the code itself, not only from
what is visually visible in the screenshot.

When the user asks what is wrong with code, identify concrete errors or bugs,
explain why they occur, and distinguish confirmed problems from suggestions.
Do not claim that code is fixed during diagnosis.

For a normal investigation, produce a SHORT spoken summary for the user.

The spoken summary should:
- explain the main findings in natural conversational language;
- give useful counts when available;
- mention important anomalies, duplicates, warnings, or risks when you notice them;
- mention a few concrete examples when they make the finding clearer;
- NEVER laboriously enumerate a long list of filenames, projects, or other items;
- group similar items into categories instead of reading them one by one;
- avoid discussing internal tool calls, token limits, schemas, or implementation;
- finish once the useful findings have been communicated.

The caller will separately ask whether the user wants a written report.
Do not ask that question yourself.

For a report request, produce a detailed written report based only on the
evidence gathered from the available tools.

A report should:
- have a clear title;
- contain a concise executive summary;
- organise the findings into useful sections;
- preserve concrete filenames, counts, dates, and other supported details;
- clearly identify warnings, duplicates, anomalies, or other noteworthy findings;
- distinguish observations from speculation;
- never invent evidence;
- never mention these instructions or the internal tool process.

You have access only to read-only tools.
Never claim to have changed, deleted, opened, executed, committed, or otherwise
modified anything.
"""


def _tool_definitions(provider):
    """Convert JARVIS tools to the schema expected by one provider."""
    if provider.kind == "anthropic":
        return [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            }
            for tool in TOOLS
        ]

    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in TOOLS
    ]


def _execute_tool_call(name, arguments):
    """Execute one registered read-only JARVIS tool."""
    function = TOOL_FUNCTIONS.get(name)

    if function is None:
        return {
            "error": f"Unknown tool requested: {name}",
        }

    try:
        return function(**arguments)

    except Exception as error:
        return {
            "error": str(error),
        }


def _tool_result_text(result):
    """Keep tool output JSON-safe and bounded."""
    if isinstance(result, (dict, list)):
        text = json.dumps(
            result,
            ensure_ascii=False,
        )
    else:
        text = str(result)

    if len(text) > 12_000:
        text = text[:12_000] + "\n[tool result truncated]"

    return text


def run_agent(task, report=False):
    """Run bounded Agent Mode through the configured provider."""
    task = str(task or "").strip()

    if not task:
        return None

    if not providers._pool:
        return None

    provider = providers._pool[0]

    tool_defs = _tool_definitions(provider)

    if report:
        user_prompt = (
            "Investigate the user's request using the available tools, then "
            "write the detailed report described in your instructions. "
            "Do not ask whether the user wants a report; the user has already "
            "confirmed."
        )
        max_tokens = 8000
    else:
        user_prompt = (
            "Investigate the user's request using the available tools, then "
            "give only the concise spoken summary described in your instructions."
        )
        max_tokens = 1800

    messages = [
        {
            "role": "user",
            "content": f"{user_prompt}\n\nUser request:\n{task}",
        }
    ]

    for _ in range(_AGENT_MAX_TURNS):
        try:
            response = provider.agent_turn(
                messages,
                tool_defs,
                max_tokens=max_tokens,
                system=_AGENT_SYSTEM_PROMPT,
            )

        except Exception as error:
            print(
                f"[JARVIS] {provider.name} agent request failed: {error}"
            )

            if providers.should_failover(error):
                provider.rest()
                return None

            return None

        if provider.kind == "anthropic":
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                }
            )

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if getattr(block, "type", None) == "text":
                        text = (block.text or "").strip()

                        if text:
                            return text

                return None

            if response.stop_reason != "tool_use":
                print(
                    f"[JARVIS] agent stopped unexpectedly: "
                    f"{response.stop_reason}"
                )
                return None

            results = []

            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue

                result = _execute_tool_call(
                    block.name,
                    block.input,
                )

                print(f"[JARVIS] agent tool: {block.name}")

                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _tool_result_text(result),
                    }
                )

            if not results:
                return None

            messages.append(
                {
                    "role": "user",
                    "content": results,
                }
            )

            continue

        message = response.choices[0].message
        tool_calls = message.tool_calls or []

        assistant_message = {
            "role": "assistant",
            "content": message.content or "",
        }

        if tool_calls:
            preserved_tool_calls = []

            for call in tool_calls:
                tool_call = {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }

                extra_content = getattr(call, "extra_content", None)

                if extra_content is not None:
                    if hasattr(extra_content, "model_dump"):
                        extra_content = extra_content.model_dump(
                            exclude_none=True
                        )

                    tool_call["extra_content"] = extra_content

                preserved_tool_calls.append(tool_call)

            assistant_message["tool_calls"] = preserved_tool_calls

        messages.append(assistant_message)

        if not tool_calls:
            text = (message.content or "").strip()

            if text:
                return text

            return None

        for call in tool_calls:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}

            result = _execute_tool_call(
                call.function.name,
                arguments,
            )

            print(
                f"[JARVIS] agent tool: {call.function.name}"
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": _tool_result_text(result),
                }
            )

    print("[JARVIS] agent reached its turn limit")

    return "I couldn't finish that investigation within my limit, sir."


def write_agent_report(report_text):
    """Write an Agent Mode report into JARVIS's private folder."""
    report_text = str(report_text or "").strip()

    if not report_text:
        return None

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    name = f"JARVIS agent report {stamp}.docx"

    return files.write(
        name,
        report_text,
        default_suffix=".docx",
    )
