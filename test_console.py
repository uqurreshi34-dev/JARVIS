import time

from commands import _application_manager, handle_command


def run_direct(line):
    verb, _, name = line.partition(" ")
    name = name.strip()

    if not name:
        print("  direct mode: use '!open <name>' or '!close <name>'")
        return

    if verb == "open":
        ok = _application_manager.launch(name)
    elif verb == "close":
        ok = _application_manager.close(name)
    else:
        print("  direct mode: use '!open <name>' or '!close <name>'")
        return

    print(f"  -> {'success' if ok else 'failed'}")


print("JARVIS console.")
print("  Full pipeline (LLM + manager):  open outlook")
print("  Bypass the LLM (manager only):  !close outlook")
print("  Quit:                           quit")
print()

while True:
    try:
        line = input("You: ").strip()
    except (EOFError, KeyboardInterrupt):
        break

    if not line:
        continue

    if line.lower() == "quit":
        break

    if line.startswith("!"):
        run_direct(line[1:].strip())
        continue

    result = handle_command(line)

    if not result:
        print("JARVIS: I don't know how to do that yet.")
        continue

    print(f"JARVIS: {result['response']}")

    start = time.monotonic()
    ok = result["action"]()
    elapsed = time.monotonic() - start

    print(f"  -> {'success' if ok else 'failed'} ({elapsed:.1f}s)")
