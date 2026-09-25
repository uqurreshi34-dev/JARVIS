"""phone_tls.py: one port, the right certificate for each caller.

With a certificate issued for the Tailscale name in use (JARVIS_CERT),
JARVIS served only that, and a board connecting by address -- checking
against JARVIS's own authority -- was refused ("unable to get local issuer
certificate"). Now a caller that names no host gets JARVIS's own
certificate. Served through werkzeug, as phone.py serves it, with
certificates made here. Checked:

- by address, trusting JARVIS's authority: accepted, JARVIS's own certificate;
- by name, trusting the issuing authority: accepted, the issued certificate;
- without the fallback, by address: refused -- the failure this fixes;
- which names count as "no host";
- esp32_setup's live check tells accepted, refused and not running apart;
- phone.py keeps JARVIS's own pair made and serves through phone_tls;
- JARVIS's own certificates carry the key identifiers that strict checking
  (Python 3.13 and later, by default) insists on, and are checked strictly
  here on any Python; ones made before that are replaced.

    python tools/test_phone_tls.py
"""

import http.client
import ipaddress
import os
import socket
import ssl
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

import phone_tls  # noqa: E402
from tools import esp32_setup  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


folder = tempfile.mkdtemp()
now = datetime.now(timezone.utc)


def authority(label):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
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


def issue(ca_key, ca, common_name, names, stem):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path, key_path = os.path.join(folder, stem + ".pem"), os.path.join(folder, stem + ".key")

    with open(cert_path, "wb") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM))

    with open(key_path, "wb") as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                       serialization.NoEncryption()))

    return cert_path, key_path


TAILNET_NAME = "jarvis.example.ts.net"

# JARVIS's own authority and certificate, naming the address; and a public
# authority's certificate for the Tailscale name, as JARVIS_CERT would be.
local_key, local_ca = authority("JARVIS Local CA")
local_pair = issue(local_key, local_ca, "JARVIS",
                   [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))], "local")
public_key, public_ca = authority("Stand-in Public CA")
issued_pair = issue(public_key, public_ca, TAILNET_NAME, [x509.DNSName(TAILNET_NAME)], "issued")


def app(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


def serve(context):
    server = make_server("127.0.0.1", 0, app, threaded=True, ssl_context=context)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def ask(port, trust, name=None):
    """GET / as a caller trusting [trust], by address or by [name]."""
    context = ssl.create_default_context(cadata=trust.public_bytes(serialization.Encoding.DER))
    context.verify_flags |= ssl.VERIFY_X509_STRICT     # as Python 3.13 and later check by default
    host = name or "127.0.0.1"

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secured:
                subject = x509.load_der_x509_certificate(secured.getpeercert(binary_form=True)).subject
                served = subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
                secured.sendall(b"GET / HTTP/1.1\r\nHost: " + host.encode() + b"\r\nConnection: close\r\n\r\n")
                answer = secured.recv(200)
                return ("accepted", served) if answer.startswith(b"HTTP/1.1 200") else ("odd", answer[:40])

    except ssl.SSLCertVerificationError as error:
        return "refused", error.verify_message


both = serve(phone_tls.serving_context(*issued_pair, local=local_pair))
port = both.server_port

outcome, served = ask(port, local_ca)
check(outcome == "accepted" and served == "JARVIS",
      f"by address, trusting JARVIS's authority: accepted, JARVIS's own certificate ({outcome}, {served})")

outcome, served = ask(port, public_ca, TAILNET_NAME)
check(outcome == "accepted" and served == TAILNET_NAME,
      f"by name, trusting the issuing authority: accepted, the issued certificate ({outcome}, {served})")

check(esp32_setup.live_check("127.0.0.1", port, local_ca)[0] == "accepted",
      "esp32_setup's live check accepts it, as the board will")

only_issued = serve(phone_tls.serving_context(*issued_pair))
outcome, reason = ask(only_issued.server_port, local_ca)
check(outcome == "refused", f"without the fallback, by address: refused, as it was ({reason})")

outcome, reason = esp32_setup.live_check("127.0.0.1", only_issued.server_port, local_ca)
check(outcome == "refused", f"and the live check says so, rather than 'Ready' ({reason})")

closed = socket.socket()
closed.bind(("127.0.0.1", 0))
free_port = closed.getsockname()[1]
closed.close()
check(esp32_setup.live_check("127.0.0.1", free_port, local_ca, timeout=1)[0] == "absent",
      "with JARVIS not running, the live check says so")

check(phone_tls.wants_local(None) and phone_tls.wants_local("") and phone_tls.wants_local("192.168.1.157")
      and phone_tls.wants_local("::1") and not phone_tls.wants_local(TAILNET_NAME),
      "no name, or an address sent as one, gets the local certificate; a name does not")

# JARVIS's own certificates, made by phone.py's own code -- read from its
# source and run here, since importing phone.py starts the voice system.
import ast  # noqa: E402

source_tree = ast.parse((ROOT / "phone.py").read_text(encoding="utf-8"))
wanted = {"ensure_certificate", "_certificate_covers", "_certificate_complete"}
functions = [node for node in source_tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
made = os.path.join(folder, "made")
os.makedirs(made)
space = {
    "os": os,
    "CERT_FILE": os.path.join(made, "cert.pem"), "KEY_FILE": os.path.join(made, "key.pem"),
    "CA_FILE": os.path.join(made, "ca.cer"),
    "CERT_ENV": "JARVIS_TEST_UNSET_CERT", "HOST_ENV": "JARVIS_TEST_UNSET_HOST",
    "certificate_addresses": lambda: ["127.0.0.1"],
}
exec(compile(ast.Module(body=functions, type_ignores=[]), "phone.py", "exec"), space)

check(len(functions) == 3 and space["ensure_certificate"](), "phone.py makes JARVIS's certificates")
check(space["_certificate_complete"](space["CERT_FILE"], space["CA_FILE"]),
      "with the key identifiers strict checkers insist on")

with open(space["CA_FILE"], "rb") as handle:
    made_ca = x509.load_der_x509_certificate(handle.read())

made_server = serve(phone_tls.serving_context(*issued_pair, local=(space["CERT_FILE"], space["KEY_FILE"])))
outcome, served = ask(made_server.server_port, made_ca)
check(outcome == "accepted" and served == "JARVIS",
      f"which a strict check accepts, by address ({outcome}, {served})")
check(esp32_setup.live_check("127.0.0.1", made_server.server_port, made_ca)[0] == "accepted",
      "and so does esp32_setup's live check")
made_server.shutdown()

# One made the old way, without the identifiers, is replaced.
old_key, old_ca = local_key, local_ca
bare = (
    x509.CertificateBuilder()
    .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS")]))
    .issuer_name(old_ca.subject).public_key(old_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
    .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
    .sign(old_key, hashes.SHA256())
)

with open(space["CERT_FILE"], "wb") as handle:
    handle.write(bare.public_bytes(serialization.Encoding.PEM))

check(not space["_certificate_complete"](space["CERT_FILE"], space["CA_FILE"]),
      "one made without them is recognised")
check(space["ensure_certificate"]() and space["_certificate_complete"](space["CERT_FILE"], space["CA_FILE"]),
      "and replaced when JARVIS starts")

both.shutdown()
only_issued.shutdown()

source = (ROOT / "phone.py").read_text(encoding="utf-8")
start = source[source.index("    def start(self):"):source.index("    def stop(self):")]
check("local_ready = ensure_certificate()" in start and "if not supplied and not local_ready" in start,
      "phone.py keeps JARVIS's own pair made, even when serving a supplied one")
check("phone_tls.serving_context(" in start and "ssl_context=context" in start,
      "and serves through phone_tls")

sys.exit(1 if failures else 0)
