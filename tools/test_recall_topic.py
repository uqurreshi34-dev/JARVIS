""""What do you know about football?" answers about football, or says it can't.

It used to read the general summary -- name, home coordinates, default
project -- whatever the topic. Checked:

- a topic gets only the memories relevant to it, by meaning;
- a topic nothing is about gets a plain "I don't know anything about ...";
- no topic, or "me", still gets the general summary;
- the local model is found in the cache before the network is tried.

Runs in a sandboxed JARVIS folder, never your real memory.

    python tools/test_recall_topic.py
"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

from actions import memory, semantic_memory  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


with open(memory._path(), "w", encoding="utf-8") as handle:
    handle.write(
        "name: avid coder\n"
        "home latitude: 48.8566\n"
        "home longitude: 2.3522\n"
        "default project: c sharp\n"
        "gym days: monday and friday\n"
        "I support Arsenal\n"
    )

semantic_memory.install()
semantic_memory.clear_cache()

said = memory.describe_about("football")
check("latitude" not in said and "name" not in said, f"an unrelated topic lists nothing else: {said!r}")
check(said == "I don't know anything about football, sir.", "and says so plainly")

check(memory.describe_about("my car") == "I don't know anything about your car, sir.",
      "'my' is said back as 'your'")

said = memory.describe_about("my gym")
check(said == "Your gym days are monday and friday, sir.", f"a keyed memory is said as a sentence: {said!r}")

said = memory.describe_about("arsenal")
check("I support Arsenal" in said and "latitude" not in said, f"a loose memory is said as remembered: {said!r}")

said = memory.describe_about("my default project")
check(said == "Your default project is c sharp, sir.", f"one value takes 'is': {said!r}")

for general in ("", "me", "myself"):
    check(memory.describe_about(general) == memory.describe(), f"{general!r} still gets the general summary")

# ---- the model comes from the cache first -----------------------------------

calls = []


def cached(repo_id, filename, local_files_only=False):
    calls.append(local_files_only)
    return f"/cache/{filename}"


check(semantic_memory._model_file(cached, "x.onnx") == "/cache/x.onnx" and calls == [True],
      "a cached model is used without asking the network")

calls.clear()


def missing(repo_id, filename, local_files_only=False):
    calls.append(local_files_only)
    if local_files_only:
        raise FileNotFoundError(filename)
    return f"/downloaded/{filename}"


check(semantic_memory._model_file(missing, "x.onnx") == "/downloaded/x.onnx" and calls == [True, False],
      "a missing model is downloaded once")

sys.exit(1 if failures else 0)
