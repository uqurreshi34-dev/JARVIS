"""Measure what the real encoder actually scores, before choosing a number.

_MIN_ATTRIBUTE_SCORE decides when a stored fact answers a question
instead of the model. It is currently 0.55, an absolute cosine, and on
this machine "fuel economy" against "Returns around 55 mpg combined on
diesel" does not clear it -- which is why those tests fail.

The fix is not to nudge 0.55 down until the tests pass. That just moves
the failure somewhere quieter. This prints the scores so the threshold
can be read off real numbers, including the cases where JARVIS should
decline and let the model answer.

Run it on a machine where the ONNX encoder loads:

    python tools\\probe_attribute_scores.py
"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from actions import semantic_memory  # noqa: E402


_SUBJECTS = {
    "BMW 3 Series": [
        "Returns around 55 mpg combined on diesel.",
        "Boot space is 480 litres in the saloon.",
        "Seats five adults on long motorway journeys.",
    ],
    "Dune": [
        "Frank Herbert published it in 1965.",
        "It won both the Hugo and the Nebula awards.",
    ],
}

# (subject, spoken attribute, the fact that should win, or None when
# nothing stored answers it and JARVIS should stay quiet).
_CASES = (
    ("BMW 3 Series", "fuel economy", 0),
    ("BMW 3 Series", "boot space", 1),
    ("BMW 3 Series", "how many passengers", 2),
    ("BMW 3 Series", "mpg", 0),
    ("Dune", "wrote", 0),
    ("Dune", "who is the author", 0),
    ("Dune", "awards", 1),
    # Nothing stored answers these. A threshold that admits them is too
    # low, whatever it does for the cases above.
    ("BMW 3 Series", "insurance group", None),
    ("BMW 3 Series", "tyre pressure", None),
    ("Dune", "film adaptation budget", None),
    ("Dune", "how many pages", None),
)


def _scores(attribute, facts):
    vectors = semantic_memory._encode([attribute] + list(facts))

    if vectors is None or len(vectors) < 2:
        return None

    query = vectors[0]

    return [float(vector @ query) for vector in vectors[1:]]


def main():
    if not semantic_memory._load_model():
        print("The encoder did not load, so there is nothing to measure.")
        return 1

    should_answer = []
    should_decline = []

    print(f"{'subject':14} {'attribute':24} {'best':>6} {'2nd':>6} "
          f"{'margin':>7}  verdict")
    print("-" * 78)

    for subject, attribute, wanted in _CASES:
        facts = _SUBJECTS[subject]
        scores = _scores(attribute, facts)

        if scores is None:
            print(f"could not encode {attribute!r}")
            continue

        order = sorted(range(len(scores)), key=lambda i: scores[i],
                       reverse=True)
        best = scores[order[0]]
        second = scores[order[1]] if len(order) > 1 else 0.0
        margin = best - second

        if wanted is None:
            verdict = "should DECLINE"
            should_decline.append((best, margin))
        elif order[0] == wanted:
            verdict = "right fact on top"
            should_answer.append((best, margin))
        else:
            verdict = f"WRONG fact on top ({facts[order[0]][:28]!r})"
            should_answer.append((best, margin))

        print(f"{subject:14} {attribute:24} {best:6.3f} {second:6.3f} "
              f"{margin:7.3f}  {verdict}")

    print()

    if should_answer and should_decline:
        need = min(score for score, _margin in should_answer)
        allow = max(score for score, _margin in should_decline)

        print(
            f"lowest score among questions that SHOULD be answered: {need:.3f}")
        print(
            f"highest score among questions that should DECLINE:    {allow:.3f}")

        if need > allow:
            print(
                f"\nAn absolute floor works. Anything between {allow:.3f} and "
                f"{need:.3f} separates them; the midpoint is "
                f"{(need + allow) / 2:.3f}."
            )
        else:
            print(
                "\nNo absolute floor separates them: a question with no "
                "stored answer scores as high as one with a good answer. "
                "The threshold needs the margin as well as the score."
            )

            need_margin = min(margin for _score, margin in should_answer)
            allow_margin = max(margin for _score, margin in should_decline)

            print(f"lowest margin when it should answer: {need_margin:.3f}")
            print(f"highest margin when it should decline: {allow_margin:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
