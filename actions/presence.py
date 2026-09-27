"""Whether you are home: your phone, as Tailscale sees it.

A motion sensor knows that something moved, not who. Your phone is the
best witness JARVIS has to who: it goes where you go. Tailscale, running
on the PC and the phone, already knows every device on your tailnet and
the addresses each one can be reached on, so asking it costs nothing and
needs no new hardware.

"Online" alone is not enough. With Tailscale on, the phone is online on
4G across the city too. What says home is an address on this PC's own
network among the ones the phone reports: on the home wifi it has one,
away it does not.

    home      the phone is on this network
    away      the phone is online, somewhere else
    unknown   no Tailscale here, no phone found, or the phone is off

The phone is found by JARVIS_PHONE_DEVICE in .env (its Tailscale name,
"galaxy-a17"), or, with that unset, as the only phone on the tailnet.
JARVIS_HOME_NETWORK (for example 192.168.1.0/24) names the home network
when this PC's own address does not say it. Nothing here is printed with
an address in it, and nothing is kept on disk.
"""

import ipaddress
import json
import os
import socket
import subprocess
import threading
import time


PHONE_ENV = "JARVIS_PHONE_DEVICE"
NETWORK_ENV = "JARVIS_HOME_NETWORK"

# Asked at most this often: a motion report every thirty seconds does not
# need Tailscale asked every thirty seconds.
CACHE_SECONDS = 30.0

_PHONE_SYSTEMS = ("android", "ios")

_CANDIDATES = (
    "tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
)

_lock = threading.Lock()
_cached = None          # (monotonic, answer)


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _status():
    """`tailscale status --json`, parsed, or None."""
    for command in _CANDIDATES:
        try:
            result = subprocess.run([command, "status", "--json"], capture_output=True, text=True, timeout=4,
                                    creationflags=_no_window())
        except (OSError, subprocess.SubprocessError):
            continue

        if result.returncode == 0 and result.stdout.strip():
            try:
                return json.loads(result.stdout)
            except ValueError:
                return None

    return None


def _local_address():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        # No packet is sent: connecting a UDP socket only picks the route.
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def home_network():
    """The home network: JARVIS_HOME_NETWORK, or this PC's own /24."""
    named = (os.getenv(NETWORK_ENV) or "").strip()

    if named:
        try:
            return ipaddress.ip_network(named, strict=False)
        except ValueError:
            print(f"[JARVIS] {NETWORK_ENV} is not a network (like 192.168.1.0/24); using this PC's own")

    address = _local_address()

    if not address or address.startswith("127."):
        return None

    return ipaddress.ip_network(f"{address}/24", strict=False)


def _names(peer):
    names = {str(peer.get("HostName") or "").casefold()}
    dns = str(peer.get("DNSName") or "").casefold().rstrip(".")

    if dns:
        names.add(dns)
        names.add(dns.split(".")[0])

    return {name for name in names if name}


def find_phone(status, wanted=None):
    """Your phone among the tailnet's devices, or None."""
    peers = list(((status or {}).get("Peer") or {}).values())
    wanted = (wanted if wanted is not None else os.getenv(PHONE_ENV) or "").strip().casefold()

    if wanted:
        return next((peer for peer in peers if wanted in _names(peer)), None)

    phones = [peer for peer in peers if str(peer.get("OS") or "").casefold() in _PHONE_SYSTEMS]
    return phones[0] if len(phones) == 1 else None


def _addresses(peer):
    """The IP addresses a peer reports it can be reached on."""
    found = []

    for endpoint in list(peer.get("Addrs") or []) + [peer.get("CurAddr") or ""]:
        host = str(endpoint).rsplit(":", 1)[0].strip("[]")

        try:
            found.append(ipaddress.ip_address(host))
        except ValueError:
            continue

    return found


def judge(status, network, wanted=None):
    """"home", "away" or "unknown", from a status and the home network."""
    phone = find_phone(status, wanted)

    if phone is None or not phone.get("Online"):
        return "unknown"

    if network is None:
        return "unknown"

    if any(address in network for address in _addresses(phone)):
        return "home"

    return "away"


def phone():
    """Whether your phone is home, from Tailscale, asked at most every CACHE_SECONDS."""
    global _cached

    with _lock:
        if _cached and time.monotonic() - _cached[0] < CACHE_SECONDS:
            return _cached[1]

    answer = judge(_status(), home_network())

    with _lock:
        _cached = (time.monotonic(), answer)

    return answer


def forget():
    """Drop the cached answer. For tests, and after Tailscale is switched on."""
    global _cached

    with _lock:
        _cached = None
