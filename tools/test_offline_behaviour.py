"""Offline regression tests for local routing, speech fallback and joining.

Runs with the network unplugged. No microphone, no provider, no application
is launched: handle_command only resolves an intent and returns the callable
that would perform it.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import commands  # noqa: E402
import speech  # noqa: E402


def _check_local_stays_local(failures):
    """A locally resolved command must not build planner context."""
    with (
        patch.object(commands, "_planner_context") as context_mock,
        patch.object(
            commands._interpreter,
            "interpret",
        ) as interpret_mock,
    ):
        result = commands.handle_command("open netflix", probe=True)

    if not result:
        failures.append("'open netflix' did not resolve at all")
    elif result.get("intent") != "open_application":
        failures.append(
            f"'open netflix' resolved to {result.get('intent')!r}"
        )

    if context_mock.called:
        failures.append(
            "planner context was built for a single local command; "
            "this is the Blender health check that made commands slow"
        )

    if interpret_mock.called:
        failures.append(
            "the model was called for a command the fast path resolved"
        )


def _check_model_is_the_fallback(failures):
    """Speech the local path cannot resolve must reach the model."""
    stand_in = commands._blank_result("get_system_status")

    with (
        patch.object(commands, "_planner_context", return_value=""),
        patch.object(
            commands._interpreter,
            "interpret",
            return_value=stand_in,
        ) as interpret_mock,
    ):
        commands.handle_command("zzxq wibble frotz plinth", probe=True)

    if not interpret_mock.called:
        failures.append(
            "unresolvable speech never reached the model; "
            "garbled commands will answer 'that eluded me'"
        )


def _check_compound_still_plans(failures):
    """A genuine compound request must still reach the planner."""
    with patch.object(
        commands._interpreter,
        "interpret",
    ) as interpret_mock:
        result = commands.handle_command(
            "open calculator and open notepad",
            probe=True,
        )

    if not result:
        failures.append("compound command did not resolve")
        return

    if result.get("intent") != "compound_task":
        failures.append(
            f"compound command resolved to {result.get('intent')!r} "
            "instead of a plan"
        )

    if interpret_mock.called:
        failures.append(
            "a locally planned compound command still called the model"
        )

    # The join is the cosmetic fix: every fragment after the first must
    # start lowercase. Asserting the shape rather than the exact wording,
    # so rephrasing the phrasebook does not break the test.
    response = result.get("response") or ""

    if " and " in response:
        tail = response.split(" and ", 1)[1].lstrip()

        if tail[:1].isupper() and not tail[:2].isupper():
            failures.append(
                f"joined reply restarts with a capital: {response!r}"
            )


def _check_trailing_sir(failures):
    """Stripping the address must match the form, not two spellings."""
    cases = (
        ("Bringing up Netflix, sir.", "Bringing up Netflix"),
        ("Closing Netflix now, sir.", "Closing Netflix now"),
        ("Right away, sir!", "Right away"),
        ("Done, Sir.", "Done"),
        ("Opening Netflix sir", "Opening Netflix"),
        ("Saved sir.txt, sir.", "Saved sir.txt"),
        ("Opening the Sir Alex file, sir.", "Opening the Sir Alex file"),
        ("Done.", "Done."),
        ("sir.", ""),
    )

    for source, expected in cases:
        actual = commands._TRAILING_SIR.sub("", source).strip()

        if actual != expected:
            failures.append(
                f"stripping {source!r} gave {actual!r}, "
                f"expected {expected!r}"
            )


def _check_join(failures):
    steps = [
        {"result": {"response": "Calculator, coming up, sir."}},
        {"result": {"response": "Bringing up Notepad, sir."}},
    ]

    actual = commands._compound_response(steps, "fallback")
    expected = "Calculator, coming up and bringing up Notepad, sir."

    if actual != expected:
        failures.append(f"joined reply was {actual!r}")


def _check_speech_survives_a_hang(failures):
    """A neural voice that hangs must be abandoned, not waited on."""
    import time as clock

    engine = speech.speech
    speech._neural_failed_at = 0.0

    def never_returns(*_args, **_kwargs):
        clock.sleep(60)

    with (
        patch.object(engine, "_audio_for", side_effect=never_returns),
        patch.object(engine, "_speak_fallback") as fallback_mock,
    ):
        started = clock.monotonic()
        engine.speak("a phrase whose synthesis never completes")
        elapsed = clock.monotonic() - started

    if elapsed > speech._NEURAL_SYNTHESIS_DEADLINE + 3:
        failures.append(
            f"a hanging synthesis blocked speech for {elapsed:.0f}s"
        )

    if not fallback_mock.called:
        failures.append("a hanging synthesis never reached the fallback")

    if speech._speaking.is_set():
        failures.append("the speaking flag was left set after a hang")

    speech._neural_failed_at = 0.0


def _check_speech_falls_back(failures):
    """A failed neural voice must fall back and release the speaking flag."""
    engine = speech.speech
    speech._neural_failed_at = 0.0

    def unreachable(_text):
        raise ConnectionError("offline")

    with (
        patch.object(engine, "_speak_neural", side_effect=unreachable),
        patch.object(engine, "_speak_fallback") as fallback_mock,
    ):
        engine.speak("a phrase that is certainly not cached")

        if not fallback_mock.called:
            failures.append(
                "a failed neural voice did not fall back to pyttsx3"
            )

        if speech._speaking.is_set():
            failures.append(
                "the speaking flag was left set; the HUD would stay stuck"
            )

        # The cooldown should stop the next uncached phrase paying the
        # connect timeout all over again.
        with patch.object(
            engine,
            "_speak_neural",
            side_effect=unreachable,
        ) as neural_mock:
            engine.speak("a second phrase, also not cached")

            if neural_mock.called:
                failures.append(
                    "the neural voice was retried inside the cooldown; "
                    "every offline reply will stall on the timeout"
                )

    speech._neural_failed_at = 0.0


def _check_synthesis_timeouts(failures):
    """edge_tts must be given bounded timeouts, not its 10s/60s defaults."""
    seen = {}

    def record(*args, **kwargs):
        seen.update(kwargs)
        raise ConnectionError("offline")

    directory = tempfile.mkdtemp(prefix="jarvis-tts-test-")
    path = os.path.join(directory, "phrase.mp3")
    sidecar = os.path.join(directory, "phrase.txt")

    with patch.object(speech.edge_tts, "Communicate", side_effect=record):
        try:
            speech.speech._synthesize_to_cache("hello", path, sidecar)
        except Exception:
            pass

    connect = seen.get("connect_timeout")
    receive = seen.get("receive_timeout")

    if connect is None or receive is None:
        failures.append(
            "edge_tts was called without explicit timeouts; offline "
            "replies will hang for up to 70 seconds"
        )
        return

    if connect > 8:
        failures.append(
            f"connect_timeout is {connect}s, too long to stall a reply"
        )

    if receive > 30:
        failures.append(f"receive_timeout is {receive}s, too long")


def _check_no_stacked_syntheses(failures):
    """A stuck synthesis must not be followed by a second one behind it."""
    import time as clock

    engine = speech.speech
    speech._neural_failed_at = 0.0
    speech._neural_busy.clear()

    attempts = []

    def never_returns(*_args, **_kwargs):
        attempts.append(1)
        clock.sleep(60)

    with (
        patch.object(engine, "_audio_for", side_effect=never_returns),
        patch.object(engine, "_speak_fallback"),
    ):
        engine.speak("first uncached phrase")

        # Past the cooldown, so a second attempt is allowed to try again.
        speech._neural_failed_at = 0.0

        started = clock.monotonic()
        engine.speak("second uncached phrase")
        elapsed = clock.monotonic() - started

    if len(attempts) > 1:
        failures.append(
            f"{len(attempts)} syntheses were started; the second stacked "
            "up behind a thread that was still holding the cache lock"
        )

    if elapsed > 2:
        failures.append(
            f"the second phrase waited {elapsed:.0f}s behind a stuck "
            "synthesis instead of falling back immediately"
        )

    speech._neural_failed_at = 0.0
    speech._neural_busy.clear()


def _check_cached_phrase_keeps_the_voice(failures):
    """A cached phrase needs no network, so the cooldown must not apply."""
    import time as clock

    engine = speech.speech

    # Mid-cooldown: a failure just happened.
    speech._neural_failed_at = clock.monotonic()
    speech._neural_busy.clear()

    with (
        patch.object(engine, "_is_cached", return_value=True),
        patch.object(engine, "_speak_neural") as neural_mock,
        patch.object(engine, "_speak_fallback"),
    ):
        engine.speak("a phrase that is already on disk")

        if not neural_mock.called:
            failures.append(
                "a cached phrase used the fallback voice during the "
                "cooldown, even though it needs no network"
            )

    speech._neural_failed_at = clock.monotonic()

    with (
        patch.object(engine, "_is_cached", return_value=False),
        patch.object(engine, "_speak_neural") as neural_mock,
        patch.object(engine, "_speak_fallback") as fallback_mock,
    ):
        engine.speak("a phrase that is not on disk")

        if neural_mock.called:
            failures.append(
                "an uncached phrase retried the network inside the cooldown"
            )

        if not fallback_mock.called:
            failures.append("an uncached phrase did not fall back")

    # A stuck synthesis holds the cache lock, so even a cached phrase has
    # to take the fallback rather than block behind it.
    speech._neural_failed_at = 0.0
    speech._neural_busy.set()

    with (
        patch.object(engine, "_is_cached", return_value=True),
        patch.object(engine, "_speak_neural") as neural_mock,
        patch.object(engine, "_speak_fallback") as fallback_mock,
    ):
        engine.speak("a cached phrase during a stuck synthesis")

        if neural_mock.called:
            failures.append(
                "a cached phrase tried to use the cache while a stuck "
                "synthesis was holding its lock"
            )

        if not fallback_mock.called:
            failures.append(
                "a cached phrase during a stuck synthesis did not fall back"
            )

    speech._neural_failed_at = 0.0
    speech._neural_busy.clear()


def main():
    failures = []

    _check_local_stays_local(failures)
    _check_model_is_the_fallback(failures)
    _check_compound_still_plans(failures)
    _check_trailing_sir(failures)
    _check_join(failures)
    _check_speech_falls_back(failures)
    _check_synthesis_timeouts(failures)
    _check_speech_survives_a_hang(failures)
    _check_no_stacked_syntheses(failures)
    _check_cached_phrase_keeps_the_voice(failures)

    if failures:
        print("FAILED")

        for failure in failures:
            print(f"- {failure}")

        return 1

    print(
        "PASSED: local commands stay local, the model is the fallback, "
        "and an offline voice degrades instead of hanging."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
