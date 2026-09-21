"""What JARVIS has actually been asked, grouped by what he decided.

Every command JARVIS resolves is already written to the journal by
journal.command, with the spoken words, the intent he settled on, and
whether the fast path caught it or the model was asked. That is the
whole raw material for two separate jobs, and neither needs new logging.

The first is knowing which intents depend on a model at all. An intent
that is always resolved locally cannot be misrouted by a weaker model,
however bad that model is. An intent that always goes to the model is
entirely at its mercy.

The second is a golden set. The utterances here are real -- what was
said, in the words it was said in -- which is worth far more than
invented phrasings when the question is whether a smaller model can
still route them.

Read-only, offline, and it imports nothing from JARVIS so it can be run
against a copied log anywhere.
"""

import argparse
import ast
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


# "2026-09-21 19:06:24  command          'what is it' -> answer_question (model)"
_LINE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\s+command\s+(?P<rest>.+)$"
)

# The spoken text is written with !r, so it is a Python literal and may be
# single or double quoted depending on what it contains. Everything after
# it is ours: the intent, an optional [subject], and the route.
_PARTS = re.compile(
    r"^(?P<spoken>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
    r"\s*->\s*(?P<intent>\S+)"
    r"(?:\s*\[(?P<detail>.*)\])?"
    r"\s*\((?P<route>local|model)\)\s*$"
)


def _logs(folder):
    """The live log first, then archives oldest to newest."""
    live = folder / "jarvis-log.txt"
    archives = sorted(folder.glob("jarvis-log-*.txt"))

    return [path for path in [*archives, live] if path.exists()]


def _parse(path):
    """Yield (date, spoken, intent, route) for every command line."""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = _LINE.match(raw)

        if not line:
            continue

        parts = _PARTS.match(line.group("rest").strip())

        if not parts:
            continue

        try:
            spoken = ast.literal_eval(parts.group("spoken"))
        except (ValueError, SyntaxError):
            continue

        if not isinstance(spoken, str) or not spoken.strip():
            continue

        yield (
            line.group("date"),
            spoken.strip(),
            parts.group("intent"),
            parts.group("route"),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder",
        default=str(Path.home() / "JARVIS"),
        help="folder holding jarvis-log.txt (default: ~/JARVIS)",
    )
    parser.add_argument(
        "--out",
        help="write the model-routed utterances to this JSON file",
    )
    parser.add_argument(
        "--since",
        help=(
            "ignore anything before this date, YYYY-MM-DD. A routing fix "
            "only shows up as an absence after the day it landed."
        ),
    )
    parser.add_argument(
        "--show",
        type=int,
        default=6,
        help="how many example utterances to print per intent (default 6)",
    )
    args = parser.parse_args()

    folder = Path(args.folder).expanduser()
    files = _logs(folder)

    if not files:
        print(f"No journal found in {folder}")
        return 1

    routes = Counter()
    by_intent = defaultdict(Counter)
    last_seen = {}
    skipped = 0

    for path in files:
        for date, spoken, intent, route in _parse(path):
            if args.since and date < args.since:
                skipped += 1
                continue

            routes[route] += 1
            by_intent[intent][(spoken, route)] += 1

            key = (intent, spoken, route)
            last_seen[key] = max(last_seen.get(key, date), date)

    total = sum(routes.values())

    if not total:
        print(f"Read {len(files)} file(s) in {folder}, found no command lines.")
        return 1

    print(f"{len(files)} file(s) in {folder}")

    if args.since:
        print(f"ignoring {skipped} command(s) before {args.since}")

    print(
        f"{total} commands: {routes['local']} local, {routes['model']} model"
    )
    print()

    # Model-heavy intents first: those are the ones a weaker model can
    # ruin, and the ones a deterministic route would most repay.
    def model_share(intent):
        counts = by_intent[intent]
        asked = sum(n for (_, route), n in counts.items() if route == "model")

        return asked, sum(counts.values())

    ranked = sorted(
        by_intent,
        key=lambda intent: (-model_share(intent)[0], intent),
    )

    print(f"{'intent':<28} {'model':>6} {'local':>6}   examples")
    print("-" * 78)

    exported = {}

    for intent in ranked:
        asked, seen = model_share(intent)
        said = [
            spoken for (spoken, route), _ in by_intent[intent].most_common()
            if route == "model"
        ]

        print(f"{intent:<28} {asked:>6} {seen - asked:>6}", end="")

        if said:
            exported[intent] = said

            for position, spoken in enumerate(said[:args.show]):
                prefix = "   " if position == 0 else " " * 45
                when = last_seen.get((intent, spoken, "model"), "?")
                print(f"{prefix}{when}  {spoken!r}")

            if len(said) > args.show:
                print(" " * 45 + f"... and {len(said) - args.show} more")
        else:
            print("   (always resolved locally)")

    unique = sum(len(said) for said in exported.values())

    print()
    print(
        f"{len(exported)} intent(s) reached the model, "
        f"{unique} distinct utterance(s) between them."
    )

    if args.out:
        Path(args.out).write_text(
            json.dumps(exported, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Written to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
