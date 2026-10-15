"""Write one JARVIS Signal Desk briefing across every market we can read, now.

    python tools/signal_desk.py

Nothing is traded. Nothing is sent anywhere. It writes a plain-text
briefing and, for each market, a 4-hour chart and a 15-minute chart into
~/JARVIS/signal-desk/, and prints the briefing so you can read it here.
While JARVIS runs it writes one by itself after every 4-hour close
(actions/signal_watch.py); this is the same briefing, on demand.
"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# The OANDA token lives in .env, as it does for JARVIS itself.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


from actions import signal_desk  # noqa: E402


def main():
    results = signal_desk.scan()

    for name, found in results:
        print(f"{name}:  {found['verdict'].upper() if found else 'could not be read'}")

    path, text = signal_desk.write(results)
    print()
    print(text)
    print(f"Saved to {path}")


if __name__ == "__main__":
    main()
