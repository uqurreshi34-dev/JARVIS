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

_CODE_SUFFIXES = frozenset({
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".cs",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".html",
    ".css",
    ".sql",
    ".sh",
    ".ps1",
})

_SKIP_CODE_DIRS = frozenset({
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
})


def _active_code_filename(title):
    """Extract a code filename from common editor window-title formats."""
    if not title:
        return None

    title = str(title).strip()

    normalised = (
        title
        .replace(" — ", " - ")
        .replace(" – ", " - ")
        .replace(" | ", " - ")
        .replace(" · ", " - ")
    )

    parts = normalised.split(" - ")

    for part in parts:
        candidate = part.strip().lstrip("*").strip()
        suffix = os.path.splitext(candidate)[1].casefold()

        if suffix in _CODE_SUFFIXES:
            return os.path.basename(candidate)

    return None


def _read_saved_active_code(title):
    """Read the active saved code file without an expensive project-wide scan."""
    filename = _active_code_filename(title)

    if not filename:
        return None

    filename_key = filename.casefold()

    # First try the directory JARVIS is currently running from.
    # This is cheap and resolves files in the active project immediately.
    current_directory = os.path.abspath(os.getcwd())
    direct_path = os.path.join(current_directory, filename)

    if os.path.isfile(direct_path):
        try:
            with open(
                direct_path,
                "r",
                encoding="utf-8",
                errors="ignore",
            ) as handle:
                return {
                    "path": direct_path,
                    "text": handle.read(),
                }

        except (OSError, UnicodeError):
            pass

    # Then try the directory containing agent.py.
    # This also covers the normal JARVIS project when launched elsewhere.
    module_directory = os.path.dirname(
        os.path.abspath(__file__)
    )
    module_path = os.path.join(module_directory, filename)

    if (
        os.path.isfile(module_path)
        and os.path.normcase(module_path) != os.path.normcase(direct_path)
    ):
        try:
            with open(
                module_path,
                "r",
                encoding="utf-8",
                errors="ignore",
            ) as handle:
                return {
                    "path": module_path,
                    "text": handle.read(),
                }

        except (OSError, UnicodeError):
            pass

    # Finally use recent project roots, but only inspect the root itself
    # before falling back to a bounded recursive search.
    roots = []

    for project in _project_manager.projects(refresh=True):
        root = os.path.abspath(project.path)

        if not os.path.isdir(root):
            continue

        roots.append(root)

        candidate = os.path.join(root, filename)

        if os.path.isfile(candidate):
            try:
                with open(
                    candidate,
                    "r",
                    encoding="utf-8",
                    errors="ignore",
                ) as handle:
                    return {
                        "path": candidate,
                        "text": handle.read(),
                    }

            except (OSError, UnicodeError):
                continue

    # Only recurse when necessary, and stop as soon as two matches exist.
    matches = []

    for root in roots:
        for current_root, directories, filenames in os.walk(root):
            directories[:] = [
                directory
                for directory in directories
                if directory.casefold() not in _SKIP_CODE_DIRS
            ]

            for name in filenames:
                if name.casefold() != filename_key:
                    continue

                matches.append(os.path.join(current_root, name))

                if len(matches) > 1:
                    return None

    if len(matches) != 1:
        return None

    path = matches[0]

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
            errors="ignore",
        ) as handle:
            return {
                "path": path,
                "text": handle.read(),
            }

    except (OSError, UnicodeError):
        return None


def list_jarvis_files():
    """List files available in JARVIS's private working folder."""
    return files.listing()


def list_directory(path):
    """Summarise the contents of any existing local directory."""
    path = os.path.abspath(str(path or "").strip())

    if not os.path.isdir(path):
        return {
            "path": path,
            "error": "Directory not found.",
        }

    try:
        entries = os.listdir(path)
    except OSError as error:
        return {
            "path": path,
            "error": str(error),
        }

    directories = []
    file_counts = {}

    for name in entries:
        full_path = os.path.join(path, name)

        if os.path.isdir(full_path):
            directories.append(name)
            continue

        if os.path.isfile(full_path):
            suffix = os.path.splitext(name)[1].casefold() or "[no extension]"
            file_counts[suffix] = file_counts.get(suffix, 0) + 1

    return {
        "path": path,
        "total_entries": len(entries),
        "directories": sorted(directories),
        "file_counts_by_extension": dict(sorted(file_counts.items())),
    }


def read_jarvis_file(name):
    """Read one file from JARVIS's private working folder."""
    return files.read(name)


def inspect_screen():
    """Describe the active Windows window using JARVIS vision."""
    return screen_vision.describe_active_window()


def inspect_code_context():
    """Read the focused code from the editor, or its saved project file."""
    text, title, unsaved = screen_control.active_document_state()

    if text:
        return {
            "title": title,
            "text": text,
            "source": "editor",
            "unsaved_changes": unsaved,
        }

    if unsaved:
        return {
            "title": title,
            "text": "",
            "source": "unavailable",
            "unsaved_changes": True,
            "note": (
                "The editor text was not accessible and the document has "
                "unsaved changes. Do not reconstruct or overwrite the file."
            ),
        }

    saved = _read_saved_active_code(title)

    if saved:
        return {
            "title": title,
            "path": saved["path"],
            "text": saved["text"],
            "source": "disk",
            "unsaved_changes": False,
        }

    return {
        "title": title,
        "text": "",
        "source": "unavailable",
        "unsaved_changes": False,
        "note": (
            "The editor text was not accessible and the saved code file "
            "could not be uniquely identified. Do not reconstruct or "
            "overwrite the document."
        ),
    }


def replace_focused_code(code):
    """Replace the focused code document without saving it."""
    return screen_control.replace_focused_text(code)


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
        "name": "list_directory",
        "description": (
            "Inspect any local directory specified by the user. "
            "Use this for requests about a specific folder, directory, "
            "drive, or filesystem location. Return a concise summary "
            "rather than enumerating every file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The local directory path to inspect.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "function": list_directory,
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
            "Read the actual source code in the focused editor or document. "
            "If editor text is inaccessible, safely fall back to a uniquely "
            "identified saved code file in a recent project. Do not use this "
            "tool to reconstruct code from a screenshot, and do not overwrite "
            "a document whose unsaved editor contents cannot be read. "
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
        "name": "replace_focused_code",
        "description": (
            "Replace the complete contents of the currently focused code "
            "document with corrected code. This changes the editor contents "
            "but does not save the file, commit, or push anything. Use this "
            "only during an explicitly approved code-fix pass."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "The complete corrected contents of the code document."
                    ),
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        "function": replace_focused_code,
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

_CODE_TOOL_NAMES = (
    "inspect_screen",
    "inspect_code_context",
)

_CODE_FIX_TOOL_NAMES = (
    "inspect_screen",
    "inspect_code_context",
    "replace_focused_code",
)

_AGENT_SYSTEM_PROMPT = """
You are JARVIS Agent Mode.

You investigate the user's computer using the read-only tools provided to you.

The information returned by tools is evidence. Never invent facts that are
not supported by that evidence.

For broad JARVIS-folder questions, use list_jarvis_files.
For a specific folder, directory, drive, or filesystem path named by the user,
use list_directory and summarise the returned evidence without enumerating
every file. Do not read individual files
unless the user's request explicitly asks you to inspect their contents, or
the filenames alone are insufficient to answer the specific question.

For code investigations, use inspect_screen when useful to understand the
active application, then use inspect_code_context to obtain the actual
focused source code. Prefer source returned from the editor or the uniquely
identified saved file on disk over visual transcription from the screenshot.

If inspect_code_context reports that the source is unavailable or that the
document has unsaved changes and its editor text cannot be read, do not
reconstruct the file from the screenshot and do not overwrite it. Explain
that the source could not be verified.

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

During normal investigation and report generation, you have access only to
read-only tools.

During an explicitly approved code-fix pass, replace_focused_code is the only
write-capable tool available to you. It changes the focused editor contents
without saving the file.

Never commit, push, or modify any other file. Only claim that code was fixed
after replace_focused_code successfully completed.
"""


def _tool_definitions(
    provider,
    allow_code_fix=False,
    allowed_tool_names=None,
):
    """Convert JARVIS tools to the schema expected by one provider."""
    available_tools = TOOLS

    if allowed_tool_names is not None:
        allowed = set(allowed_tool_names)

        available_tools = [
            tool
            for tool in available_tools
            if tool["name"] in allowed
        ]

    if not allow_code_fix:
        available_tools = [
            tool
            for tool in available_tools
            if tool["name"] != "replace_focused_code"
        ]

    if provider.kind == "anthropic":
        return [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            }
            for tool in available_tools
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
        for tool in available_tools
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


def _clean_spoken_response(text):
    text = str(text or "").strip()

    if text.lower().startswith("**spoken summary:**"):
        text = text[len("**Spoken summary:**"):].strip()

    elif text.lower().startswith("spoken summary:"):
        text = text[len("Spoken summary:"):].strip()

    return text


def run_agent(task, report=False, fix=False, code_task=False):
    """Run bounded Agent Mode through the configured provider."""
    task = str(task or "").strip()

    if not task:
        return None

    if not providers._pool:
        return None

    provider = providers._pool[0]

    allowed_tool_names = None

    if code_task:
        allowed_tool_names = (
            _CODE_FIX_TOOL_NAMES
            if fix
            else _CODE_TOOL_NAMES
        )

    tool_defs = _tool_definitions(
        provider,
        allow_code_fix=fix,
        allowed_tool_names=allowed_tool_names,
    )

    if fix:
        user_prompt = (
            "The user has explicitly approved fixing the code. "
            "Use inspect_code_context to inspect the current code. "
            "Diagnose the confirmed problem and then use "
            "replace_focused_code with the complete corrected code. "
            "Make only the necessary correction. "
            "Do not save the file, commit anything, push anything, "
            "or modify any other file. "
            "Only report success after replace_focused_code succeeds."
        )
        max_tokens = 8000

    elif report:
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
                            return _clean_spoken_response(text)

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
                return _clean_spoken_response(text)

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
