"""Plan bounded multi-step JARVIS requests without provider-specific commands.

The planner translates one compound user request into a short sequence of
ordinary, independently executable JARVIS commands. It does not execute them
itself and it contains no knowledge of particular subjects, people, products,
or speech-recognition misspellings.
"""

import json
import re

import providers


_MAX_STEPS = 6
_MIN_STEPS = 2
_COMPOUND_SEPARATORS = (
    re.compile(r"\s+then\s+", re.IGNORECASE),
    re.compile(r"\s+after that\s+", re.IGNORECASE),
    re.compile(r"\s+followed by\s+", re.IGNORECASE),
    re.compile(r"\s+and\s+", re.IGNORECASE),
)

# These are capabilities, not utterance matchers. The model chooses which
# existing JARVIS command shape fulfils the user's request.
_CAPABILITIES = """
AVAILABLE JARVIS CAPABILITIES

- open or close a local application by name
- open or close a known code project
- open a website or navigate the browser already in use
- search the web or read the current web page
- create, read, append, copy, or inspect files in JARVIS's working folder
- inspect a named local directory or drive
- create or run a user-defined task
- model a supplied reference in Blender
- reconstruct a supplied subject through Tripo and open it in Blender
- inspect the current Blender scene
- modify the current Blender scene using natural language
- restore the original Blender scene
- change volume, media, reminders, calendar, notes, clipboard, images, or
  other capabilities already exposed by ordinary JARVIS commands

Only use capabilities that already exist. Do not invent tools or APIs.
"""

_SYSTEM_PROMPT = """
You are JARVIS's compound-task planner.

Turn one natural-language request into a short ordered sequence of ordinary
JARVIS commands that can be executed independently, one after another.

The user has asked for compound work, not an explanation. Preserve the
user's intent and ordering exactly.

Rules:
- Return 2 to 6 steps. Do not create a plan for a request that is actually one
  ordinary command; set is_compound to false and return no steps.
- Each step must be a single ordinary JARVIS command, not a compound command.
- A step must be something JARVIS already knows how to do.
- Prefer existing capabilities over inventing a new one.
- Preserve references such as "this", "these references", "the current model",
  or "that folder" when they are meaningful in JARVIS context.
- Do not invent a filename, application, project, person, URL, or subject.
- Do not invent missing information. When a prerequisite is absent, keep the
  corresponding step ordinary so the normal JARVIS command can report what is
  missing.
- Keep the number of steps minimal: one user goal per step.
- Never split one atomic operation into meaningless sub-steps.
- Keep steps in strict execution order.
- Do not ask the user questions inside a step.
- Do not encode implementation details, Python, API calls, or internal tool
  names into commands.
- Mark requires_confirmation true when the overall plan contains an action
  that is destructive, externally consequential, or otherwise clearly needs
  user approval. Normal local modelling, inspection, opening, and reading do
  not need confirmation merely because they are multi-step.

Browser continuity is important:
- "open <browser> and open <website>" is one compound task. Open the browser
  application once, then navigate that existing browser to the website.
- When a browser has already been opened earlier in the same plan, use the
  existing-browser navigation capability for later website destinations rather
  than launching a second browser window or tab through the default browser.
- Do not treat a later website destination as a separate fresh-browser launch
  merely because the user said "open".

For modelling requests, distinguish these existing capabilities by intent:
- use Tripo when the user asks for external 3D reconstruction, multiple
  reference images, or explicitly names Tripo;
- use ordinary Blender modelling when the request is to turn the current
  reference image into a Blender model without external reconstruction;
- use Blender modification for a later change to the resulting live scene.

A later step may depend on an earlier one. For example, a scene modification
may sensibly follow a modelling step.

Return only valid JSON matching the supplied schema.

""" + _CAPABILITIES


_SCHEMA = {
    "type": "object",
    "properties": {
        "is_compound": {
            "type": "boolean",
        },
        "summary": {
            "type": "string",
        },
        "requires_confirmation": {
            "type": "boolean",
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                    },
                    "purpose": {
                        "type": "string",
                    },
                },
                "required": ["command", "purpose"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "is_compound",
        "summary",
        "requires_confirmation",
        "steps",
    ],
    "additionalProperties": False,
}


def should_plan(command):
    """True when an utterance is worth offering to the compound planner.

    This is deliberately only a cheap gate. The language model remains the
    authority on whether the request is genuinely compound, so ordinary
    questions containing "and" are not forced into a multi-step execution.
    """
    text = re.sub(r"[^\w\s]", " ", (command or "").casefold())
    text = " ".join(text.split())

    if len(text.split()) < 4:
        return False

    return any(
        marker in f" {text} "
        for marker in (
            " and ",
            " then ",
            " after that ",
            " followed by ",
        )
    )


def _split_compound(command):
    """Split a simple spoken sequence into candidate one-command clauses."""
    text = " ".join((command or "").strip().split())

    if not text:
        return ()

    for separator in _COMPOUND_SEPARATORS:
        parts = separator.split(text)

        if len(parts) > 1:
            cleaned = tuple(
                part.strip(" ,.")
                for part in parts
                if part.strip(" ,.")
            )

            if len(cleaned) >= _MIN_STEPS:
                return cleaned

    return ()


def local_plan(command, resolve):
    """Return a compound plan without an LLM when every clause is already local.

    `resolve` is the existing command fast-path supplied by commands.py. The
    planner never imports the command module, so there is no circular import.
    Any query, unresolved clause, or compound-looking clause is rejected and
    falls back to the normal LLM planner.
    """
    parts = _split_compound(command)

    if len(parts) < _MIN_STEPS:
        return None

    steps = []

    for part in parts[:_MAX_STEPS]:
        result = resolve(part)

        if not result or result.get("kind") != "action":
            return None

        steps.append({
            "command": part,
            "purpose": "execute the requested action",
        })

    if len(steps) > _MAX_STEPS:
        return None

    return {
        "is_compound": True,
        "summary": "I'll handle those actions in order, sir.",
        "requires_confirmation": False,
        "steps": tuple(steps),
    }


def _normalise_plan(data):
    """Validate and clean a model-produced plan."""
    if not isinstance(data, dict):
        return None

    is_compound = bool(data.get("is_compound"))
    summary = str(data.get("summary") or "").strip()
    raw_steps = data.get("steps") or []

    if not is_compound:
        return {
            "is_compound": False,
            "summary": summary,
            "requires_confirmation": bool(data.get("requires_confirmation")),
            "steps": (),
        }

    if not isinstance(raw_steps, list):
        return None

    steps = []

    for raw in raw_steps[:_MAX_STEPS]:
        if not isinstance(raw, dict):
            return None

        command = " ".join(
            str(raw.get("command") or "").split()
        ).strip()
        purpose = " ".join(
            str(raw.get("purpose") or "").split()
        ).strip()

        if not command:
            return None

        steps.append({
            "command": command,
            "purpose": purpose,
        })

    if not _MIN_STEPS <= len(steps) <= _MAX_STEPS:
        return None

    return {
        "is_compound": True,
        "summary": summary or "I'll handle that in stages, sir.",
        "requires_confirmation": bool(data.get("requires_confirmation")),
        "steps": tuple(steps),
    }


def plan(command, context=None):
    """Return a validated compound plan, or None when planning is unsuitable."""
    if not should_plan(command):
        return None

    prompt = (
        "USER REQUEST:\n"
        f"{str(command or '').strip()}\n\n"
        "CURRENT JARVIS CONTEXT:\n"
        f"{str(context or '').strip()}"
    )

    try:
        content = providers.chat(
            [
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "compound_plan",
                    "schema": _SCHEMA,
                },
            },
            temperature=0,
            max_tokens=1800,
            reasoning_effort="low",
        )
    except Exception as error:
        print(f"[JARVIS] compound planner failed: {error}")
        return None

    try:
        data = json.loads(content or "")
    except (ValueError, TypeError):
        print("[JARVIS] compound planner returned invalid JSON")
        return None

    plan_data = _normalise_plan(data)

    if not plan_data or not plan_data["is_compound"]:
        return None

    return plan_data


def execute(steps, dispatch):
    """Execute ordinary one-command steps through JARVIS's normal dispatcher.

    Steps intentionally go back through the normal command path rather than
    duplicating action implementations. A compound plan therefore inherits
    the same application lookup, Blender handling, journalling, safety, and
    error behaviour as an ordinary command.
    """
    for index, step in enumerate(steps, start=1):
        command = step["command"]

        print(
            f"[JARVIS] compound step {index}/{len(steps)}: {command}",
            flush=True,
        )

        result = dispatch(command)

        if not result:
            print(
                f"[JARVIS] compound step {index} produced no result",
                flush=True,
            )
            return False

        # Compound plans currently orchestrate actions. A query or pending
        # follow-up would need a conversation boundary rather than silent
        # execution inside the action worker, so reject it rather than
        # pretending it succeeded.
        if result.get("kind") != "action":
            print(
                f"[JARVIS] compound step {index} is not an action: "
                f"{result.get('intent')}",
                flush=True,
            )
            return False

        try:
            if not result["action"]():
                return False
        except Exception as error:
            print(
                f"[JARVIS] compound step {index} failed: {error}",
                flush=True,
            )
            return False

    return True
