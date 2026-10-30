"""Give the toy cam its cloud brain: PC mode and cloud mode.

The toy (DFRobot ESP32-S3 AI Camera, "toy cam") can talk to JARVIS on this PC or to jarvis-toy-cloud
on Render, which needs no PC switched on. "Jarvis, switch to cloud mode" / "... PC mode" moves it
between the two (the toy keeps the choice in its flash). This writes what it needs to reach the cloud:

    python tools/toy_cloud_setup.py

- arduino/jarvis_camera/toy_cloud.h   the cloud's address and the toy's token. Kept out of git,
                                      and the token is never printed. Rewritten on every run.

The token is TOY_TOKEN in .env, the same value as TOY_TOKEN in Render's Environment settings; the
address is TOY_CLOUD_HOST in .env, else jarvis-toy-cloud.onrender.com. The cloud's certificate
authorities are in toy_cloud_ca.h beside the sketch (public, in git). Checked live, when it can be:
the cloud answers /health with a certificate those authorities signed.

Without toy_cloud.h the toy builds as before: PC only.
"""

import os
import re
import socket
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKETCH = ROOT / "arduino" / "jarvis_camera"
HEADER = SKETCH / "toy_cloud.h"
CA_HEADER = SKETCH / "toy_cloud_ca.h"

TOKEN_ENV = "TOY_TOKEN"
HOST_ENV = "TOY_CLOUD_HOST"
DEFAULT_HOST = "jarvis-toy-cloud.onrender.com"

_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class SetupError(Exception):
    """Something the user has to put right first."""


def header(host, token):
    """toy_cloud.h: the cloud's address and the toy's token."""
    host = (host or "").strip().casefold()
    token = (token or "").strip()

    if not token:
        raise SetupError(f"{TOKEN_ENV} is not set in .env: add the same value you put in Render.")

    # Written into C source: anything but a plain token would break it.
    if any(ch in token for ch in '"\\\n\r') or not token.isascii():
        raise SetupError(f"{TOKEN_ENV} contains characters a sketch cannot hold.")

    if not _HOST.match(host):
        raise SetupError(f"{HOST_ENV} is not a plain address such as {DEFAULT_HOST}.")

    return (
        "// The toy's cloud brain (tools/toy_cloud_setup.py). Kept out of git. Rewritten on every run.\n"
        "#pragma once\n"
        "\n"
        "// jarvis-toy-cloud on Render: talk, what the toy sees and the faces it finds, in cloud mode.\n"
        f'#define TOY_CLOUD_HOST "{host}"\n'
        "\n"
        "// The same as TOY_TOKEN in Render's Environment settings.\n"
        f'#define TOY_CLOUD_TOKEN "{token}"\n'
    )


def authorities():
    """The PEM text of toy_cloud_ca.h's authorities."""
    text = CA_HEADER.read_text(encoding="ascii")
    found = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)

    if not found:
        raise SetupError(f"{CA_HEADER.relative_to(ROOT)} has no certificates in it.")

    return "\n".join(found) + "\n"


def live_check(host, pem, timeout=10.0):
    """("accepted", issuer) when the cloud answers with a certificate the toy will accept;
    ("refused", why) when it answers with one it would not; ("unreachable", why) otherwise."""
    context = ssl.create_default_context(cadata=pem)

    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                issuer = dict(item[0] for item in tls.getpeercert().get("issuer", ()))
                return "accepted", issuer.get("organizationName", "") + " " + issuer.get("commonName", "")
    except ssl.SSLCertVerificationError as error:
        return "refused", error.verify_message or str(error)
    except (OSError, ssl.SSLError) as error:
        return "unreachable", str(error)


def main():
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    host = os.getenv(HOST_ENV) or DEFAULT_HOST

    try:
        text = header(host, os.getenv(TOKEN_ENV))
        pem = authorities()
    except SetupError as problem:
        print(f"Not ready: {problem}")
        return 1

    HEADER.write_text(text, encoding="ascii")
    print(f"Wrote {HEADER.relative_to(ROOT)} for https://{host.strip().casefold()} (the token is not shown).")

    outcome, detail = live_check(host.strip().casefold(), pem)

    if outcome == "accepted":
        print(f"Checked live: the cloud answered with a certificate the toy will accept ({detail.strip()}).")
    elif outcome == "refused":
        print(f"Not ready: the cloud answered, but the toy would refuse its certificate ({detail}).")
        print(f"Its signer is not in {CA_HEADER.relative_to(ROOT)}: send this message to be put right.")
        return 1
    else:
        print(f"The cloud could not be reached to check ({detail}). Run this again when online to be sure.")

    print()
    print("Then flash the toy:  pio run -e aicamdetect -t upload --upload-port COM8  (in arduino/jarvis_camera)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
