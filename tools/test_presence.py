"""Whether it is you: your phone as Tailscale sees it, and the greeting.

Checked, with Tailscale's answer made up rather than asked:

- your phone found by JARVIS_PHONE_DEVICE, or as the tailnet's only phone,
  and never guessed among two;
- home only with an address on the home network among the phone's; online
  elsewhere is away; off, missing, or no network known is unknown;
- the greeting: welcome back with the phone home or unknown, movement with
  the phone away said plainly, and nothing at all while you have been
  talking to JARVIS in the last five minutes;
- answers name a board's room as the house plan does, "your room";
- presence.phone() asks Tailscale at most every thirty seconds.

    python tools/test_presence.py
"""

import ipaddress
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

from actions import presence, sensors  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


HOME = ipaddress.ip_network("192.168.1.0/24")


def status(*peers):
    return {"Peer": {f"key{index}": peer for index, peer in enumerate(peers)}}


def phone(online=True, addrs=(), name="galaxy-a17", system="android"):
    return {"HostName": name, "DNSName": f"{name}.tail1234.ts.net.", "OS": system, "Online": online,
            "Addrs": list(addrs), "CurAddr": ""}


laptop = {"HostName": "laptop", "DNSName": "laptop.tail1234.ts.net.", "OS": "windows", "Online": True,
          "Addrs": ["192.168.1.157:41641"]}

os.environ.pop(presence.PHONE_ENV, None)

# ---- which device is your phone ------------------------------------------------------------

check(presence.find_phone(status(laptop, phone()))["HostName"] == "galaxy-a17", "the tailnet's only phone is yours")
check(presence.find_phone(status(phone(name="a"), phone(name="b"))) is None, "two phones: neither is guessed")
check(presence.find_phone(status(phone(name="a"), phone(name="b")), wanted="b")["HostName"] == "b",
      "JARVIS_PHONE_DEVICE picks one by its Tailscale name")
check(presence.find_phone(status(phone(name="a")), wanted="a.tail1234.ts.net")["HostName"] == "a",
      "or by its full name")

# ---- home, away, unknown ---------------------------------------------------------------------

at_home = status(laptop, phone(addrs=["86.1.2.3:41641", "192.168.1.23:41641"]))
away = status(laptop, phone(addrs=["86.1.2.3:41641", "10.20.0.5:41641"]))
check(presence.judge(at_home, HOME) == "home", "an address on the home network is home")
check(presence.judge(away, HOME) == "away", "online with no home address is away")
check(presence.judge(status(laptop, phone(online=False, addrs=["192.168.1.23:1"])), HOME) == "unknown",
      "a phone that is off is unknown, not away")
check(presence.judge(None, HOME) == "unknown", "no Tailscale here is unknown")
check(presence.judge(at_home, None) == "unknown", "no home network known is unknown")
check(presence.judge(status(laptop), HOME) == "unknown", "no phone on the tailnet is unknown")
check(presence.judge(status(phone(addrs=["[fe80::1]:41641", "192.168.1.9:41641"])), HOME) == "home",
      "an IPv6 address alongside does not confuse it")

# ---- asked at most every thirty seconds ---------------------------------------------------------

calls = []
real_status, real_network = presence._status, presence.home_network
presence._status = lambda: (calls.append(1), at_home)[1]
presence.home_network = lambda: HOME
presence.forget()
check(presence.phone() == "home" and presence.phone() == "home" and len(calls) == 1, "Tailscale is asked once, then cached")
presence.forget()
presence._status, presence.home_network = real_status, real_network

# ---- the greeting --------------------------------------------------------------------------------

answer = {"value": "home"}
sensors.set_presence(lambda: answer["value"])
sensors.set_namer(lambda board: {"room": "my room"}.get(board))


def arrive():
    sensors.reset()
    sensors.report({"name": "room", "event": "online"})
    return sensors.report({"name": "room", "event": "motion"})


check(arrive() == "Welcome back, sir.", "movement with your phone home: welcome back")

answer["value"] = "unknown"
check(arrive() == "Welcome back, sir.", "with no way to tell, as before: welcome back")

answer["value"] = "away"
check(arrive() == "Movement in your room, sir, and your phone isn't home.",
      "movement with your phone away is said plainly, in the plan's words")

answer["value"] = "home"
sensors.reset()
sensors.report({"name": "room", "event": "online"})
sensors.heard_you()
check(sensors.report({"name": "room", "event": "motion"}) is None, "no greeting while you have been talking to him")

sensors._last_spoken -= sensors.TALKING_SECONDS + 1
sensors._sensors["room"]["moved"] = None
check(sensors.report({"name": "room", "event": "motion"}) == "Welcome back, sir.",
      "and it comes back once you have been quiet five minutes")

sensors.set_presence(lambda: (_ for _ in ()).throw(RuntimeError("tailscale fell over")))
check(arrive() == "Welcome back, sir.", "a presence check that fails never stops the greeting")

# ---- answers in the plan's words --------------------------------------------------------------------

sensors.set_presence(None)
arrive()
said = sensors.answer(sensors.question("is anybody in the room"))
check(said.startswith("Someone is in your room"), f"'the room' is the plan's room, said to you ({said!r})")
sensors.set_namer(None)
said = sensors.answer(sensors.question("is anybody in the room"))
check(said.startswith("Someone is in the room"), f"with no plan, the board's own name ({said!r})")

sensors.reset()
sys.exit(1 if failures else 0)
