"""tools/esp32_setup.py: preparing a board to check JARVIS's certificate.

Uses certificates made here, in a temporary folder, the way JARVIS makes
its own (a local authority signing a server certificate that names the
PC's addresses) -- never the real ones. Checked:

- the authority, stored in binary for the phone, reaches the sketch as the
  text setCACert() needs, and is the same certificate;
- a matching authority and server certificate pass; one from another run
  is refused, as the board would refuse it;
- an address the certificate does not name is refused, and so is a name
  given where the board needs an address;
- the config header carries the address, port and authority, and no token;
- the secrets header carries the token, with the wifi left for the user,
  and refuses a token that would break the C source;
- the header files are kept out of git;
- the tool's settings are phone.py's own, read from its source.

    python tools/test_esp32_setup.py
"""

import ipaddress
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from tools import esp32_setup  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def authority(label):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"JARVIS Local CA {label}")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=True,
                                     key_agreement=False, content_commitment=False, data_encipherment=False,
                                     crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def server_for(ca_key, ca, addresses):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    names = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS")]))
        .issuer_name(ca.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )


folder = tempfile.mkdtemp()
ca_key, ca = authority("one")
server = server_for(ca_key, ca, ["192.168.1.20", "100.101.102.103"])

ca_path = os.path.join(folder, "jarvis-phone-ca.cer")
cert_path = os.path.join(folder, "jarvis-phone-cert.pem")

with open(ca_path, "wb") as handle:
    handle.write(ca.public_bytes(serialization.Encoding.DER))   # binary, as JARVIS saves it

with open(cert_path, "wb") as handle:
    handle.write(server.public_bytes(serialization.Encoding.PEM))

loaded_ca, loaded_server = esp32_setup._load(ca_path, cert_path)
check(loaded_ca == ca and loaded_server == server, "the binary authority and the server certificate are read")

try:
    esp32_setup.check(loaded_ca, loaded_server, "192.168.1.20")
    check(True, "a matching authority and certificate, naming the address, pass")
except esp32_setup.SetupError as problem:
    check(False, f"a matching authority and certificate, naming the address, pass ({problem})")


def refused(ca_certificate, server_certificate, host, words):
    try:
        esp32_setup.check(ca_certificate, server_certificate, host)
    except esp32_setup.SetupError as problem:
        return words in str(problem)
    return False


_other_key, other_ca = authority("two")
check(refused(other_ca, loaded_server, "192.168.1.20", "different runs"),
      "an authority from another run is refused, as the board would refuse it")
check(refused(loaded_ca, loaded_server, "192.168.1.99", "does not name 192.168.1.99"),
      "an address the certificate does not name is refused")
check(refused(loaded_ca, loaded_server, "jarvis-pc", "not an IP address"),
      "a name where the board needs an address is refused")

try:
    esp32_setup._load(os.path.join(folder, "missing.cer"), cert_path)
    check(False, "missing certificates say to start JARVIS once")
except esp32_setup.SetupError as problem:
    check("Start JARVIS once" in str(problem), "missing certificates say to start JARVIS once")

header = esp32_setup.config_header(loaded_ca, "192.168.1.20", 8765)
check('#define JARVIS_HOST "192.168.1.20"' in header and "#define JARVIS_PORT 8765" in header,
      "the config header carries the address and port")

start = header.index("-----BEGIN CERTIFICATE-----")
end = header.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
round_trip = x509.load_pem_x509_certificate(header[start:end].encode())
check(round_trip == ca, "and the authority, as text for setCACert, the very same certificate")
check("TOKEN" not in header, "and no token")
check(all(ord(ch) < 128 for ch in header), "it is plain ASCII, as a sketch needs")

secrets = esp32_setup.secrets_header("abc123")
check('#define JARVIS_TOKEN "abc123"' in secrets and '#define WIFI_SSID "your wifi name"' in secrets,
      "the secrets header carries the token, with the wifi left for the user")

try:
    esp32_setup.secrets_header('bad"token')
    check(False, "a token that would break the C source is refused")
except esp32_setup.SetupError:
    check(True, "a token that would break the C source is refused")

# The tool uses phone.py's settings without importing it (that would start
# the voice system). Read phone.py's own definitions, not run them.
import ast  # noqa: E402

phone_source = ast.parse((ROOT / "phone.py").read_text(encoding="utf-8"))
defined = {}

for node in phone_source.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
        if name in ("TOKEN_ENV", "PORT_ENV", "DEFAULT_PORT") and isinstance(node.value, ast.Constant):
            defined[name] = node.value.value
        elif name in ("CA_FILE", "CERT_FILE"):
            literals = [n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            defined[name] = literals[-1] if literals else None

check(defined.get("TOKEN_ENV") == esp32_setup.TOKEN_ENV and defined.get("PORT_ENV") == esp32_setup.PORT_ENV
      and defined.get("DEFAULT_PORT") == esp32_setup.DEFAULT_PORT,
      f"the token and port settings are the ones phone.py uses ({defined.get('TOKEN_ENV')}, {defined.get('DEFAULT_PORT')})")
check(defined.get("CA_FILE") == esp32_setup.CA_FILE.name and defined.get("CERT_FILE") == esp32_setup.CERT_FILE.name,
      f"and so are the certificate files ({defined.get('CA_FILE')}, {defined.get('CERT_FILE')})")

ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
check("arduino/jarvis_sensor/jarvis_secrets.h" in ignored and "arduino/jarvis_sensor/jarvis_config.h" in ignored,
      "the header files are kept out of git")

# The sketch: a bare board reports no phantom movement, and the C3 Super
# Mini gets pins of its own, clear of the ones that decide how it starts.
import re  # noqa: E402

sketch = (ROOT / "arduino" / "jarvis_sensor" / "jarvis_sensor.ino").read_text(encoding="utf-8")
check("pinMode(PIR_PIN, INPUT_PULLDOWN)" in sketch, "the PIR pin is held low, so an unconnected one reads no movement")

c3 = sketch[sketch.index("#if defined(CONFIG_IDF_TARGET_ESP32C3)"):sketch.index("#else")]
c3_pins = {int(n) for n in re.findall(r"#define (?:DHT|PIR)_PIN (\d+)", c3)}
check(len(c3_pins) == 2 and not c3_pins & {2, 8, 9} and c3_pins <= set(range(0, 11)) | {20, 21},
      f"the C3 Super Mini uses pins it has, clear of GPIO 2, 8 and 9 ({sorted(c3_pins)})")
check("WiFi.setTxPower(WIFI_POWER_8_5dBm)" in sketch and sketch.index("WiFi.setTxPower") > sketch.rindex(
    "#if defined(CONFIG_IDF_TARGET_ESP32C3)"), "and a lower transmit power, only on the C3, so it can join the wifi")
check(sketch.index('#include "jarvis_secrets.h"') < sketch.index("#ifndef DHT_PIN"),
      "a pin may be changed in jarvis_secrets.h, which is read first")
called = [line for line in sketch.splitlines() if "setInsecure(" in line.split("//")[0]]
check(not called and "secure.setCACert(JARVIS_CA)" in sketch, "the board checks JARVIS's certificate, never setInsecure")

# Leave a config beside the stand-in headers for a compile check, when asked.
if os.environ.get("WRITE_CONFIG_TO"):
    Path(os.environ["WRITE_CONFIG_TO"], "jarvis_config.h").write_text(header, encoding="ascii")

sys.exit(1 if failures else 0)
