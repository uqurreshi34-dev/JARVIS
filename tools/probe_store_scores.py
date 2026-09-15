"""Score real questions against what is really stored, not fixtures.

probe_attribute_scores.py measured short example facts. The facts in
subjects.txt are several times longer, and a two-word question scores
lower against a long sentence than against a short one. So a threshold
tuned on fixtures can be wrong for the real store, which is what appears
to be happening with the BMW questions.

This reads subjects.txt, resolves each question the way JARVIS does, and
prints the score of every fact for that subject. Nothing is hardcoded:
the subjects, the facts and the threshold all come from the running
code, so it stays honest as the store grows.

    python tools\\probe_store_scores.py
    python tools\\probe_store_scores.py "bmw fuel economy" "toyota hybrids"

With no questions given, it builds a set from whatever is stored.
"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from actions import memory_subjects, semantic_memory, subject_store  # noqa: E402


# Attributes worth asking about, used only to build default questions when
# none are supplied. Each is tried against every stored subject; the ones
# that turn out to have no answer are exactly as informative as the ones
# that do.
_DEFAULT_ATTRIBUTES = (
    "fuel economy",
    "boot space",
    "how many seats",
    "engine",
    "electric range",
    "history",
    "tyre pressure",
    "insurance group",
)


def _questions():
    if len(sys.argv) > 1:
        return list(sys.argv[1:])

    built = []

    for label in subject_store.subjects():
        for attribute in _DEFAULT_ATTRIBUTES:
            built.append(f"{label} {attribute}")

    return built


def _scores(attribute, facts):
    vectors = semantic_memory._encode([attribute] + list(facts))

    if vectors is None or len(vectors) < 2:
        return None

    query = vectors[0]

    return [float(vector @ query) for vector in vectors[1:]]


def main():
    if not subject_store.subjects():
        print("subjects.txt is empty; nothing to probe.")
        return 1

    if not semantic_memory._load_model():
        print("The encoder did not load, so there is nothing to measure.")
        return 1

    floor = memory_subjects._MIN_ATTRIBUTE_SCORE

    print(f"threshold in code: {floor}")
    print(f"stored: {subject_store.line_count()} facts across "
          f"{len(subject_store.subjects())} subject(s)\n")

    answered = []
    declined = []

    for question in _questions():
        corrected = memory_subjects.correct(question)
        resolved = memory_subjects.resolve(corrected)

        if not resolved:
            print(f"{question!r}\n   no subject resolved\n")
            continue

        label = resolved[0]
        attribute = memory_subjects._remaining_tokens(corrected, label)
        facts = subject_store.facts_for(label)

        if not attribute or not facts:
            print(f"{question!r}\n   resolved {label!r}, "
                  f"attribute={attribute}, {len(facts)} stored facts\n")
            continue

        scores = _scores(" ".join(attribute), facts)

        if scores is None:
            print(f"{question!r}\n   could not encode\n")
            continue

        order = sorted(range(len(scores)), key=lambda i: scores[i],
                       reverse=True)
        best = scores[order[0]]
        verdict = "ANSWERS" if best >= floor else "declines"

        (answered if best >= floor else declined).append((best, question))

        print(f"{question!r}  ->  {label}  [{verdict} at {floor}]")

        for rank, index in enumerate(order[:3]):
            mark = "*" if scores[index] >= floor else " "
            print(f"   {mark} {scores[index]:.3f}  "
                  f"{facts[index][:78]}")

        if len(order) > 3:
            print(f"     ...  {len(order) - 3} lower-scoring fact(s), "
                  f"floor {scores[order[-1]]:.3f}")

        print(f"   length: query {len(' '.join(attribute))} chars, "
              f"best fact {len(facts[order[0]])} chars")
        print()

    print("-" * 70)

    if answered:
        print(f"{len(answered)} question(s) answer, "
              f"scores {min(s for s, _ in answered):.3f} to "
              f"{max(s for s, _ in answered):.3f}")

    if declined:
        print(f"{len(declined)} question(s) decline, "
              f"scores {min(s for s, _ in declined):.3f} to "
              f"{max(s for s, _ in declined):.3f}")

        print("\nHighest-scoring questions that still decline:")

        for score, question in sorted(declined, reverse=True)[:5]:
            print(f"   {score:.3f}  {question}")

    print(
        "\nPick the threshold from this, not from the fixtures: it wants to "
        "sit above the questions nothing stored answers, and below the ones "
        "that should."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
