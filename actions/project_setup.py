"""Data-driven project setup planner for JARVIS.

Phase 1 is deliberately planning-only.
It never creates files, starts processes, installs packages, or opens Cursor.
All stack-specific knowledge lives in project_setup_recipes.json.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import os

_RECIPE_FILE = Path(__file__).with_name("project_setup_recipes.json")


@dataclass(frozen=True)
class Recipe:
    name: str
    label: str
    aliases: tuple[str, ...]
    directory: str
    prerequisites: tuple[dict, ...]
    questions: tuple[dict, ...]
    defaults: dict
    steps: tuple[dict, ...]


def _normalise(value):
    return " ".join(str(value or "").casefold().split())


def _load():
    try:
        with _RECIPE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            f"Could not load project setup recipes: {error}"
        ) from error

    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Unsupported project setup recipe data.")

    recipes = data.get("recipes")

    if not isinstance(recipes, dict) or not recipes:
        raise ValueError("No project setup recipes are defined.")

    return recipes


def _recipe(name, raw):
    required = {
        "label",
        "aliases",
        "directory",
        "prerequisites",
        "questions",
        "defaults",
        "steps",
    }

    missing = required - raw.keys()

    if missing:
        raise ValueError(
            f"Recipe {name!r} is missing: {sorted(missing)}"
        )

    if not isinstance(raw["aliases"], list):
        raise ValueError(f"Recipe {name!r} aliases must be a list.")

    if not isinstance(raw["steps"], list) or not raw["steps"]:
        raise ValueError(f"Recipe {name!r} has no setup steps.")

    for step in raw["steps"]:
        if not isinstance(step, dict):
            raise ValueError(f"Recipe {name!r} has an invalid step.")

        if not step.get("name") or not isinstance(step.get("argv"), list):
            raise ValueError(f"Recipe {name!r} has an invalid step.")

    return Recipe(
        name=name,
        label=str(raw["label"]).strip(),
        aliases=tuple(
            _normalise(value)
            for value in raw["aliases"]
        ),
        directory=str(raw["directory"]).strip(),
        prerequisites=tuple(raw["prerequisites"]),
        questions=tuple(raw["questions"]),
        defaults=dict(raw["defaults"]),
        steps=tuple(raw["steps"]),
    )


def recipes():
    data = _load()

    return {
        name: _recipe(name, raw)
        for name, raw in data.items()
    }


def resolve(name):
    query = _normalise(name)

    if not query:
        return None

    for recipe in recipes().values():
        if query == _normalise(recipe.name):
            return recipe

        if query in recipe.aliases:
            return recipe

    return None


def resolve_many(names):
    selected = []
    seen = set()

    for name in names:
        recipe = resolve(name)

        if recipe is None:
            raise ValueError(
                f"No project setup recipe matches {name!r}."
            )

        if recipe.name in seen:
            continue

        seen.add(recipe.name)
        selected.append(recipe)

    if not selected:
        raise ValueError(
            "At least one project setup recipe is required."
        )

    return tuple(selected)


def check_prerequisites(names):
    """Check every executable declared by the selected recipes."""
    selected = resolve_many(names)
    results = []

    for recipe in selected:
        for prerequisite in recipe.prerequisites:
            executable = str(
                prerequisite.get("executable") or ""
            ).strip()

            args = [
                str(value)
                for value in prerequisite.get("args") or ()
            ]

            if not executable:
                results.append(
                    {
                        "recipe": recipe.name,
                        "executable": "",
                        "available": False,
                        "reason": "Recipe prerequisite has no executable.",
                    }
                )
                continue

            path = shutil.which(executable)

            if not path:
                results.append(
                    {
                        "recipe": recipe.name,
                        "executable": executable,
                        "available": False,
                        "reason": "Executable not found.",
                    }
                )
                continue

            try:
                result = subprocess.run(
                    [path, *args],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except (
                OSError,
                subprocess.SubprocessError,
            ) as error:
                results.append(
                    {
                        "recipe": recipe.name,
                        "executable": executable,
                        "available": False,
                        "reason": str(error),
                    }
                )
                continue

            output = (
                result.stdout.strip()
                or result.stderr.strip()
            )

            results.append(
                {
                    "recipe": recipe.name,
                    "executable": executable,
                    "path": path,
                    "available": result.returncode == 0,
                    "reason": (
                        output
                        if result.returncode != 0
                        else None
                    ),
                    "version": output or None,
                }
            )

    return tuple(results)


def resolve_project_dir(base_dir, project_name):
    """Resolve a project name beneath the configured projects directory."""
    name = str(project_name or "").strip()

    if not name:
        raise ValueError("A project name is required.")

    if name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError(
            "Project name must be a single folder name."
        )

    root = Path(base_dir).resolve()
    target = (root / name).resolve()

    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError(
            "Project name must stay inside the projects directory."
        ) from error

    return target


def setup_request(
    names,
    base_dir,
    project_name,
    use_defaults=None,
    answers=None,
):
    """Assess a setup request and resolve the default-choice decision."""
    selected = resolve_many(names)
    project_path = resolve_project_dir(
        base_dir,
        project_name,
    )

    prerequisite_results = check_prerequisites(names)

    missing = tuple(
        result
        for result in prerequisite_results
        if not result["available"]
    )

    result = {
        "status": "blocked" if missing else "confirmation_required",
        "recipes": tuple(recipe.name for recipe in selected),
        "labels": tuple(recipe.label for recipe in selected),
        "project_name": str(project_name).strip(),
        "base_dir": str(Path(base_dir).resolve()),
        "project_dir": str(project_path),
        "prerequisites": prerequisite_results,
        "missing_prerequisites": missing,
        "prompt": None,
        "questions": (),
        "defaults": _defaults(selected),
        "plan": None,
    }

    if project_path.exists():
        result["status"] = "blocked"
        result["prompt"] = None
        result["project_conflict"] = True
        result["reason"] = (
            "A project already exists at the requested location."
        )
        return result

    result["project_conflict"] = False
    result["reason"] = None

    if missing:
        return result

    result["prompt"] = (
        "Shall I use the recommended defaults?"
    )

    if use_defaults is None:
        return result

    if use_defaults:
        result["status"] = "ready"
        result["plan"] = plan(
            names,
            project_path,
            answers=_defaults(selected),
        )
        return result

    required_questions = questions(
        [recipe.name for recipe in selected]
    )

    supplied = answers or {}

    missing_answers = tuple(
        question
        for question in required_questions
        if question["id"] not in supplied
    )

    if missing_answers:
        result["status"] = "questions_required"
        result["questions"] = missing_answers
        return result

    result["status"] = "ready"
    result["questions"] = required_questions
    result["plan"] = plan(
        names,
        project_path,
        answers=supplied,
    )

    return result


def questions(names):
    result = []
    seen = set()

    for recipe in resolve_many(names):
        for question in recipe.questions:
            if len(question.get("options") or ()) < 2:
                continue

            question_id = question["id"]

            if question_id in seen:
                continue

            seen.add(question_id)
            result.append(question)

    return tuple(result)


def _defaults(selected):
    values = {}

    for recipe in selected:
        values.update(
            {
                key: values.get(key, value)
                for key, value in recipe.defaults.items()
            }
        )

    for question in questions(
        [recipe.name for recipe in selected]
    ):
        values.setdefault(
            question["id"],
            question["default"],
        )

        selected_value = values[question["id"]]

        for option in question["options"]:
            if str(option["value"]) != str(selected_value):
                continue

            values.update(option.get("set") or {})
            break

    return values


def _normalise_argv_value(value):
    """Normalise absolute executable/file paths for the host OS."""
    text = str(value)

    if os.path.isabs(text):
        return os.path.normpath(text)

    return text


def plan(names, project_dir, answers=None):
    selected = resolve_many(names)

    values = _defaults(selected)
    values.update(answers or {})

    for question in questions(
        [recipe.name for recipe in selected]
    ):
        selected_value = values.get(question["id"])

        for option in question["options"]:
            if str(option["value"]) == str(selected_value):
                values.update(option.get("set") or {})
                break

    root = Path(project_dir).resolve()
    single_recipe = len(selected) == 1
    steps = []

    for recipe in selected:
        recipe_dir = (
            root
            if single_recipe
            else root / recipe.directory
        ).resolve()

        values["project_dir"] = str(root)
        values["recipe_dir"] = str(recipe_dir)

        for step in recipe.steps:
            try:
                argv = tuple(
                    _normalise_argv_value(
                        value.format_map(values)
                    )
                    for value in step["argv"]
                )
            except KeyError as error:
                raise ValueError(
                    f"Recipe {recipe.name!r} requires "
                    f"unknown value {error.args[0]!r}."
                ) from error

            steps.append(
                {
                    "recipe": recipe.name,
                    "name": step["name"],
                    "cwd": str(recipe_dir),
                    "argv": argv,
                }
            )

    return {
        "recipes": tuple(recipe.name for recipe in selected),
        "labels": tuple(recipe.label for recipe in selected),
        "project_dir": str(root),
        "defaults": values,
        "questions": questions(
            [recipe.name for recipe in selected]
        ),
        "prerequisites": tuple(
            prerequisite
            for recipe in selected
            for prerequisite in recipe.prerequisites
        ),
        "steps": tuple(steps),
    }


def execute(plan_data):
    """Execute an approved project setup plan safely."""
    if not isinstance(plan_data, dict):
        raise ValueError("A setup plan is required.")

    if plan_data.get("status") not in (None, "ready"):
        raise ValueError(
            "Only a ready setup plan can be executed."
        )

    project_dir = Path(
        plan_data["project_dir"]
    ).resolve()

    project_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    results = []

    for step in plan_data.get("steps") or plan_data.get("plan", {}).get("steps") or ():
        if not isinstance(step, dict):
            raise ValueError("Setup plan contains an invalid step.")

        argv = step.get("argv")

        if not isinstance(argv, (list, tuple)) or not argv:
            raise ValueError(
                f"Setup step {step.get('name', '<unknown>')!r} has no command."
            )

        cwd = Path(
            step.get("cwd") or project_dir
        ).resolve()

        cwd.mkdir(
            parents=True,
            exist_ok=True,
        )

        command = tuple(
            str(value)
            for value in argv
        )

        resolved_executable = shutil.which(command[0])

        if resolved_executable:
            suffix = Path(resolved_executable).suffix.casefold()

            if suffix in {".cmd", ".bat"}:
                command = (
                    os.environ.get("COMSPEC", "cmd.exe"),
                    "/d",
                    "/c",
                    resolved_executable,
                    *command[1:],
                )
            else:
                command = (
                    resolved_executable,
                    *command[1:],
                )

        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        result = {
            "name": step.get("name"),
            "recipe": step.get("recipe"),
            "cwd": str(cwd),
            "argv": tuple(str(value) for value in argv),
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "succeeded": completed.returncode == 0,
        }

        results.append(result)

        if completed.returncode != 0:
            return {
                "status": "failed",
                "project_dir": str(project_dir),
                "steps": tuple(results),
                "failed_step": result,
            }

    return {
        "status": "completed",
        "project_dir": str(project_dir),
        "steps": tuple(results),
        "failed_step": None,
    }
