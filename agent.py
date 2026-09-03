"""JARVIS Agent Mode: read-only tools for Claude."""

import json
import os
from datetime import datetime

from anthropic import AnthropicFoundry
from dotenv import load_dotenv

from actions import files
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


def run_agent(task, report=False):
    """Run a bounded Claude tool-use loop for a read-only investigation."""
    task = str(task or "").strip()

    if not task:
        return None

    api_key = os.getenv("ANTHROPIC_FOUNDRY_API_KEY")
    base_url = os.getenv("ANTHROPIC_FOUNDRY_BASE_URL")
    model = os.getenv("ANTHROPIC_FOUNDRY_MODEL") or "claude-opus-5"
    effort = (
        os.getenv("CLAUDE_ANSWER_EFFORT")
        or os.getenv("CLAUDE_DEFAULT_EFFORT")
        or "high"
    ).strip().casefold()

    if not api_key or not base_url:
        return "Claude Agent Mode is not configured, sir."

    client = AnthropicFoundry(
        api_key=api_key,
        base_url=base_url,
    )

    tool_defs = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["input_schema"],
            "strict": True,
        }
        for tool in TOOLS
    ]

    if report:
        user_prompt = (
            "Investigate the user's request using the available tools, then "
            "write the detailed report described in your instructions. "
            "Do not ask whether the user wants a report; the user has already "
            "confirmed."
        )
    else:
        user_prompt = (
            "Investigate the user's request using the available tools, then "
            "give only the concise spoken summary described in your instructions."
        )

    messages = [
        {
            "role": "user",
            "content": f"{user_prompt}\n\nUser request:\n{task}",
        }
    ]

    max_turns = _AGENT_MAX_TURNS

    for _ in range(max_turns):
        kwargs = {
            "model": model,
            "max_tokens": 8000 if report else 1800,
            "system": _AGENT_SYSTEM_PROMPT,
            "tools": tool_defs,
            "tool_choice": {
                "type": "auto",
                "disable_parallel_tool_use": not report,
            },
            "messages": messages,
        }

        if effort:
            kwargs["output_config"] = {
                "effort": effort,
            }

        try:
            response = client.messages.create(**kwargs)
        except Exception as error:
            print(f"[JARVIS] agent request failed: {error}")
            return None

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

            tool_name = block.name
            function = TOOL_FUNCTIONS.get(tool_name)

            if function is None:
                result = {
                    "error": f"Unknown tool requested: {tool_name}"
                }
            else:
                try:
                    result = function(**block.input)
                except Exception as error:
                    result = {
                        "error": str(error),
                    }

            if isinstance(result, (dict, list)):
                result_text = json.dumps(
                    result,
                    ensure_ascii=False,
                )
            else:
                result_text = str(result)

            if len(result_text) > 12_000:
                result_text = (
                    result_text[:12_000]
                    + "\n[tool result truncated]"
                )

            print(f"[JARVIS] agent tool: {tool_name}")

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                }
            )

        if not results:
            print("[JARVIS] agent requested tools but supplied none")
            return None

        messages.append(
            {
                "role": "user",
                "content": results,
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
