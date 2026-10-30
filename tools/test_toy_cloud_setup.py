"""tools/toy_cloud_setup.py: the toy cam's cloud brain, toy_cloud.h.

    python tools/test_toy_cloud_setup.py

No network: the live check is tried against a local TLS server signed by a made-up authority.
Checked: the header holds the address and token and nothing else; a missing or unsafe token or a
strange address is refused with why; the authorities are read from toy_cloud_ca.h; a server whose
certificate they did not sign is refused, and one that cannot be reached is said to be unreachable.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import toy_cloud_setup as setup  # noqa: E402

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


def refused(host, token):
    try:
        setup.header(host, token)
    except setup.SetupError as problem:
        return str(problem)

    return None


text = setup.header(" Jarvis-Toy-Cloud.onrender.com ", " abc-DEF_123 ")
check('#define TOY_CLOUD_HOST "jarvis-toy-cloud.onrender.com"' in text
      and '#define TOY_CLOUD_TOKEN "abc-DEF_123"' in text and text.startswith("// The toy's cloud brain"),
      "the header: the cloud's address, tidied, and the token")
check(text.isascii() and "#pragma once" in text, "plain ASCII a sketch can include once")
check("TOY_TOKEN" in (refused(setup.DEFAULT_HOST, "") or ""), "no token: refused, saying which setting")
check("cannot hold" in (refused(setup.DEFAULT_HOST, 'ab"c') or "") and refused(setup.DEFAULT_HOST, "a\\b"),
      "a token a C string cannot hold: refused")
check(refused("https://x.onrender.com", "t") and refused("localhost", "t") and refused("a b.com", "t"),
      "an address that is not a plain host name: refused")
check(setup.DEFAULT_HOST == "jarvis-toy-cloud.onrender.com", "jarvis-toy-cloud on Render by default")

pem = setup.authorities()
check(pem.count("-----BEGIN CERTIFICATE-----") == 4, "the four authorities read from toy_cloud_ca.h")
check(setup.HEADER.name == "toy_cloud.h" and setup.HEADER.parent == setup.CA_HEADER.parent,
      "written beside the camera sketch")
ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
check("arduino/jarvis_camera/toy_cloud.h" in ignored, "and kept out of git")

# ---- the live check, against a local server ----

try:
    import datetime
    import socket
    import ssl
    import tempfile
    import threading

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    def certificate(name, key, issuer_name, issuer_key, ca):
        now = datetime.datetime.now(datetime.timezone.utc)
        builder = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                   .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))

        # Python 3.13 on checks these as browsers do.
        builder = builder.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)

        if ca:
            builder = builder.add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False),
                critical=True)
        else:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            builder = builder.add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                                            critical=False)

        return builder.sign(issuer_key, hashes.SHA256())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = certificate("Made-up Root", ca_key, "Made-up Root", ca_key, True)
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = certificate("localhost", server_key, "Made-up Root", ca_key, False)
    folder = Path(tempfile.mkdtemp())
    (folder / "cert.pem").write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    (folder / "key.pem").write_bytes(server_key.private_bytes(serialization.Encoding.PEM,
                                                              serialization.PrivateFormat.PKCS8,
                                                              serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / "cert.pem", folder / "key.pem")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def serve():
        for _ in range(2):
            try:
                connection, _address = listener.accept()
                with context.wrap_socket(connection, server_side=True) as tls:
                    tls.recv(1)
            except (OSError, ssl.SSLError):
                pass

    threading.Thread(target=serve, daemon=True).start()
    real_connect = setup.socket.create_connection
    setup.socket.create_connection = lambda address, timeout=None: real_connect(("127.0.0.1", port), timeout=timeout)

    ours = ca_cert.public_bytes(serialization.Encoding.PEM).decode()
    outcome, detail = setup.live_check("localhost", ours, timeout=5)
    check(outcome == "accepted" and "Made-up Root" in detail, "signed by an authority the toy has: accepted")
    outcome, detail = setup.live_check("localhost", pem, timeout=5)
    check(outcome == "refused", "signed by one it has not: refused, saying why")

    setup.socket.create_connection = real_connect
    listener.close()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    free_port = closed.getsockname()[1]
    closed.close()
    setup.socket.create_connection = lambda address, timeout=None: real_connect(("127.0.0.1", free_port), timeout=timeout)
    outcome, _detail = setup.live_check("localhost", ours, timeout=2)
    setup.socket.create_connection = real_connect
    check(outcome == "unreachable", "nothing there: unreachable, not refused")
except ImportError as error:
    print(f"SKIP the live check (cryptography is not installed: {error})")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
