"""Pretend to be ESP32 boards, to see the sensor panel before the real ones.

Posts to JARVIS exactly as a board does -- over HTTPS to /sensor, with the
phone token, trusting only JARVIS's own certificate authority, and by the
same address the board would use -- so a run through here exercises the
whole path: the certificate check, the token, sensors.py's decisions, the
spoken announcement and the HUD's sensor panel.

Start JARVIS first, then, from the JARVIS folder:

    python tools/sensor_simulator.py                   one board, "test"
    python tools/sensor_simulator.py room kitchen      two boards, named so
    python tools/sensor_simulator.py --every 5 --for 60

Each board says it is online, then sends temperature and humidity every
few seconds (a real board: every 30) and, now and then, motion. Stop it
with Ctrl+C: the boards go quiet, and the panel slides away once they
have been silent for two minutes, as it would for real boards.

The token is read from .env and never printed.
"""

import argparse
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import esp32_setup  # noqa: E402  (JARVIS's settings, without starting JARVIS)


class Board:
    """One pretend board: readings that wander the way a room's do."""

    def __init__(self, name, index, motion_every):
        self.name = name
        self.temperature = 20.5 + 2.4 * index
        self.humidity = 45.0 + 6.0 * index
        self.motion_every = motion_every

    def readings(self):
        self.temperature += random.uniform(-0.15, 0.2)
        self.humidity = min(95.0, max(15.0, self.humidity + random.uniform(-1.0, 1.0)))
        return {"name": self.name, "temperature": round(self.temperature, 1), "humidity": round(self.humidity)}

    def moved(self, seconds):
        """Whether someone walked past in the last [seconds]."""
        return self.motion_every > 0 and random.random() < seconds / self.motion_every


def connection(ca_path):
    """TLS that trusts JARVIS's authority and nothing else, as the board does."""
    with open(ca_path, "rb") as handle:
        raw = handle.read()

    if raw.lstrip().startswith(b"-----"):
        return ssl.create_default_context(cadata=raw.decode("ascii"))

    return ssl.create_default_context(cadata=raw)


def post(url, token, context, payload):
    """Send one report. Returns JARVIS's answer, or raises with what to do."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Jarvis-Token": token},
        method="POST",
    )

    # Straight to JARVIS, as a board on the wifi would: never through a
    # proxy set for the internet.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=context))

    try:
        with opener.open(request, timeout=8) as answer:
            return json.loads(answer.read().decode("utf-8") or "{}")

    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise SystemExit("JARVIS refused the token. Is JARVIS_PHONE_TOKEN in .env the one JARVIS started with?")
        raise SystemExit(f"JARVIS answered {error.code}.")

    except urllib.error.URLError as error:
        reason = error.reason

        if isinstance(reason, ssl.SSLCertVerificationError):
            raise SystemExit(f"JARVIS's certificate was not accepted ({reason.verify_message}). "
                             "Start JARVIS once so it renews it, then try again.")

        raise SystemExit(f"Could not reach JARVIS at {url} ({reason}). Is JARVIS running?")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pretend ESP32 sensor boards, for the HUD's sensor panel.")
    parser.add_argument("names", nargs="*", default=["test"], help="board names (default: test)")
    parser.add_argument("--every", type=float, default=5.0, help="seconds between readings (default 5)")
    parser.add_argument("--for", dest="duration", type=float, default=0.0, help="stop after this many seconds")
    parser.add_argument("--motion", type=float, default=25.0,
                        help="motion about this often, in seconds; 0 for none (default 25)")
    parser.add_argument("--host", help="JARVIS's address (default: as tools/esp32_setup.py finds it)")
    options = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    token = os.getenv(esp32_setup.TOKEN_ENV)

    if not token:
        print(f"{esp32_setup.TOKEN_ENV} is not set in .env; JARVIS would not know the boards.")
        return 1

    if not esp32_setup.CA_FILE.exists():
        print("JARVIS's certificates are not there yet. Start JARVIS once, then try again.")
        return 1

    host = options.host or os.getenv("JARVIS_SENSOR_HOST") or esp32_setup.local_address()
    port = int(os.getenv(esp32_setup.PORT_ENV) or esp32_setup.DEFAULT_PORT)
    url = f"https://{host}:{port}/sensor"
    context = connection(esp32_setup.CA_FILE)
    every = max(1.0, options.every)

    boards = [Board(name.strip().casefold(), index, options.motion)
              for index, name in enumerate(options.names) if name.strip()]

    print(f"Reporting to {url} as {', '.join(b.name for b in boards)}. Ctrl+C to stop.")

    def send(payload):
        answer = post(url, token, context, payload)
        spoke = " (JARVIS spoke)" if answer.get("spoke") else ""
        print(f"  {json.dumps(payload)}{spoke}")

    started = time.monotonic()

    try:
        for board in boards:
            send({"name": board.name, "event": "online"})

        while not options.duration or time.monotonic() - started < options.duration:
            for board in boards:
                send(board.readings())

                if board.moved(every):
                    send({"name": board.name, "event": "motion"})

            time.sleep(every)

    except KeyboardInterrupt:
        pass

    print("Stopped. The panel slides away once the boards have been quiet for two minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
