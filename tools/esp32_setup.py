"""Prepare an ESP32 sensor sketch to talk to JARVIS securely.

A board posts to JARVIS over HTTPS, and must know whom it is talking to:
without that, anything on the wifi could pretend to be JARVIS. The board
is given JARVIS's own certificate authority -- the one the phone trusts --
so it checks the certificate properly with setCACert() instead of
switching checking off with setInsecure().

Run it from the JARVIS folder, after JARVIS has run at least once (which
is when the certificates are made):

    python tools/esp32_setup.py

It writes two files beside each sketch, the sensor in arduino/jarvis_sensor/
and the camera in arduino/jarvis_camera/, all kept out of git:

- jarvis_config.h   JARVIS's address, port and certificate authority.
                    Rewritten on every run.
- jarvis_secrets.h  your wifi name and password, and the phone token.
                    Made once, with the token filled in from .env; you add
                    the wifi details by hand. Never rewritten, and the
                    token is never printed. The camera's is made with the
                    wifi details already in the sensor's, when there are
                    some, and named "my room cam".

Checked before anything is written: the certificate authority really did
sign the certificate JARVIS serves, and that certificate names the address
the board will use. If JARVIS later makes new certificates (because this
PC's address changed), run this again and upload the sketch again.
"""

import ipaddress
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# JARVIS's phone server settings, the same names phone.py uses. Not
# imported from there: importing phone.py starts the voice system too, far
# too much for reading two files. test_esp32_setup.py reads phone.py's own
# definitions to keep these the same.
CA_FILE = ROOT / "jarvis-phone-ca.cer"
CERT_FILE = ROOT / "jarvis-phone-cert.pem"
TOKEN_ENV = "JARVIS_PHONE_TOKEN"
PORT_ENV = "JARVIS_PHONE_PORT"
DEFAULT_PORT = 8765

SKETCH = ROOT / "arduino" / "jarvis_sensor"
CONFIG_H = SKETCH / "jarvis_config.h"
SECRETS_H = SKETCH / "jarvis_secrets.h"

# The camera, named after its room with "cam" on the end so the house plan
# places it there and points it out of that room's window.
CAMERA_SKETCH = ROOT / "arduino" / "jarvis_camera"
CAMERA_NAME = "my room cam"

# Every sketch: its folder, and the name a new secrets file gives it.
SKETCHES = ((SKETCH, "room"), (CAMERA_SKETCH, CAMERA_NAME))

# The first ESP32 Arduino release whose TLS library checks an IP address
# named in a certificate; older ones compare only text names and would
# refuse JARVIS's certificate when the board connects by address.
MINIMUM_BOARD_PACKAGE = "3.1"


class SetupError(Exception):
    """Something the user has to put right before the board can connect."""


def _load(ca_path, cert_path):
    from cryptography import x509

    if not os.path.exists(ca_path) or not os.path.exists(cert_path):
        raise SetupError(
            "JARVIS's certificates are not there yet. Start JARVIS once "
            "(python main.py) so it makes them, then run this again."
        )

    with open(ca_path, "rb") as handle:
        raw = handle.read()

    # The phone wants the authority in binary (DER); read either form.
    ca = x509.load_pem_x509_certificate(raw) if raw.lstrip().startswith(b"-----") else x509.load_der_x509_certificate(raw)

    with open(cert_path, "rb") as handle:
        server = x509.load_pem_x509_certificate(handle.read())

    return ca, server


def addresses_named(server):
    """Every IP address the server certificate vouches for."""
    from cryptography import x509

    try:
        names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []

    return [str(address) for address in names.get_values_for_type(x509.IPAddress)]


def check(ca, server, host):
    """Raise SetupError unless a board using [host] would accept JARVIS."""
    try:
        server.verify_directly_issued_by(ca)
    except Exception:
        raise SetupError(
            "The certificate authority file did not sign the certificate JARVIS "
            "serves; they are from different runs. Start JARVIS once so it puts "
            "them right, then run this again."
        )

    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise SetupError(f"{host!r} is not an IP address; the board connects by address.")

    named = addresses_named(server)

    if host not in named:
        raise SetupError(
            f"JARVIS's certificate does not name {host} (it names {', '.join(named) or 'no address'}). "
            "Start JARVIS on this network so it renews it, then run this again."
        )


def pem(certificate):
    from cryptography.hazmat.primitives import serialization

    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def config_header(ca, host, port):
    """jarvis_config.h: the address, the port and the authority, nothing secret."""
    return (
        "// Written by tools/esp32_setup.py. Do not edit: run it again instead.\n"
        "// JARVIS's address and port, and the certificate authority the board\n"
        "// checks JARVIS's certificate against (setCACert, never setInsecure).\n"
        "#pragma once\n"
        "\n"
        f'#define JARVIS_HOST "{host}"\n'
        f"#define JARVIS_PORT {int(port)}\n"
        "\n"
        'static const char JARVIS_CA[] = R"JARVISCA(\n'
        f"{pem(ca).strip()}\n"
        ')JARVISCA";\n'
    )


def secrets_header(token, name="room", wifi=None):
    """jarvis_secrets.h: made once; the wifi details are the user's to add.

    [wifi] is the (ssid, password) lines already written in another sketch's
    secrets, copied as they are so the user types them once.
    """
    token = token or ""

    # Written into C source: anything but a plain token would break it.
    if any(ch in token for ch in '"\\\n\r'):
        raise SetupError("JARVIS_PHONE_TOKEN contains characters a sketch cannot hold.")

    if any(ch in name for ch in '"\\\n\r'):
        raise SetupError("A board's name cannot hold quotes or backslashes.")

    ssid, password = wifi or ('#define WIFI_SSID "your wifi name"', '#define WIFI_PASSWORD "your wifi password"')

    return (
        "// Your wifi and JARVIS's token. Kept out of git. Fill in the wifi.\n"
        "#pragma once\n"
        "\n"
        f"{ssid}\n"
        f"{password}\n"
        "\n"
        "// The same token the phone uses (JARVIS_PHONE_TOKEN in .env).\n"
        f'#define JARVIS_TOKEN "{token}"\n'
        "\n"
        "// What JARVIS calls this board, and the room the house plan puts it in:\n"
        "// \"room\" is the room's own sensor; \"my room cam\" is its camera.\n"
        f'#define SENSOR_NAME "{name}"\n'
    )


def wifi_lines(secrets_path):
    """The WIFI_SSID and WIFI_PASSWORD lines of a secrets file, if filled in; else None.

    Read, never printed: the password goes from one ignored file to another.
    """
    try:
        lines = Path(secrets_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    ssid = next((line.strip() for line in lines if line.strip().startswith("#define WIFI_SSID ")), None)
    password = next((line.strip() for line in lines if line.strip().startswith("#define WIFI_PASSWORD ")), None)

    if not ssid or not password or '"your wifi name"' in ssid or '"your wifi password"' in password:
        return None

    return ssid, password


def live_check(host, port, ca, timeout=3.0):
    """Connect to the running JARVIS as the board will, and say how it went.

    Returns ("accepted", None), ("refused", reason) or ("absent", reason).
    The files alone cannot settle it: what counts is the certificate JARVIS
    actually serves to a caller connecting by address, which is only known
    by asking it.
    """
    import socket
    import ssl
    from cryptography.hazmat.primitives import serialization

    context = ssl.create_default_context(cadata=ca.public_bytes(serialization.Encoding.DER))

    # Strict on every Python, not only 3.13 and later where it is the
    # default, so the answer is the same whichever one runs this.
    context.verify_flags |= ssl.VERIFY_X509_STRICT

    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as raw:
            # By address, as the board connects: no name is sent.
            with context.wrap_socket(raw, server_hostname=host):
                return "accepted", None

    except ssl.SSLCertVerificationError as error:
        return "refused", error.verify_message

    except ssl.SSLError as error:
        return "refused", str(error)

    except OSError as error:
        return "absent", str(error)


def local_address():
    """This PC's address on the local network, as phone.py finds it."""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        # No packet is sent: connecting a UDP socket only picks the route.
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def main():
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    token = os.getenv(TOKEN_ENV)

    if not token:
        print(
            f"{TOKEN_ENV} is not set in .env. Without it JARVIS picks a new "
            "token every start and the board could never keep up. Add a line such as\n"
            f"    {TOKEN_ENV}=a-long-random-word\n"
            "to .env, restart JARVIS, and run this again."
        )
        return 1

    host = os.getenv("JARVIS_SENSOR_HOST") or local_address()
    port = int(os.getenv(PORT_ENV) or DEFAULT_PORT)

    try:
        ca, server = _load(CA_FILE, CERT_FILE)
        check(ca, server, host)
    except SetupError as problem:
        print(f"Not ready: {problem}")
        return 1

    print(f"Ready. The boards will reach JARVIS at https://{host}:{port}")
    print("They check JARVIS's certificate against JARVIS's own authority (setCACert).")

    for folder, name in SKETCHES:
        if not folder.exists():
            continue

        config, secrets = folder / "jarvis_config.h", folder / "jarvis_secrets.h"
        config.write_text(config_header(ca, host, port), encoding="ascii")
        print(f"Wrote {config.relative_to(ROOT)}")

        if secrets.exists():
            print(f"Kept {secrets.relative_to(ROOT)} as it is.")
            continue

        # The wifi from a sketch that already has it, so it is typed once.
        wifi = next((found for other, _name in SKETCHES if other != folder
                     for found in [wifi_lines(other / "jarvis_secrets.h")] if found), None)
        secrets.write_text(secrets_header(token, name, wifi), encoding="ascii")

        if wifi:
            print(f"Wrote {secrets.relative_to(ROOT)} with the wifi you already set, named \"{name}\".")
        else:
            print(f"Wrote {secrets.relative_to(ROOT)}: add your wifi name and password to it.")

    # The files are right; now whether JARVIS really serves the board
    # something it will accept.
    outcome, reason = live_check(host, port, ca)

    if outcome == "accepted":
        print("Checked live: JARVIS answered with a certificate the board will accept.")
    elif outcome == "refused":
        print()
        print(f"Not ready: JARVIS answered, but with a certificate the board would refuse ({reason}).")
        print("Restart JARVIS so it serves the board its own certificate, then run this again.")
        return 1
    else:
        print("JARVIS is not running, so it could not be checked live. Start it and run this again to be sure.")

    print()
    print(f"In the Arduino IDE, the esp32 board package must be {MINIMUM_BOARD_PACKAGE} or later")
    print("(Tools > Board > Boards Manager > esp32): older ones cannot check an address in a certificate.")
    print(f"Tip: reserve {host} for this PC in your router, so it never changes and the board never")
    print("needs setting up again.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
