"""Data-driven project setup planner for JARVIS.

Phase 1 is deliberately planning-only.
It never creates files, starts processes, installs packages, or opens Cursor.
All stack-specific knowledge lives in project_setup_recipes.json.
"""

from dataclasses import dataclass
import json
from pathlib import Path


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


def questions(names):
    result = []
    seen = set()

    for recipe in resolve_many(names):
        for question in recipe.questions:
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
                    value.format_map(values)
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
