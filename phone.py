"""Talk to JARVIS from a phone on the same network.

The server runs inside the JARVIS process rather than beside it, so a
command from a phone goes through exactly the same handle_command() as
one spoken at the desk -- same memory, same patterns, same working set,
same everything. Nothing is duplicated, so nothing can drift.

Phase one is text only: type on the phone, read the reply. Speaking and
hearing come next, and the split is deliberate -- browsers refuse
microphone access over plain HTTP (navigator.mediaDevices is simply
undefined), so voice needs certificates and this does not. Getting the
whole request path working without that complication first means only
one hard problem at a time.

Built on werkzeug's make_server rather than Flask's app.run() for two
reasons: it can be shut down cleanly from another thread, and it takes
an ssl_context, which is what phase three will need.
"""

import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

from werkzeug.serving import ThreadedWSGIServer
from pywebpush import webpush, WebPushException

import phone_tls
import transcriber
from actions import location, askfiles, cameras, detections, files, highlight, sighting_search, sightings
from voice import FILLERS, set_phone_active

import speech


# Loaded here rather than relied upon from elsewhere: this module reads
# its settings at import time, and it is imported before anything that
# would otherwise have called load_dotenv() -- so without this, a token
# set in .env is silently ignored and a random one generated instead.
load_dotenv()

# The port the phone connects to. Well above anything privileged, and
# unlikely to collide with a dev server already running.
DEFAULT_PORT = 8765

TOKEN_ENV = "JARVIS_PHONE_TOKEN"
PORT_ENV = "JARVIS_PHONE_PORT"

CERT_FILE = os.path.join(os.path.dirname(__file__), "jarvis-phone-cert.pem")
KEY_FILE = os.path.join(os.path.dirname(__file__), "jarvis-phone-key.pem")

# Point these at a certificate issued by something a phone already
# trusts and the browser stops complaining -- no CA to install, and no
# warning to click through. Tailscale issues exactly that for a
# machine on your tailnet ("tailscale cert <name>.<tailnet>.ts.net"),
# which also makes JARVIS reachable from outside the house without
# opening a single port. Left unset, the self-signed pair below is
# generated and used as before, so nothing changes for local-only use.
CERT_ENV = "JARVIS_CERT"
KEY_ENV = "JARVIS_KEY"

VAPID_PRIVATE_ENV = "JARVIS_VAPID_PRIVATE"
VAPID_PUBLIC_ENV = "JARVIS_VAPID_PUBLIC"
VAPID_SUBJECT_ENV = "JARVIS_VAPID_SUBJECT"

# The name the phone should use. With Tailscale this is the MagicDNS
# name the certificate is actually issued for; a bare IP would fail
# validation however good the certificate is.
HOST_ENV = "JARVIS_PHONE_HOST"

# Any further addresses or names the certificate should vouch for,
# comma separated. Rarely needed: the LAN and Tailscale addresses are
# found automatically.
EXTRA_ADDRESSES_ENV = "JARVIS_EXTRA_ADDRESSES"
CA_FILE = os.path.join(
    os.path.dirname(__file__),
    "jarvis-phone-ca.cer"
)

# How long a single command may take before the phone gives up waiting.
# Generous, since a command can involve a model call.
REQUEST_TIMEOUT = 60

# How many unprompted announcements to hold for a phone that isn't
# collecting them. Enough for a long absence, bounded so a machine left
# running for days doesn't grow without limit.
MAX_NOTICES = 40

# How long the push service keeps trying to reach a phone that is asleep or
# out of signal with an alarm (movement at home while you are away), in
# seconds. An ordinary notice is sent once and dropped if it cannot land.
ALARM_TTL_ENV = "JARVIS_ALARM_PUSH_SECONDS"
ALARM_TTL_SECONDS = 900

# The phones that asked for notifications, in the JARVIS folder, so a
# restart does not silently stop them until each phone asks again -- an
# alarm that only works if the page was opened since JARVIS last started
# is no alarm. A subscription cannot be used to send anything without
# this PC's VAPID private key, which is never written here.
PUSH_FILE_NAME = ".jarvis-push.json"

# Maximum size of a picture sent from the phone camera.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

MAX_ASKFILES_ORGANISE_ITEMS = 5000

# Address prefixes that mean "on the home network".
#
# Worth knowing before relying on this: over Tailscale it tells you
# nothing about location. A tailnet address identifies the device, not
# where the device is, so a phone reports the same 100.x address on the
# sofa and on a train -- measured, not assumed. It is still meaningful
# for a phone reaching JARVIS directly over the LAN, which is why it is
# kept and configurable, but GPS in actions/location.py is what actually
# answers "am I home". /where reports what the server really sees, which
# is how that was established in the first place.
HOME_NETWORKS_ENV = "JARVIS_HOME_NETWORKS"
DEFAULT_HOME_NETWORKS = "192.168.,10.,172.16.,127.0.0.1,::1"

# Below this, a recording is too short to be a command. 16 kHz mono
# 16-bit is 32000 bytes a second, so this is about a third of a second
# of audio plus the WAV header -- shorter than anyone can say anything,
# but long enough that a real short command like "stop" still gets
# through.
MIN_VOICE_BYTES = 44 + 32000 // 3


def home_networks():
    """Address prefixes counted as being at home."""
    raw = os.getenv(HOME_NETWORKS_ENV) or DEFAULT_HOME_NETWORKS

    return [part.strip() for part in raw.split(",") if part.strip()]


def at_home(address):
    """Whether a client address looks like the home network.

    Free, needs no permission and costs no battery -- the server sees
    where a request came from regardless. It answers a coarser question
    than GPS ("am I on my own wifi") but answers it without asking
    anyone for anything.
    """
    address = (address or "").strip()

    if not address:
        return False

    return any(address.startswith(prefix) for prefix in home_networks())


def local_address():
    """This machine's address on the local network, best effort.

    Connecting a UDP socket outward doesn't send anything, but it does
    make the OS pick the interface it would actually route through --
    which is the address a phone on the same network can reach. More
    reliable than gethostbyname, which often returns 127.0.0.1.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        probe.connect(("8.8.8.8", 80))

        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _certificate_covers(path, addresses):
    """Whether an existing certificate vouches for every address.

    Cheap to check and worth checking: a certificate that predates the
    machine joining a Tailnet has no idea about the new address, and
    the only symptom is a browser warning that looks like something
    else entirely.
    """
    if not addresses:
        return True

    try:
        from cryptography import x509

        with open(path, "rb") as handle:
            certificate = x509.load_pem_x509_certificate(handle.read())

        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value

        covered = {str(entry) for entry in names.get_values_for_type(
            x509.IPAddress
        )}
        covered |= set(names.get_values_for_type(x509.DNSName))

    except Exception:
        # Unreadable, or built without the extension. Treated as not
        # covering, so it gets replaced rather than silently kept.
        return False

    return all(address in covered for address in addresses)


def _certificate_complete(cert_path, ca_path):
    """Whether the pair carries the key identifiers strict checkers insist on.

    Python 3.13 and later check certificates strictly by default, and
    refuse a server certificate that does not say which authority key
    signed it. Certificates made before JARVIS added these are replaced.
    """
    try:
        from cryptography import x509

        with open(cert_path, "rb") as handle:
            certificate = x509.load_pem_x509_certificate(handle.read())

        with open(ca_path, "rb") as handle:
            raw = handle.read()

        authority = (x509.load_pem_x509_certificate(raw) if raw.lstrip().startswith(b"-----")
                     else x509.load_der_x509_certificate(raw))

        certificate.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        authority.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        usage = authority.extensions.get_extension_for_class(x509.KeyUsage).value

        return bool(usage.key_cert_sign)

    except Exception:
        return False


def tailscale_address():
    """This machine's Tailscale address, or None.

    Asks Tailscale rather than guessing: the CLI is the only thing that
    reliably knows, and the address matters because a certificate has
    to name it or the browser refuses to trust the connection.
    """
    candidates = [
        "tailscale",
        r"C:\Program Files\Tailscale\tailscale.exe",
        "/usr/bin/tailscale",
        "/usr/local/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ]

    for command in candidates:
        try:
            result = subprocess.run(
                [command, "ip", "-4"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue

        address = (result.stdout or "").strip().splitlines()

        if result.returncode == 0 and address:
            return address[0].strip()

    return None


def certificate_addresses():
    """Every address the certificate should vouch for.

    The LAN address alone is not enough. Reached over Tailscale the
    connection arrives on a different address entirely, and a
    certificate that does not name it makes the browser warn -- which
    on Android matters more than it should, because a MagicDNS bug
    there can leave the address as the only way in.
    """
    addresses = []

    for candidate in (local_address(), tailscale_address()):
        if candidate and candidate not in addresses:
            addresses.append(candidate)

    extra = os.getenv(EXTRA_ADDRESSES_ENV) or ""

    for candidate in extra.split(","):
        candidate = candidate.strip()

        if candidate and candidate not in addresses:
            addresses.append(candidate)

    return addresses


def ensure_certificate():
    """Create a local CA and a server certificate for JARVIS HTTPS."""

    if (
        os.path.exists(CERT_FILE)
        and os.path.exists(KEY_FILE)
        and os.path.exists(CA_FILE)
    ):
        covers = _certificate_covers(CERT_FILE, certificate_addresses())
        complete = _certificate_complete(CERT_FILE, CA_FILE)

        if covers and complete:
            return True

        # An address JARVIS can now be reached on that the existing
        # certificate says nothing about -- a Tailscale address added
        # after it was first generated, typically. Regenerated rather
        # than left to warn, since a browser warning is the difference
        # between the phone working and not.
        #
        # Or one made before JARVIS gave its certificates the key
        # identifiers RFC 5280 asks for, which strict checkers refuse.
        if not covers:
            print(
                "[JARVIS] the certificate does not cover every address; "
                "regenerating it."
            )
        else:
            print(
                "[JARVIS] the certificate predates the key identifiers "
                "strict checkers require; regenerating it."
            )

        if os.getenv(CERT_ENV):
            print(
                f"[JARVIS] the phone uses {CERT_ENV}, so nothing changes there; "
                "run python tools/esp32_setup.py again for sensor boards."
            )
        else:
            print(
                "[JARVIS] you will need to install the new "
                f"{os.path.basename(CA_FILE)} on your phone, and run "
                "python tools/esp32_setup.py again for sensor boards."
            )

        for path in (CERT_FILE, KEY_FILE, CA_FILE):
            try:
                os.remove(path)
            except OSError:
                pass

    print("[JARVIS] generating local HTTPS certificates...")

    try:
        import ipaddress
        from datetime import datetime, timedelta

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        now = datetime.utcnow()

        # Everything JARVIS can be reached on, so the browser trusts
        # the connection whichever route it arrives by.
        alternative_names = [x509.DNSName("localhost")]

        for address in certificate_addresses():
            try:
                alternative_names.append(
                    x509.IPAddress(ipaddress.ip_address(address))
                )
            except ValueError:
                # A name rather than an address, which is equally
                # usable -- a Tailscale MagicDNS name, for instance.
                alternative_names.append(x509.DNSName(address))

        configured_host = os.getenv(HOST_ENV)

        if configured_host:
            try:
                ipaddress.ip_address(configured_host)
            except ValueError:
                alternative_names.append(x509.DNSName(configured_host))

        print(
            "[JARVIS] certificate will cover: "
            + ", ".join(str(n.value) for n in alternative_names)
        )

        # -------------------------------------------------------------
        # 1. Create the local JARVIS Certificate Authority.
        # -------------------------------------------------------------

        ca_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        ca_subject = x509.Name([
            # Dated, so a regenerated authority is a visibly different
            # entry on the phone. Every one used to be called exactly
            # "JARVIS Local CA", which meant a new one installed
            # alongside an old one collided -- Android files them by a
            # hash of the subject name, so two identically named
            # authorities land on the same entry and it may check
            # against the wrong one. The symptom is a certificate
            # error that looks like a name mismatch and isn't.
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"JARVIS Local CA {now.strftime('%Y-%m-%d %H%M')}",
            ),
        ])

        ca_certificate = (
            x509.CertificateBuilder()
            .subject_name(ca_subject)
            .issuer_name(ca_subject)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(
                x509.BasicConstraints(
                    ca=True,
                    path_length=None,
                ),
                critical=True,
            )
            # What RFC 5280 asks of an authority, and what strict checkers
            # (Python 3.13 and later, by default) insist on: it may sign
            # certificates, and it says which key is its own.
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=False,
                    key_cert_sign=True,
                    key_agreement=False,
                    content_commitment=False,
                    data_encipherment=False,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

        # -------------------------------------------------------------
        # 2. Create the JARVIS server certificate.
        # -------------------------------------------------------------

        server_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        server_subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS"),
        ])

        server_certificate = (
            x509.CertificateBuilder()
            .subject_name(server_subject)
            .issuer_name(ca_certificate.subject)
            .public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName(alternative_names),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(
                    ca=False,
                    path_length=None,
                ),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    key_cert_sign=False,
                    key_agreement=False,
                    content_commitment=False,
                    data_encipherment=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([
                    x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
                ]),
                critical=False,
            )
            # Which authority signed it, and its own key: without the
            # first, strict checkers refuse it ("Missing Authority Key
            # Identifier").
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

        # -------------------------------------------------------------
        # 3. Write the server private key.
        # -------------------------------------------------------------

        with open(KEY_FILE, "wb") as file:
            file.write(
                server_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )

        # -------------------------------------------------------------
        # 4. Write the server certificate.
        # -------------------------------------------------------------

        with open(CERT_FILE, "wb") as file:
            file.write(
                server_certificate.public_bytes(
                    serialization.Encoding.PEM
                )
            )

            # Include the CA certificate after the server certificate.
            # This allows the browser to receive the complete chain.
            file.write(
                ca_certificate.public_bytes(
                    serialization.Encoding.PEM
                )
            )

        # -------------------------------------------------------------
        # 5. Write the CA certificate in DER format.
        #
        # Android/Samsung gets this file, NOT the private key.
        # -------------------------------------------------------------

        with open(CA_FILE, "wb") as file:
            file.write(
                ca_certificate.public_bytes(
                    serialization.Encoding.DER
                )
            )

        print("[JARVIS] HTTPS certificates created.")
        return True

    except Exception as error:
        print(
            f"[JARVIS] could not generate HTTPS certificates: "
            f"{error}"
        )
        return False


# How long a connection is given to answer JARVIS's TLS goodbye.
_GOODBYE_SECONDS = 1.0

# How long a connection is given to finish its TLS handshake. A board on the
# wifi takes well under a second; one that has not finished in this long has
# lost a packet or gone, and is dropped.
_HANDSHAKE_SECONDS = 10.0


class _TidyServer(ThreadedWSGIServer):
    """The phone server: each TLS handshake on its own thread, and each connection ended as TLS says to.

    werkzeug wraps the listening socket, so the TLS handshake happens
    inside accept(), on the one thread that accepts every connection, with
    no time limit. A single connection that stalls part way through its
    handshake (a packet lost on the wifi is enough) stops JARVIS accepting
    anything, and the sensor board waiting on it sits out its own
    handshake limit of two minutes: the board fell silent for two minutes
    after a report, and its movement was missed. Here the listening socket
    is wrapped not to handshake on accept; each connection handshakes on
    its own request thread, given _HANDSHAKE_SECONDS, and one that stalls
    holds up nothing but itself.

    socketserver closes a finished connection by shutting down the TCP
    socket under the TLS layer, so the other end sees the connection
    drop without TLS's close_notify. Browsers shrug; a strict client
    reads that as an error, and the sensor boards' TLS library prints
    one for every report ("(-76) UNKNOWN ERROR CODE (004C)"). unwrap()
    sends close_notify first, then the socket is closed as before.
    """

    def __init__(self, host, port, app, ssl_context=None, **options):
        super().__init__(host, port, app, **options)

        if ssl_context is not None:
            self.socket = ssl_context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)
            self.ssl_context = ssl_context

    def finish_request(self, request, client_address):
        if isinstance(request, ssl.SSLSocket):
            before = request.gettimeout()

            try:
                request.settimeout(_HANDSHAKE_SECONDS)
                request.do_handshake()
            except (OSError, ValueError):
                # Stalled, refused or gone: shutdown_request closes it.
                return

            request.settimeout(before)

        super().finish_request(request, client_address)

    def shutdown_request(self, request):
        if isinstance(request, ssl.SSLSocket):
            try:
                request.settimeout(_GOODBYE_SECONDS)
                request.unwrap()
            except (OSError, ValueError):
                # Gone already, or never finished its handshake:
                # nothing to say goodbye to. ssl.SSLError is an OSError.
                pass

        super().shutdown_request(request)


class PhoneServer:
    """A small web server letting a phone drive the same assistant."""

    def __init__(self, port=None, token=None):
        self.port = int(port or os.getenv(PORT_ENV) or DEFAULT_PORT)

        # A shared secret, so being on the same wifi isn't by itself
        # enough to drive someone's desktop. Generated per run when
        # none is configured -- the URL is printed at startup either
        # way, so a generated one is no harder to use, it just won't
        # survive a restart.
        self.token = token or os.getenv(TOKEN_ENV) or secrets.token_urlsafe(8)

        self._handler = None
        self._server = None
        self._thread = None
        self._look_handler = None
        self._sensor_handler = None

        # Things JARVIS said unprompted that the phone hasn't collected
        # yet. Battery warnings, market alerts, pattern runs and the
        # morning diary are all announced to whoever is at the desk --
        # which is nobody, if you're in another room. Held here so the
        # phone can pick them up rather than them being said to an
        # empty chair.
        self._notices = []
        self._notices_lock = threading.Lock()
        self._next_notice_id = 0

        # Web Push subscriptions registered by the phone browser. Read
        # from PUSH_FILE_NAME when the server starts, written on each change.
        self._push_subscriptions = []
        self._push_lock = threading.Lock()

        # Notice ids count from zero each run, because the queue lives
        # in memory. A phone remembers how far it had read, so without
        # something identifying the run it would carry a cursor from a
        # previous session -- asking for "anything after 5" when the
        # new run has only reached 1, and so seeing nothing at all,
        # permanently. This lets the page notice the restart and start
        # over.
        self._run_id = secrets.token_hex(4)

        self._app = Flask(__name__)
        self._register_routes()

    def announce(self, text):
        """Hold something JARVIS said unprompted, for the phone to collect.

        Deliberately a queue rather than a push: the phone may not be
        connected, may be asleep, or may not exist. Anything said while
        nobody was looking is still worth reading afterwards.
        """
        text = (text or "").strip()

        if not text:
            return

        self._hold(text)

        # Push is deliberately separate from the backlog. If push fails,
        # /notices still guarantees the phone can recover the announcement.
        threading.Thread(
            target=self._send_push,
            args=(text,),
            daemon=True,
        ).start()

    def sighting(self, text, camera):
        """Something a camera recognised coming into view: to the phone now, not held behind a turn at the
        desk, and tapping it opens that camera's live feed. Quiet hours apply, as to any announcement."""
        text = (text or "").strip()

        if not text:
            return

        shown = {"name": cameras.normal_name(camera), "title": cameras.normal_name(camera).upper()}
        self._hold(text, shown)

        threading.Thread(
            target=self._send_push,
            args=(text, shown),
            daemon=True,
        ).start()

    def alarm(self, text, board):
        """Movement at home while you are away: to the phone now, with a way to see it.

        Held and pushed like any announcement, but the push is urgent, is
        not silenced by quiet hours, and carries the camera in the room
        board [board] is in, so tapping it opens that camera's live feed.
        """
        text = (text or "").strip()

        if not text:
            return

        camera = cameras.beside(board)
        self._hold(text, camera)

        threading.Thread(
            target=self._send_push,
            args=(text, camera, True),
            daemon=True,
        ).start()

    def _hold(self, text, camera=None):
        """Keep [text] for the phone to collect, with the camera it offers, if any."""
        notice = {"text": text, "at": time.strftime("%H:%M")}

        if camera:
            notice["camera"] = camera

        with self._notices_lock:
            self._next_notice_id += 1
            notice["id"] = self._next_notice_id
            self._notices.append(notice)

            # Bounded, because a JARVIS left running for days with no
            # phone connected should not accumulate for ever.
            if len(self._notices) > MAX_NOTICES:
                del self._notices[:-MAX_NOTICES]

    def notices_since(self, last_seen):
        """Everything announced after the caller's last seen id.

        Nothing is consumed by reading. Each device tracks how far it
        has got, so a phone and a tablet both see everything --
        clearing the queue on collection meant whichever polled first
        took them and the other never knew they existed.
        """
        with self._notices_lock:
            return [n for n in self._notices if n["id"] > last_seen]

    def latest_notice_id(self):
        """The newest id issued, so a caller can start from now."""
        with self._notices_lock:
            return self._next_notice_id

    def _send_push(self, text, camera=None, alarm=False):
        """Send an immediate Web Push notification to registered phones.

        An [alarm] is sent whatever the hour, marked urgent so a sleeping
        phone is woken for it, and kept by the push service for a while if
        the phone cannot be reached; [camera] is what tapping it opens.
        """
        if not text or not (alarm or self._push_allowed()):
            return

        private_key = os.getenv(VAPID_PRIVATE_ENV)
        subject = os.getenv(VAPID_SUBJECT_ENV)

        if not private_key or not subject:
            print(
                "[JARVIS] push is not configured; "
                "missing VAPID private key or subject."
            )
            return

        with self._push_lock:
            subscriptions = list(self._push_subscriptions)

        if not subscriptions:
            return

        payload = {
            "title": "JARVIS",
            "body": text,
        }

        delivery = {}

        if alarm:
            payload["alarm"] = True
            delivery = {"ttl": self._alarm_ttl(), "headers": {"Urgency": "high"}}

        if camera:
            payload["camera"] = camera

        vapid_claims = {
            "sub": subject,
        }

        expired = []

        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info=subscription,
                    data=json.dumps(payload),
                    vapid_private_key=private_key,
                    vapid_claims=vapid_claims,
                    **delivery,
                )

            except WebPushException as error:
                status = getattr(
                    getattr(error, "response", None),
                    "status_code",
                    None,
                )

                # 404/410 means the browser subscription is no longer valid.
                if status in (404, 410):
                    expired.append(subscription.get("endpoint"))

                else:
                    print(
                        f"[JARVIS] phone push failed: {error}"
                    )

            except Exception as error:
                print(
                    f"[JARVIS] phone push failed: {error}"
                )

        if expired:
            with self._push_lock:
                self._push_subscriptions = [
                    item
                    for item in self._push_subscriptions
                    if item.get("endpoint") not in expired
                ]
                self._save_push_locked()

    @staticmethod
    def _push_path():
        root = files.root()
        return os.path.join(root, PUSH_FILE_NAME) if root else None

    def _load_push(self):
        """The subscriptions kept from earlier runs, if there are any to read."""
        path = self._push_path()

        if not path or not os.path.exists(path):
            return

        try:
            with open(path, encoding="utf-8") as handle:
                kept = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"[JARVIS] could not read {PUSH_FILE_NAME}: {error}")
            return

        kept = [item for item in kept if isinstance(item, dict) and item.get("endpoint")] \
            if isinstance(kept, list) else []

        with self._push_lock:
            known = {item.get("endpoint") for item in self._push_subscriptions}
            self._push_subscriptions += [item for item in kept if item["endpoint"] not in known]

    def _save_push_locked(self):
        """Write the subscriptions out; called holding _push_lock. Replaced whole, never half written."""
        path = self._push_path()

        if not path:
            return

        try:
            with open(path + ".tmp", "w", encoding="utf-8") as handle:
                json.dump(self._push_subscriptions, handle)

            os.replace(path + ".tmp", path)
        except OSError as error:
            print(f"[JARVIS] could not keep the phone's notifications: {error}")

    def set_look_handler(self, handler):
        """Register what describes a picture from the phone.

        Takes (image bytes, question) and returns what to say.
        """
        self._look_handler = handler

    def set_sensor_handler(self, handler):
        """Register what to do when a sensor on the wifi reports in.

        Takes the reported payload and returns what to say, or None
        when the report was worth recording and not worth saying.
        """
        self._sensor_handler = handler

    def set_handler(self, handler):
        """Register what actually runs a command.

        Takes the spoken text, returns what JARVIS said back. Kept as a
        callback rather than importing commands directly so this module
        stays testable on its own, and so main.py keeps deciding how a
        command is actually run.
        """
        self._handler = handler

    @staticmethod
    def _hostname():
        """The name to reach JARVIS by.

        A certificate is issued for a name, so with a real one the URL
        has to use that name -- a bare IP fails validation however
        valid the certificate is. Falls back to the local address,
        which is right for the self-signed case where the name never
        mattered.
        """
        configured = os.getenv(HOST_ENV)

        if configured:
            return configured.strip()

        return local_address()

    @property
    def url(self):
        return f"https://{self._hostname()}:{self.port}/?t={self.token}"

    def _authorised(self):
        supplied = (
            request.args.get("t")
            or request.headers.get("X-Jarvis-Token")
            or ""
        )

        return secrets.compare_digest(supplied, self.token)

    @staticmethod
    def _camera_shown(text):
        """The camera a command asked to see ("show me the room cam"), for the phone to play; else None.

        The same matcher the desk uses, so the phone opens exactly what
        the desk would show.
        """
        try:
            asked = cameras.asked(text)
        except Exception:
            return None

        if not asked or asked[0] != "show" or not asked[1]["online"]:
            return None

        return {"name": asked[1]["name"], "title": cameras.title(text, asked[1])}

    @staticmethod
    def _cameras_shown(text):
        """Every connected camera, when a command asked for them all ("show me all the cameras"), for the
        phone to play as a grid; else None. Only for two or more: one alone is played as _camera_shown
        plays a camera asked for by name."""
        try:
            if not cameras.all_asked(text):
                return None

            showing = cameras.online()
        except Exception:
            return None

        if len(showing) < 2:
            return None

        return [{"name": camera["name"], "title": camera["name"].upper()} for camera in showing]

    def _camera_or_grid(self, text):
        """{"camera": one camera to play} or {"cameras": every camera, as a grid}, for a reply; and
        {"sighting": its picture} for "show me the last sighting", so the phone shows it too."""
        shown = self._camera_or_grid_only(text)

        wanted = sightings.asked(text)

        if wanted == "show":
            picture, record = sightings.last_picture_file()

            if picture:
                shown["sighting"] = {"title": f"SIGHTING {record['camera'].upper()}",
                                     "url": "/sighting/picture"}

        # "When was I last on the house cam": the newest sighting it found, by its folder's name.
        if wanted == "search":
            _, picture, title = sighting_search.answer(text)

            if picture:
                shown["sighting"] = {"title": title,
                                     "url": "/sighting/picture?name=" + os.path.basename(os.path.dirname(picture))}

        return shown

    def _camera_or_grid_only(self, text):
        grid = self._cameras_shown(text)

        if grid:
            return {"camera": None, "cameras": grid}

        if cameras.all_asked(text):
            alone = cameras.online()
            return {"camera": ({"name": alone[0]["name"], "title": alone[0]["name"].upper()} if alone else None),
                    "cameras": None}

        return {"camera": self._camera_shown(text), "cameras": None}

    def _register_routes(self):
        app = self._app

        @app.get("/service-worker.js")
        def service_worker():
            return SERVICE_WORKER.replace("__TOKEN__", self.token), 200, {
                "Content-Type": "application/javascript; charset=utf-8",
                "Cache-Control": "no-store",
            }

        @app.get("/")
        def page():
            return PAGE.replace(
                "__TOKEN__",
                self.token
            ).replace(
                "__VAPID_PUBLIC_KEY__",
                os.getenv(VAPID_PUBLIC_ENV, "")
            ), 200, {
                "Content-Type": "text/html; charset=utf-8",
                # Never an old copy: a phone holding the page from before an update kept its old code
                # (22 October 2026: "show me the last sighting" opened nothing there).
                "Cache-Control": "no-store",
            }

        @app.get("/where")
        def where():
            """What the server sees about where this request came from.

            Deliberately reports the raw address as well as the verdict.
            Whether a phone at home arrives from a 192.168 address or a
            Tailscale one depends on settings nobody should have to
            guess at -- so this makes it observable, and the home
            network list can then be set from what actually happens.
            """
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            address = request.remote_addr or ""

            return jsonify({
                "address": address,
                "home": at_home(address),
                "networks": home_networks(),
            })

        @app.post("/transcribe")
        def transcribe_only():
            """Turn speech into words without acting on them.

            /voice transcribes and then runs the result as a command, which is
            wrong for a question about a picture: "what am I holding" should
            be asked of the photograph, not carried out by the desk.
            """
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            audio = request.get_data()

            if not audio or len(audio) < MIN_VOICE_BYTES:
                return jsonify({"heard": ""})

            set_phone_active(True)

            try:
                text = transcriber.transcribe_wav(audio)
            except Exception as error:
                print(f"[JARVIS] phone transcription failed: {error}")
                return jsonify({"heard": ""})

            return jsonify({"heard": text or ""})

        @app.post("/look")
        def look():
            """A picture from the phone camera, and what to make of it.
            """
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            if not self._look_handler:
                return jsonify({
                    "error": "JARVIS is still starting up."
                }), 503

            image = request.get_data()

            if not image:
                return jsonify({"error": "No picture received."}), 400

            if len(image) > MAX_IMAGE_BYTES:
                return jsonify({
                    "error": "That picture is too large, sir."
                }), 413

            question = (request.args.get("q") or "").strip()

            try:
                reply = self._look_handler(image, question)

            except Exception as error:
                print(f"[JARVIS] phone camera failed: {error}")

                return jsonify({
                    "error": "I couldn't look at that."
                }), 500

            if isinstance(reply, dict):
                return jsonify({
                    "reply": reply.get("reply", ""),
                    "speak": reply.get("speak", []),
                })

            return jsonify({
                "reply": reply or "",
                "speak": [],
            })

        @app.post("/location")
        def location_report():
            """Where the phone says it is.

            Sent only when asked for: a browser cannot report position
            in the background anyway, and asking continuously would
            cost battery for readings nobody wanted.
            """
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            payload = request.get_json(silent=True) or {}

            accepted = location.remember_position(
                payload.get("latitude"),
                payload.get("longitude"),
                payload.get("accuracy"),
            )

            if not accepted:
                return jsonify({"error": "Unusable position."}), 400

            return jsonify({"ok": True, "home": location.is_home()})

        @app.post("/sensor")
        def sensor_report():
            """A board on the wifi saying what it can feel.

            The same door the phone uses, and the same token: being on
            the network is not by itself permission to make someone's
            desktop talk.

            Answers immediately and always. A microcontroller waiting
            on a reply is a microcontroller not watching its pin, and
            one that gets an error will usually just reboot and try
            again -- so a report that says nothing worth announcing is
            still a success, and says so.
            """
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            payload = request.get_json(silent=True) or {}

            if not self._sensor_handler:
                return jsonify({"ok": True, "spoke": False}), 200

            try:
                said = self._sensor_handler(payload)

            except Exception as error:
                # A failure up here must never look like a failure down
                # there. The board did its job by reporting; whatever
                # went wrong is ours, and it is logged rather than
                # bounced back as a status the board would retry.
                print(f"[JARVIS] sensor report failed: {error}", flush=True)

                return jsonify({"ok": True, "spoke": False}), 200

            return jsonify({"ok": True, "spoke": bool(said)}), 200

        @app.post("/camera/wanted")
        def camera_wanted():
            """A camera asking whether a picture is wanted, and whether to stream. Asked every
            second, so it answers at once. A camera that cannot stream takes the picture and
            ignores "live"."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            payload = request.get_json(silent=True) or {}
            name = payload.get("name")
            # A board may ask to wait (its "wait", in seconds) until something is wanted: answered the
            # moment it is, and otherwise when the wait is up -- one connection, and one TLS handshake on
            # the board, every few seconds rather than every second. Without it, answered at once.
            wait = payload.get("wait")
            wait = (min(float(wait), cameras.ASK_WAIT_MAX) if isinstance(wait, (int, float))
                    and not isinstance(wait, bool) and wait > 0 else 0.0)
            until = time.monotonic() + wait

            while True:
                # "look": take a detection look now and report it, asked what it sees (actions/detections.py)
                answer = {"snap": cameras.wanted(name), "live": cameras.live_wanted(name),
                          "look": detections.look_wanted(name), "still": cameras.still_wanted(name) is not None}

                if any(answer.values()) or time.monotonic() >= until:
                    if answer["still"]:
                        cameras.still_sent(name)

                    return jsonify(answer), 200

                cameras.wait_for_change(min(cameras.ASK_WAIT_STEP, until - time.monotonic()))

        @app.post("/camera/live")
        def camera_live():
            """A camera's live view: one long upload, frame after frame, while its picture is shown.

            Refused unless live view was asked for. The connection is given
            LIVE_READ_TIMEOUT to send each piece, so a camera that drops off
            the wifi frees its thread rather than holding it.
            """
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            name = request.headers.get("X-Jarvis-Camera", "")

            if not cameras.live_wanted(name):
                return jsonify({"error": "not watching"}), 409

            connection = request.environ.get("werkzeug.socket")

            if connection is not None:
                connection.settimeout(cameras.LIVE_READ_TIMEOUT)

            frames, seconds, ended = cameras.receive_live(name, request.stream,
                                                          look_asked_at=detections.look_asked_at)
            rate = frames / seconds if seconds > 0 else 0.0
            print(f"[JARVIS] {name} live: {frames} frames in {seconds:.0f}s ({rate:.1f} a second); "
                  f"{ended}", flush=True)
            return jsonify({"ok": True, "frames": frames}), 200

        @app.post("/camera/seen")
        def camera_seen():
            """What a camera recognised on its own board (YOLO26n, the house cam flashed as s3detect):
            names, scores and where each is, never a picture. Kept in memory; anyone coming into view
            is said aloud and sent to the phone, as other alerts are (actions/detections.py)."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            if request.content_length is None or request.content_length > 32 * 1024:
                return jsonify({"error": "too large"}), 413

            payload = request.get_json(silent=True)
            name = request.headers.get("X-Jarvis-Camera") or (payload or {}).get("name") or ""
            refused = detections.record(name, payload)

            if refused:
                return jsonify({"error": refused}), 400

            return jsonify({"ok": True}), 200

        @app.get("/cameras")
        def cameras_listed():
            """The wifi cameras, for the phone to offer: name, what each looks at, whether it is online."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            return jsonify({"cameras": cameras.listed()}), 200

        @app.get("/camera/marks")
        def camera_marks():
            """What is highlighted over a camera's live view now ("highlight the bottle"): its boxes, 0 to 1
            across and down the picture, for the phone to draw over the feed (actions/highlight.py)."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            return jsonify({"boxes": highlight.boxes(request.args.get("name") or "")}), 200

        @app.get("/camera/watch")
        def camera_watch():
            """A camera's live view on the phone: MJPEG, which an <img> plays as video.

            Over the same HTTPS and token as everything else -- through
            Tailscale when away from home. Watching keeps the camera live;
            leaving the page lets it stop.
            """
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            wanted = cameras.normal_name(request.args.get("name"))

            if wanted not in {camera["name"] for camera in cameras.listed()}:
                return jsonify({"error": "no such camera"}), 404

            # A tile in the phone's grid asks for fewer frames than one camera full screen: several
            # streams at once, on mobile data.
            fps = request.args.get("fps", type=float)
            fps = min(cameras.REMOTE_FPS, max(1.0, fps)) if fps else None

            return Response(cameras.remote_frames(wanted, fps=fps), mimetype=cameras.REMOTE_MIMETYPE,
                            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

        @app.post("/camera")
        def camera_picture():
            """A picture a camera was asked for: a JPEG, kept in memory only.

            The size is checked before the body is read, so a stray upload
            is refused without being taken in.
            """
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            if request.content_length is None or request.content_length > cameras.MAX_BYTES:
                return jsonify({"error": "too large"}), 413

            refused = cameras.store(request.headers.get("X-Jarvis-Camera", ""), request.get_data(cache=False))

            if refused:
                return jsonify({"error": refused}), 400

            return jsonify({"ok": True}), 200

        @app.post("/camera/still")
        def camera_still():
            """A still a camera was asked for (larger than its live view's): passed to a sighting
            (actions/sightings.py), checked for size before the body is read, never kept here."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            if request.content_length is None or request.content_length > cameras.MAX_STILL_BYTES:
                return jsonify({"error": "too large"}), 413

            refused = cameras.take_still(request.headers.get("X-Jarvis-Camera", ""), request.get_data(cache=False))

            if refused:
                return jsonify({"error": refused}), 400

            return jsonify({"ok": True}), 200

        @app.post("/camera/face")
        def camera_face():
            """A look's own picture in which the house cam's face finder saw a face: passed to the
            sighting it belongs to (actions/sightings.py), to tell at once whether it is you; checked
            for size before the body is read, never kept here."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            if request.content_length is None or request.content_length > cameras.MAX_STILL_BYTES:
                return jsonify({"error": "too large"}), 413

            try:
                score = float(request.headers.get("X-Jarvis-Face", ""))
            except ValueError:
                score = None

            refused = cameras.take_face(request.headers.get("X-Jarvis-Camera", ""), request.get_data(cache=False), score)

            if refused:
                return jsonify({"error": refused}), 400

            return jsonify({"ok": True}), 200

        @app.get("/sighting/picture")
        def sighting_picture():
            """The newest sighting's best picture, for the phone ("show me the last sighting"): over the
            same HTTPS and token as everything else, read from the sighting's folder, never changed."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            # A sighting named by its folder (a search's answer), else the newest.
            name = request.args.get("name")
            picture = sighting_search.picture_of(name) if name else sightings.last_picture_file()[0]

            if not picture or not os.path.isfile(picture):
                return jsonify({"error": "no sighting picture"}), 404

            with open(picture, "rb") as kept:
                data = kept.read()

            sightings._log(f"viewed  {os.path.basename(os.path.dirname(picture))}  (on the phone)")
            return app.response_class(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

        @app.get("/health")
        def health():
            # Deliberately open: the page polls this to tell "JARVIS
            # isn't running" from "you typed the wrong token", and a
            # liveness check leaks nothing worth protecting.
            return jsonify({"ok": True})

        @app.get("/notices")
        def notices():
            """Anything JARVIS announced while nobody was at the desk.

            Collected on the poll the page already makes, rather than a
            second timer -- the phone is checking whether the PC is
            alive every few seconds regardless, so this costs nothing
            extra.
            """
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            try:
                since = int(request.args.get("since", "0"))
            except ValueError:
                since = 0

            return jsonify({
                "notices": self.notices_since(since),
                "latest": self.latest_notice_id(),
                "run": self._run_id,
            })

        @app.post("/push/subscribe")
        def push_subscribe():
            """Register the phone's Web Push subscription."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            payload = request.get_json(silent=True) or {}
            subscription = payload.get("subscription")

            if not isinstance(subscription, dict):
                return jsonify({"error": "Invalid subscription."}), 400

            endpoint = subscription.get("endpoint")

            if not endpoint:
                return jsonify({"error": "Invalid subscription."}), 400

            with self._push_lock:
                # Replace an existing subscription from this browser
                # rather than accumulating duplicates.
                self._push_subscriptions = [
                    item
                    for item in self._push_subscriptions
                    if item.get("endpoint") != endpoint
                ]

                self._push_subscriptions.append(subscription)
                self._save_push_locked()

            return jsonify({"ok": True})

        @app.post("/push/unsubscribe")
        def push_unsubscribe():
            """Remove a phone's Web Push subscription."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            payload = request.get_json(silent=True) or {}
            endpoint = payload.get("endpoint")

            if not endpoint:
                return jsonify({"error": "Invalid subscription."}), 400

            with self._push_lock:
                self._push_subscriptions = [
                    item
                    for item in self._push_subscriptions
                    if item.get("endpoint") != endpoint
                ]
                self._save_push_locked()

            return jsonify({"ok": True})

        @app.post("/phone-active")
        def phone_active():
            """Tell the desktop whether the phone owns the voice channel."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            payload = request.get_json(silent=True) or {}
            active = bool(payload.get("active"))

            set_phone_active(active)

            return jsonify({"ok": True, "active": active})

        @app.post("/askfiles/organise")
        def askfiles_organise():
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            payload = request.get_json(silent=True) or {}

            current_path = str(payload.get("current_path") or "").strip()
            current_folder = str(payload.get("current_folder") or "").strip()
            items = payload.get("items")
            existing_child_folders = payload.get("existing_child_folders")

            if not current_path or not isinstance(items, list):
                return jsonify({"error": "Invalid AskFiles folder context."}), 400

            if not isinstance(existing_child_folders, list):
                existing_child_folders = []

            if len(items) > MAX_ASKFILES_ORGANISE_ITEMS:
                return jsonify({
                    "error": (
                        "That folder is too large to organise in one pass "
                        f"(maximum {MAX_ASKFILES_ORGANISE_ITEMS} items)."
                    )
                }), 400

            try:
                plan = askfiles.plan_folder_organisation(
                    current_path=current_path,
                    current_folder=current_folder or "Current folder",
                    items=items,
                    existing_child_folders=existing_child_folders,
                )
            except Exception as error:
                print(f"[JARVIS] AskFiles organisation failed: {error}")

                return jsonify({
                    "error": "I couldn't plan that folder, sir."
                }), 500

            return jsonify(plan)

        @app.post("/command")
        def command():
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            if not self._handler:
                return jsonify({
                    "error": "JARVIS is still starting up. Try again."
                }), 503

            payload = request.get_json(silent=True) or {}
            text = (payload.get("text") or "").strip()

            if not text:
                return jsonify({"error": "Nothing to do."}), 400

            try:
                reply = self._handler(text)
            except Exception as error:
                print(f"[JARVIS] phone command failed: {error}")

                return jsonify({
                    "error": "Something went wrong running that."
                }), 500

            # Phone actions return a structured result so the page can open
            # tel:, sms: or WhatsApp links. Normal commands still return a
            # plain string.
            if isinstance(reply, dict):
                return jsonify({
                    "heard": text,
                    "reply": reply.get("spoken", ""),
                    "link": reply.get("link"),
                    **self._camera_or_grid(text),
                })

            if reply is None:
                reply = ""
            elif not isinstance(reply, str):
                reply = str(reply)

            return jsonify({
                "heard": text,
                "reply": reply,
                **self._camera_or_grid(text),
            })

        @app.post("/voice")
        def voice():
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            if not self._handler:
                return jsonify({
                    "error": "JARVIS is still starting up. Try again."
                }), 503

            content_type = (
                request.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .casefold()
            )

            if content_type != "audio/wav":
                return jsonify({
                    "error": "Phone voice must be sent as WAV audio."
                }), 400

            audio = request.get_data()

            if not audio:
                return jsonify({"error": "No audio received."}), 400

            # A recording too short to contain a command. A stray tap
            # still captures a fraction of a second of room noise,
            # which transcribes into something -- so length is checked
            # before anything is transcribed at all.
            if len(audio) < MIN_VOICE_BYTES:
                return jsonify({
                    "error": "That was too short to be a command, sir."
                }), 400

            set_phone_active(True)

            try:
                text = transcriber.transcribe_wav(audio)

                if not text:
                    return jsonify({
                        "error": "I couldn't make out what you said."
                    }), 400

                # The same filter the desk microphone applies. A very
                # short recording reliably transcribes into one short
                # word -- "the", "you", "a" -- and since no fast-path
                # phrase matches it, it would otherwise fall through to
                # the language model: a long wait and a real cost for a
                # command nobody gave. FILLERS is imported rather than
                # redefined so the phone and the desk cannot drift
                # apart on what counts as noise.
                words = text.split()

                if len(words) == 1 and words[0].casefold() in FILLERS:
                    print(f'[filtered] "{text}" (single filler word, phone)')

                    return jsonify({
                        "error": "I only caught a stray word, sir."
                    }), 400

                reply = self._handler(text)

                # Phone actions return a structured result so the page can open
                # tel:, sms: or WhatsApp links. Normal commands still return a
                # plain string.
                if isinstance(reply, dict):
                    return jsonify({
                        "heard": text,
                        "reply": reply.get("spoken", ""),
                        "link": reply.get("link"),
                        **self._camera_or_grid(text),
                    })

                if reply is None:
                    reply = ""
                elif not isinstance(reply, str):
                    reply = str(reply)

                return jsonify({
                    "heard": text,
                    "reply": reply,
                    **self._camera_or_grid(text),
                })

            except Exception as error:
                print(f"[JARVIS] phone voice failed: {error}")

                return jsonify({
                    "error": "I couldn't process that voice command."
                }), 500

        @app.post("/audio")
        def audio():
            """The spoken form of a reply, as MP3, for the phone to play.

            Synthesised on demand rather than returned with the command
            itself, so the text appears immediately and the voice
            follows -- a reply is readable long before it is speakable.
            Shares speak()'s cache, so a phrase JARVIS already says
            often costs nothing here.
            """
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            payload = request.get_json(silent=True) or {}
            text = (payload.get("text") or "").strip()

            if not text:
                return jsonify({"error": "Nothing to say."}), 400

            try:
                data = speech.audio_bytes(text)
            except Exception as error:
                print(f"[JARVIS] phone audio failed: {error}")
                data = None

            if not data:
                return jsonify({"error": "No audio."}), 503

            return data, 200, {
                "Content-Type": "audio/mpeg",
                "Cache-Control": "no-store",
            }

    @staticmethod
    def _alarm_ttl():
        """Seconds an alarm push may wait for the phone: JARVIS_ALARM_PUSH_SECONDS, else ALARM_TTL_SECONDS."""
        try:
            seconds = int(os.getenv(ALARM_TTL_ENV) or ALARM_TTL_SECONDS)
        except ValueError:
            seconds = ALARM_TTL_SECONDS

        return max(0, seconds)

    @staticmethod
    def _push_allowed():
        """Whether an announcement is allowed to buzz the phone."""
        from actions.watch import quiet_hours

        return not quiet_hours()

    @staticmethod
    def _certificate_pair():
        """(cert, key) to serve with, and whether they were supplied.

        A supplied pair is used as-is and never regenerated -- these
        are issued by a real authority and JARVIS has no business
        touching them.
        """
        cert = os.getenv(CERT_ENV)
        key = os.getenv(KEY_ENV)

        if cert and key:
            if os.path.exists(cert) and os.path.exists(key):
                return cert, key, True

            print(
                f"[JARVIS] {CERT_ENV}/{KEY_ENV} are set but the files "
                "are missing; falling back to the local certificate."
            )

        return CERT_FILE, KEY_FILE, False

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._load_push()

        cert, key, supplied = self._certificate_pair()

        # JARVIS's own pair is kept up to date even when a supplied one is
        # served: sensor boards connect by address and check against
        # JARVIS's own authority, which a certificate issued for a name
        # cannot satisfy. phone_tls gives it to callers that name no host.
        local_ready = ensure_certificate()

        if not supplied and not local_ready:
            print(
                "[JARVIS] phone server not started because HTTPS "
                "certificate setup failed."
            )
            return

        try:
            context = phone_tls.serving_context(
                cert, key, (CERT_FILE, KEY_FILE) if supplied and local_ready else None)
        except (OSError, ssl.SSLError) as error:
            print(f"[JARVIS] phone server not started: its certificate could not be loaded ({error})")
            return

        try:
            # 0.0.0.0 so the phone can reach it over the local network.
            # HTTPS is required by mobile browsers for microphone access.
            self._server = _TidyServer(
                "0.0.0.0",
                self.port,
                self._app,
                ssl_context=context,
            )
        except OSError as error:
            print(
                f"[JARVIS] could not start the phone server on port "
                f"{self.port}: {error}"
            )
            return

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()

        print(f"[JARVIS] phone access ready at {self.url}")

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass


SERVICE_WORKER = r"""
self.addEventListener("push", event => {
    let data = {
        title: "JARVIS",
        body: "JARVIS has something to tell you."
    };

    try {
        data = event.data ? event.data.json() : data;
    } catch (error) {
        // Fall back to the default notification.
    }

    // An alarm (movement at home while you are away) has its own tag, so
    // a market notice arriving after it cannot replace it, and stays on
    // screen until it is dealt with.
    const options = {
        body: data.body || "",
        icon: "/?t=__TOKEN__",
        badge: "/?t=__TOKEN__",
        tag: data.alarm ? "jarvis-alarm" : "jarvis-notice",
        renotify: true,
        data: { camera: data.camera || null },
    };

    if (data.alarm) {
        options.requireInteraction = true;
        options.vibrate = [400, 200, 400, 200, 400];
    }

    event.waitUntil(
        self.registration.showNotification(data.title || "JARVIS", options)
    );
});

self.addEventListener("notificationclick", event => {
    event.notification.close();

    // An alarm opens the camera in that room: in the page if it is open,
    // or a new one told which camera to show.
    const camera = (event.notification.data || {}).camera;
    let url = "/?t=__TOKEN__";

    if (camera) {
        url += "&watch=" + encodeURIComponent(camera.name) +
            "&title=" + encodeURIComponent(camera.title || "");
    }

    event.waitUntil(
        clients.matchAll({
            type: "window",
            includeUncontrolled: true
        }).then(existing => {
            for (const client of existing) {
                if ("focus" in client) {
                    return client.focus().then(focused => {
                        if (camera) (focused || client).postMessage({ watch: camera });
                    });
                }
            }

            return clients.openWindow(url);
        })
    );
});
"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1,
      maximum-scale=1, user-scalable=no">
<meta name="theme-color" content="#070c13">
<title>JARVIS</title>
<style>
  :root {
    --ink: #e8f3fc;
    --dim: #7d97ad;
    --idle: #5fa5cd;
    --speaking: #5fffc3;
    --thinking: #ffb441;
    --bad: #ff6e6e;
    --panel: rgba(13, 22, 32, 0.92);
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0; height: 100%;
    background: #070c13;
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  body { display: flex; flex-direction: column; }
  header {
    padding: 14px 18px calc(14px + env(safe-area-inset-top));
    padding-top: calc(14px + env(safe-area-inset-top));
    border-bottom: 1px solid rgba(95, 200, 245, 0.18);
    display: flex; align-items: center; gap: 10px;
  }
  .dot {
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--bad); flex: none;
    transition: background 0.3s;
  }
  .dot.live { background: var(--speaking); }
  .dot.busy { background: var(--thinking); }
  h1 {
    font-size: 15px; letter-spacing: 3px; margin: 0;
    font-weight: 600; color: var(--idle);
  }
  #state { margin-left: auto; font-size: 12px; color: var(--dim); }
  #mute {
    background: none; border: none; padding: 0 0 0 12px;
    font-size: 17px; color: var(--ink); line-height: 1;
    filter: grayscale(0);
  }
  #mute.off { filter: grayscale(1); opacity: 0.4; }
  #locate {
    background: none; border: none; padding: 0 0 0 10px;
    font-size: 16px; color: var(--ink); line-height: 1;
  }
  #locate.sent { filter: none; opacity: 1; }
  #locate.idle { opacity: 0.45; }
  #notify {
    background: none;
    border: none;
    padding: 0 0 0 12px;
    font-size: 17px;
    color: var(--ink);
    line-height: 1;
  }
  #camera {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 50;
  background: #000;
  flex-direction: column;
}

#camera.open {
  display: flex;
}

/* A wifi camera's live view, from home or away: full screen, like the phone's own camera. */
#feed {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 60;
  background: #000;
  flex-direction: column;
}

#feed.open {
  display: flex;
}

#feed-top {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: calc(12px + env(safe-area-inset-top)) 16px 12px;
  color: var(--ink);
  letter-spacing: 3px;
  font-size: 13px;
}

#feed-title {
  flex: 1;
}

#feed-close {
  background: none;
  color: var(--ink);
  font-size: 22px;
  padding: 0 6px;
}

#feed-img {
  flex: 1;
  min-height: 0;
  width: 100%;
  object-fit: contain;
}

/* Highlighted things, drawn over the feed (actions/highlight.py): laid over the picture by script. */
#feed-marks {
  position: absolute;
  pointer-events: none;
  display: none;
}

#feed-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
}

/* Every camera at once: a tile each, tapped to fill the screen; back returns to them all. */
#feed-list.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  align-content: start;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}

.feed-tile {
  position: relative;
  background: #05080d;
  border: 1px solid rgba(95, 200, 245, 0.35);
  border-radius: 10px;
  overflow: hidden;
  padding: 0;
  aspect-ratio: 4 / 3;
}

.feed-tile img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.feed-tile span {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  padding: 6px 10px;
  background: rgba(8, 14, 21, 0.75);
  color: var(--ink);
  font-size: 11px;
  letter-spacing: 2px;
  text-align: left;
}

#feed-back {
  display: none;
  background: none;
  color: var(--ink);
  font-size: 15px;
  letter-spacing: 2px;
  padding: 0 6px;
}

#feed-back.shown {
  display: inline-block;
}

#feed-note {
  color: var(--dim);
  text-align: center;
  font-size: 13px;
  padding: 12px 12px calc(16px + env(safe-area-inset-bottom));
}

#preview {
  flex: 1;
  width: 100%;
  object-fit: cover;
  min-height: 0;
}

#camera-bar {
  display: flex;
  align-items: center;
  justify-content: space-around;
  padding: 18px 12px calc(18px + env(safe-area-inset-bottom));
  background: #000;
}

#camera-bar button {
  background: none;
  border: none;
  color: var(--ink);
  font-size: 22px;
  padding: 8px 18px;
  border-radius: 50%;
}

#shutter {
  font-size: 46px !important;
  color: var(--ink);
  line-height: 1;
  padding: 0 !important;
}

#camera-ask.recording {
  color: var(--bad);
}

#camera-hint {
  position: absolute;
  bottom: 92px;
  width: 100%;
  text-align: center;
  color: var(--dim);
  font-size: 12px;
  pointer-events: none;
}
  #reactor-wrap {
    position: relative; flex: none;
    height: 190px; display: flex; align-items: center;
    justify-content: center;
    border-bottom: 1px solid rgba(95, 200, 245, 0.12);
  }
  #reactor { width: 190px; height: 190px; }
  #reactor-label {
    position: absolute; bottom: 10px;
    font-size: 10px; letter-spacing: 3px; color: var(--dim);
  }
  /* On a short screen the reactor gives way to the conversation --
     it is decoration, and the words are the point. */
  @media (max-height: 620px) {
    #reactor-wrap { height: 128px; }
    #reactor { width: 128px; height: 128px; }
  }
  #log {
    flex: 1; overflow-y: auto; padding: 16px;
    display: flex; flex-direction: column; gap: 12px;
    -webkit-overflow-scrolling: touch;
  }
  .turn { max-width: 85%; padding: 11px 14px; border-radius: 14px;
          font-size: 15px; line-height: 1.45; white-space: pre-wrap;
          word-wrap: break-word; }
  .me { align-self: flex-end; background: rgba(95, 165, 205, 0.17);
        border: 1px solid rgba(95, 165, 205, 0.3); }
  .jarvis { align-self: flex-start; background: var(--panel);
            border: 1px solid rgba(95, 255, 195, 0.22); }
  .notice { align-self: stretch; background: rgba(255, 180, 65, 0.10);
            border: 1px solid rgba(255, 180, 65, 0.30);
            color: #ffe0ad; font-size: 14px; }
  .notice .when { color: var(--dim); font-size: 12px; margin-right: 8px; }
  .action {
    display: inline-block; padding: 12px 22px; border-radius: 22px;
    background: var(--idle); color: #04121c; font-weight: 600;
    text-decoration: none; font-size: 16px;
  }
  .oops { align-self: flex-start; background: rgba(255, 110, 110, 0.12);
          border: 1px solid rgba(255, 110, 110, 0.35); color: #ffc9c9; }
  .hint { color: var(--dim); font-size: 13px; text-align: center;
          padding: 24px 12px; line-height: 1.6; }
  footer {
    padding: 12px 12px calc(12px + env(safe-area-inset-bottom));
    border-top: 1px solid rgba(95, 200, 245, 0.18);
    display: flex; gap: 9px; background: #070c13;
  }
  input {
    flex: 1; min-width: 0; padding: 13px 15px; font-size: 16px;
    border-radius: 22px; border: 1px solid rgba(95, 200, 245, 0.32);
    background: rgba(10, 18, 27, 0.9); color: var(--ink);
    outline: none;
  }
  input:focus { border-color: var(--idle); }
  button {
    padding: 0 20px; font-size: 15px; font-weight: 600;
    border-radius: 22px; border: none; background: var(--idle);
    color: #04121c; flex: none;
  }
  button:disabled { opacity: 0.4; }
  #mic {
  -webkit-user-select: none;
  user-select: none;
  -webkit-touch-callout: none;
  width: 48px;
  padding: 0;
  font-size: 18px;
  background: rgba(95, 165, 205, 0.16);
  color: var(--ink);
  border: 1px solid rgba(95, 165, 205, 0.32);
}

#mic.recording {
  background: var(--bad);
  color: white;
}
</style>
</head>
<body>
  <header>
    <span class="dot" id="dot"></span>
    <h1>JARVIS</h1>
    <span id="state">connecting</span>
    <button id="notify" title="notifications">🔔</button>
    <button id="mute" title="voice">&#128266;</button>
    <button id="locate" title="send my location">&#128205;</button>
    <button id="camera-open" title="look at something">&#128247;</button>
    <button id="feeds-open" title="your cameras">&#127909;</button>
  </header>

  <div id="feed">
    <div id="feed-top">
      <button id="feed-back" type="button" title="all cameras">&#8249; BACK</button>
      <span id="feed-title">CAMERAS</span>
      <button id="feed-close" type="button" title="close">&#10005;</button>
    </div>
    <img id="feed-img" alt="">
    <canvas id="feed-marks"></canvas>
    <div id="feed-list"></div>
    <div id="feed-note"></div>
  </div>

  <div id="camera">
    <video id="preview" playsinline autoplay muted></video>

    <div id="camera-bar">
      <button id="camera-close" type="button" title="close">&#10005;</button>
      <button id="shutter" type="button" title="look">&#9679;</button>
      <button id="camera-ask" type="button" title="ask about this">&#127908;</button>
    </div>

    <div id="camera-hint">
      Tap the circle, or the microphone to ask
    </div>
  </div>

  <div id="reactor-wrap">
    <canvas id="reactor"></canvas>
    <div id="reactor-label">STANDBY</div>
  </div>

  <div id="log">
    <div class="hint" id="hint">
      Type a command, sir.<br>
      Anything you could say at the desk.
    </div>
  </div>

  <footer>
    <input id="text" placeholder="what's the weather" autocomplete="off"
          autocapitalize="off" enterkeyhint="send">
    <button id="mic" type="button" title="hold to speak">🎙</button>
    <button id="send">Send</button>
  </footer>

<script>
const TOKEN = "__TOKEN__";
const VAPID_PUBLIC_KEY = "__VAPID_PUBLIC_KEY__";
const log = document.getElementById("log");
const box = document.getElementById("text");
const send = document.getElementById("send");
const mic = document.getElementById("mic");
const dot = document.getElementById("dot");
const state = document.getElementById("state");
const hint = document.getElementById("hint");
const cameraPanel = document.getElementById("camera");
const preview = document.getElementById("preview");
const cameraOpen = document.getElementById("camera-open");
const cameraClose = document.getElementById("camera-close");
const shutter = document.getElementById("shutter");
const cameraAsk = document.getElementById("camera-ask");

let cameraStream = null;
let askingAboutFrame = null;

let alive = false;
let voiceOn = true;

let pushRegistration = null;

async function registerPushWorker() {
  if (!("serviceWorker" in navigator)) {
    return false;
  }

  try {
    pushRegistration = await navigator.serviceWorker.register(
      "/service-worker.js"
    );

    await navigator.serviceWorker.ready;

    return true;
  } catch (error) {
    console.error("[JARVIS] service worker registration failed:", error);
    return false;
  }
}

function urlBase64ToUint8Array(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding)
    .replace(/-/g, "+")
    .replace(/_/g, "/");

  const raw = atob(base64);

  return Uint8Array.from(
    [...raw].map(char => char.charCodeAt(0))
  );
}

async function enableNotifications() {
  if (!("Notification" in window)) {
    add(
      "This browser does not support notifications, sir.",
      "oops"
    );
    return;
  }

  if (!("PushManager" in window)) {
    add(
      "This browser does not support push notifications, sir.",
      "oops"
    );
    return;
  }

  if (!VAPID_PUBLIC_KEY) {
    add(
      "Push notifications are not configured on JARVIS yet, sir.",
      "oops"
    );
    return;
  }

  try {
    const permission = await Notification.requestPermission();

    if (permission !== "granted") {
      add(
        "Notifications were not enabled, sir.",
        "oops"
      );
      return;
    }

    const registration = pushRegistration
      || await navigator.serviceWorker.ready;

    pushRegistration = registration;

    let subscription = await registration.pushManager.getSubscription();

    if (!subscription) {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(
          VAPID_PUBLIC_KEY
        )
      });
    }

    const response = await fetch(
      "/push/subscribe?t=" + encodeURIComponent(TOKEN),
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          subscription: subscription.toJSON()
        })
      }
    );

    if (!response.ok) {
      throw new Error("JARVIS rejected the push subscription.");
    }

    add(
      "Push notifications are enabled, sir.",
      "jarvis"
    );

    notify.textContent = "🔔";
    notify.title = "notifications enabled";

  } catch (error) {
    console.error("[JARVIS] notification setup failed:", error);

    add(
      "I couldn't enable notifications, sir.",
      "oops"
    );
  }
}

// States the health poll must not overwrite, because something is
// genuinely happening. Kept as one list so a new state cannot be added
// later and silently get stamped over a few seconds in.
const BUSY_STATES = new Set(["thinking", "speaking", "listening"]);

// How far this device has read. Starting at 0 on a device that has
// never connected is deliberate: everything still held gets shown, so
// opening the page for the first time after being out still tells you
// what you missed.
let lastNoticeId = 0;
let noticeRun = null;

try {
  lastNoticeId = parseInt(localStorage.getItem("jarvisLastNotice") || "0", 10) || 0;
  noticeRun = localStorage.getItem("jarvisNoticeRun");
} catch (e) {
  lastNoticeId = 0;
  noticeRun = null;
}
let recording = false;

// Speaking is what starts and ends a recording, not the button. Above
// this level counts as speech; the numbers are in milliseconds.
const SPEECH_LEVEL = 0.02;
const SILENCE_AFTER_SPEECH = 1200;   // end of a sentence
const SILENCE_BEFORE_SPEECH = 2000;  // a tap with nothing behind it

let heardSpeech = false;
let lastSoundAt = 0;
let startedAt = 0;
let audioContext = null;
let inputNode = null;
let processor = null;
let micStream = null;
let recordedChunks = [];

async function setPhoneActive(active) {
  try {
    const response = await fetch(
      "/phone-active?t=" + encodeURIComponent(TOKEN),
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ active: active })
      }
    );

    return response.ok;
  } catch (error) {
    console.error("[JARVIS] phone-active failed:", error);
    return false;
  }
}

// One audio element reused for every reply. Mobile browsers refuse to
// play audio that wasn't started by a user gesture, and by the time a
// reply arrives the tap that sent it has long since "expired". The way
// round it is to start this element once during a real tap -- even with
// nothing loaded -- which marks it as user-initiated for the rest of
// the session.
const player = new Audio();
let unlocked = false;

function unlockAudio() {
  if (unlocked) return;
  unlocked = true;
  player.play().catch(() => {});
  player.pause();
}

// ---- reactor ----------------------------------------------------
// A canvas reimplementation of hud.py's arc reactor. Same geometry and
// the same palette, because the phone showing something different from
// the desk would be a second design to keep in step rather than one
// assistant seen from two places.
//
// The energy driving it is real throughout: the microphone's own level
// while recording, the reply's actual waveform while speaking, and a
// slow breath the rest of the time. Nothing here is a timer pretending
// to be activity.

const R_OUTER = 108, R_TICKS = 96;
const R_RING1 = 84, R_RING2 = 68, R_RING3 = 52, R_CORE = 30;

const PALETTE = {
  idle:      [95, 165, 205],
  listening: [80, 215, 255],
  thinking:  [255, 180, 65],
  speaking:  [95, 255, 195]
};

const canvas = document.getElementById("reactor");
const ctx2d = canvas.getContext("2d");

let hudState = "idle";
let energy = 0;        // 0..1, what the rings actually react to
let target = 0;        // where energy is heading
let sweep = 0;         // rotating arc position
let phase = 0;         // slow idle breath
let triangleSpin = 0;

// Reading the real output while it plays, so the rings move with the
// voice rather than to a guess about how long it might last.
let analyser = null;
let analyserData = null;

function setHudState(next) {
  hudState = PALETTE[next] ? next : "idle";
}

function setEnergy(value) {
  target = Math.max(0, Math.min(1, value));
}

function rgba(colour, alpha) {
  return "rgba(" + colour[0] + "," + colour[1] + "," + colour[2] + "," + alpha + ")";
}

function sizeCanvas() {
  // Drawn at the HUD's own coordinates and scaled to fit, so the
  // proportions survive whatever width the phone happens to be.
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;

  canvas.width = width * ratio;
  canvas.height = height * ratio;

  const scale = Math.min(width, height) / (R_OUTER * 2 + 16);

  ctx2d.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx2d.translate(width / 2, height / 2);
  ctx2d.scale(scale, scale);
}

function arc(radius, startDeg, spanDeg, colour, alpha, width) {
  ctx2d.beginPath();
  ctx2d.strokeStyle = rgba(colour, alpha);
  ctx2d.lineWidth = width;
  ctx2d.arc(
    0, 0, radius,
    (startDeg * Math.PI) / 180,
    ((startDeg + spanDeg) * Math.PI) / 180
  );
  ctx2d.stroke();
}

function drawTicks(colour) {
  // 72 ticks, every sixth one major -- the same as the desk.
  for (let index = 0; index < 72; index++) {
    const major = index % 6 === 0;
    const angle = (index * 5 * Math.PI) / 180;
    const length = major ? 10 : 5;

    ctx2d.beginPath();
    ctx2d.strokeStyle = rgba(colour, major ? 0.69 : 0.33);
    ctx2d.lineWidth = major ? 1.6 : 1.0;
    ctx2d.moveTo(Math.cos(angle) * R_TICKS, Math.sin(angle) * R_TICKS);
    ctx2d.lineTo(
      Math.cos(angle) * (R_TICKS + length),
      Math.sin(angle) * (R_TICKS + length)
    );
    ctx2d.stroke();
  }
}

function drawTriangle(colour, alpha, rotation, radius) {
  ctx2d.save();
  ctx2d.rotate(rotation);

  ctx2d.strokeStyle = rgba(colour, alpha);
  ctx2d.lineWidth = 1.4;

  for (let angle = 0; angle < 360; angle += 120) {
    ctx2d.save();
    ctx2d.rotate((angle * Math.PI) / 180);

    ctx2d.beginPath();
    ctx2d.moveTo(0, -radius * 0.78);
    ctx2d.lineTo(radius * 0.62, radius * 0.42);
    ctx2d.lineTo(-radius * 0.62, radius * 0.42);
    ctx2d.closePath();
    ctx2d.stroke();

    ctx2d.restore();
  }

  ctx2d.restore();
}

function drawFrame() {
  const colour = PALETTE[hudState];

  // Ease toward the target rather than jumping, so the rings settle
  // instead of flickering on every reading.
  energy += (target - energy) * (target > energy ? 0.35 : 0.08);
  sweep = (sweep + 1.1) % 360;
  phase += 0.02;
  const triangleSpeed =
  hudState === "thinking" ? 0.085 :
  hudState === "speaking" ? 0.035 :
  0.010;

triangleSpin = (triangleSpin + triangleSpeed) % (Math.PI * 2);

  // Idle has no input of its own, so it breathes slowly rather than
  // sitting perfectly still, which reads as switched off.
  const breath = hudState === "idle"
    ? 0.10 + 0.05 * Math.sin(phase)
    : 0;
  const level = Math.max(energy, breath);

  const width = canvas.clientWidth;
  const height = canvas.clientHeight;

  ctx2d.save();
  ctx2d.setTransform(1, 0, 0, 1, 0, 0);
  ctx2d.clearRect(0, 0, canvas.width, canvas.height);
  ctx2d.restore();

  drawTicks(colour);

  // Outer boundary.
  arc(R_OUTER, 0, 360, colour, 0.22, 1.2);

  // Two counter-rotating broken rings, which is what makes it read as
  // running rather than drawn.
  arc(R_RING1, sweep, 110, colour, 0.55 + level * 0.35, 2.0);
  arc(R_RING1, sweep + 180, 60, colour, 0.30 + level * 0.30, 2.0);
  arc(R_RING2, -sweep * 0.7, 80, colour, 0.45 + level * 0.35, 1.8);
  arc(R_RING2, -sweep * 0.7 + 140, 40, colour, 0.25, 1.8);
  arc(R_RING3, sweep * 1.3, 150, colour, 0.35 + level * 0.4, 1.5);

  // The core: a filled glow that swells with whatever is happening.
  const coreRadius =
  hudState === "speaking"
    ? R_CORE * (0.72 + level * 0.65)
    : R_CORE * (0.72 + level * 0.42);
  const glow = ctx2d.createRadialGradient(0, 0, 0, 0, 0, coreRadius * 2.2);

    glow.addColorStop(
    0,
    rgba(
      colour,
      hudState === "speaking"
        ? 0.75 + level * 0.55
        : 0.55 + level * 0.4
    )
  );

  glow.addColorStop(
    0.5,
    rgba(
      colour,
      hudState === "speaking"
        ? 0.24 + level * 0.18
        : 0.16
    )
  );
  glow.addColorStop(1, rgba(colour, 0));

  ctx2d.beginPath();
  ctx2d.fillStyle = glow;
  ctx2d.arc(0, 0, coreRadius * 2.2, 0, Math.PI * 2);
  ctx2d.fill();

  ctx2d.beginPath();
  ctx2d.fillStyle = rgba([235, 250, 255], 0.55 + level * 0.45);
  ctx2d.arc(0, 0, coreRadius * 0.42, 0, Math.PI * 2);
  ctx2d.fill();

  const triangleAlpha =
  hudState === "speaking"
    ? 45 + level * 35
    : 150 + level * 70;

  drawTriangle(
    colour,
    triangleAlpha,
    triangleSpin,
    coreRadius * 0.95
  );

  arc(coreRadius, 0, 360, colour, 0.8, 1.6);

  // While the reply plays, take the level from the audio itself.
  if (analyser && hudState === "speaking") {
    analyser.getByteFrequencyData(analyserData);

    let total = 0;
    for (let i = 0; i < analyserData.length; i++) total += analyserData[i];

    setEnergy(Math.min(1, (total / analyserData.length) / 90));
  }

  requestAnimationFrame(drawFrame);
}

// Wiring the player through an analyser so "speaking" is driven by the
// actual audio. Built lazily on first use: an AudioContext created
// before a user gesture starts suspended on mobile and never recovers.
function attachAnalyser() {
  if (analyser || !window.AudioContext) return;

  try {
    const context = new AudioContext();
    const source = context.createMediaElementSource(player);

    analyser = context.createAnalyser();
    analyser.fftSize = 128;
    analyserData = new Uint8Array(analyser.frequencyBinCount);

    source.connect(analyser);
    analyser.connect(context.destination);
  } catch (e) {
    // Without it the reply still plays; the rings just use a steady
    // level instead of the real waveform.
    analyser = null;
  }
}

window.addEventListener("resize", sizeCanvas);
sizeCanvas();
requestAnimationFrame(drawFrame);


async function speak(text, preloadedBlob = null) {
  if (!voiceOn || !text) return;

  // Only reset the state at the end if we actually set it, so a
  // failed fetch cannot stamp "ready" over a state it never touched.
  let speaking = false;

  try {
    let blob = preloadedBlob;

    if (!blob) {
      const res = await fetch("/audio?t=" + encodeURIComponent(TOKEN), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text })
      });

      if (!res.ok) return;

      blob = await res.blob();
    }
    if (player.src) URL.revokeObjectURL(player.src);
    player.src = URL.createObjectURL(blob);

    attachAnalyser();
    speaking = true;
    setState("busy", "speaking");

    // Resolves when playback FINISHES, not when it starts.
    // player.play() resolves as soon as audio begins, so awaiting it
    // returned before a single word had been heard -- which let the
    // caller reset the state to "ready" over the top of "speaking",
    // and on the voice path released the PC microphone while JARVIS
    // was still talking.
    await new Promise((resolve) => {
      let finished = false;

      const done = () => {
        if (finished) return;
        finished = true;
        resolve();
      };

      player.onended = done;
      player.onerror = done;

      // A reply that somehow never fires 'ended' must not leave the
      // page stuck on "speaking" for ever.
      const guard = setTimeout(done, 120000);
      const clearGuard = () => clearTimeout(guard);

      player.addEventListener("ended", clearGuard, { once: true });
      player.addEventListener("error", clearGuard, { once: true });

      player.play().catch(done);
    });

  } catch (e) {
    // No voice is a small loss when the text is already on screen.
  } finally {
    // speak() sets the speaking state, so speak() clears it -- in a
    // finally, so no early return or failure can skip it. Leaving this
    // to each caller meant one that forgot (collectNotices) left the
    // reactor stuck on "speaking" indefinitely, and since the health
    // poll deliberately does not overwrite a busy state, nothing was
    // left to rescue it.
    if (speaking) setState("live", "ready");
  }
}

function setState(kind, label) {
  dot.className = "dot" + (kind ? " " + kind : "");
  state.textContent = label;

  // The reactor follows the same state the header shows, so there is
  // one source of truth rather than two things to keep in step.
  const mapped = label === "listening" ? "listening"
               : label === "thinking" ? "thinking"
               : label === "speaking" ? "speaking"
               : "idle";

  setHudState(mapped);
  document.getElementById("reactor-label").textContent =
    mapped === "listening" ? "LISTENING"
    : mapped === "thinking" ? "PROCESSING"
    : mapped === "speaking" ? "SPEAKING"
    : "STANDBY";

  if (mapped === "idle" || mapped === "thinking") setEnergy(0);
}

// Opening a dialler, a chat or a text. Tried directly first, because
// when the browser allows it that is one fewer tap -- but a link
// followed after an async reply often looks like an unsolicited
// redirect and is refused, and there is no way to ask beforehand. So a
// button always appears too: if the direct attempt worked you never
// look at it, and if it didn't, the action is still one tap away
// rather than lost.
function offerLink(link, label) {
  if (hint) hint.remove();

  const row = document.createElement("div");
  row.className = "turn jarvis";

  const button = document.createElement("a");
  button.className = "action";
  button.href = link;
  button.textContent = label;
  button.rel = "noopener";

  row.appendChild(button);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;

  try {
    window.location.href = link;
  } catch (e) {
    // Refused; the button above is the fallback.
  }
}

function add(text, cls) {
  if (hint) hint.remove();
  const el = document.createElement("div");
  el.className = "turn " + cls;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

// Whether the PC is actually there. Polled rather than assumed, so
// closing JARVIS shows up as a clear message instead of a request
// that hangs and then fails with nothing useful.
async function ping() {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    const res = await fetch("/health", { signal: controller.signal });
    clearTimeout(timer);
    alive = res.ok;
  } catch (e) {
    alive = false;
  }
  if (!alive) {
    setState("", "JARVIS is not running");
  } else if (!BUSY_STATES.has(state.textContent)) {
    // Only reset when nothing is actually in progress. This poll runs
    // every few seconds, so anything it doesn't know about gets wiped
    // mid-flight -- which is exactly what happened to "speaking" when
    // a reply outlasted one interval.
    setState("live", "ready");
  }
  send.disabled = !alive;

  if (alive) collectNotices();
}

// Anything JARVIS announced on his own while you were elsewhere. Shown
// as it arrives, and spoken if voice is on -- a market alert you only
// read hours later is barely an alert at all.
async function collectNotices() {
  try {
    const res = await fetch(
      "/notices?t=" + encodeURIComponent(TOKEN) +
      "&since=" + encodeURIComponent(lastNoticeId)
    );
    if (!res.ok) return;

    const data = await res.json();

    // Notice ids restart from zero whenever JARVIS does, since the
    // queue is held in memory. A cursor kept from a previous run would
    // then be ahead of every new id -- asking for "anything after 5"
    // when the new run has only reached 1 -- and this device would
    // silently never see another notice. Spotting the change and
    // starting over is the whole reason the run id exists.
    if (data.run && data.run !== noticeRun) {
      noticeRun = data.run;
      lastNoticeId = 0;

      try {
        localStorage.setItem("jarvisNoticeRun", noticeRun);
        localStorage.setItem("jarvisLastNotice", "0");
      } catch (e) {
        // Storage refused; the reset still holds for this session.
      }

      // Re-ask from the beginning of this run, so the restart's own
      // announcements aren't lost to the poll that detected it.
      return collectNotices();
    }

    const notices = data.notices || [];

    if (notices.length) {
      lastNoticeId = notices[notices.length - 1].id;
      try {
        localStorage.setItem("jarvisLastNotice", String(lastNoticeId));
      } catch (e) {
        // Private browsing refuses storage; the cursor still works for
        // this session, a reload just replays what is still held.
      }
    }

    for (const notice of notices) {
      if (hint) hint.remove();
      const el = document.createElement("div");
      el.className = "turn notice";
      const when = document.createElement("span");
      when.className = "when";
      when.textContent = notice.at;
      el.appendChild(when);
      el.appendChild(document.createTextNode(notice.text));
      log.appendChild(el);
      log.scrollTop = log.scrollHeight;
    }

    // Only the most recent is spoken. Returning to a dozen queued
    // announcements should not mean sitting through all of them.
    if (notices.length) {
      speak(notices[notices.length - 1].text);
    }

    // An alarm that arrives while you are looking at the page shows its camera.
    const alarm = notices.filter(notice => notice.camera).pop();
    if (alarm && !document.hidden) openFeed(alarm.camera.name, alarm.camera.title);
  } catch (e) {
    // Missing a notice is not worth surfacing as an error.
  }
}

async function submit() {
  const text = box.value.trim();
  if (!text) return;

  if (!alive) {
    add("JARVIS isn't running on your PC, sir. Start it and try again.",
        "oops");
    return;
  }

  add(text, "me");
  box.value = "";
  send.disabled = true;
  setState("busy", "thinking");

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60000);

    const res = await fetch("/command?t=" + encodeURIComponent(TOKEN), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
      signal: controller.signal
    });
    clearTimeout(timer);

    const data = await res.json().catch(() => ({}));

    if (!res.ok) {
      add(data.error || "That didn't work, sir.", "oops");
    } else if (data.reply) {
      add(data.reply, "jarvis");

      if (data.sighting) {
        openSighting(data.sighting);
      } else if (data.cameras && data.cameras.length) {
        openGrid(data.cameras);
      } else if (data.camera) {
        openFeed(data.camera.name, data.camera.title);
      }

      if (data.link) {
        offerLink(
          data.link,
          data.reply.replace(/,? sir\\.?$/i, "")
        );
      }

      // Awaited, so "ready" is not set over the top of "speaking"
      // before the reply has actually been heard.
      await speak(data.reply);
    } else {
      add("Done, sir.", "jarvis");
    }
    setState("live", "ready");
  } catch (e) {
    // Aborted, or the PC went away mid-command. Either way the text
    // is already shown, so nothing typed is lost.
    add("No reply from your PC, sir. Is JARVIS still running?", "oops");
    setState("", "JARVIS is not running");
    alive = false;
  }

  send.disabled = !alive;
  box.focus();
}

const notify = document.getElementById("notify");
// Position is sent when asked for, never continuously. A browser stops
// reporting the moment the page is backgrounded or the screen locks, so
// a background watch would drain the battery for readings that stop
// arriving exactly when they would have been interesting.
const locate = document.getElementById("locate");
locate.classList.add("idle");

locate.addEventListener("click", () => {
  if (!navigator.geolocation) {
    add("This browser can't report a location, sir.", "oops");
    return;
  }

  locate.textContent = "⏳";

  navigator.geolocation.getCurrentPosition(
    async (position) => {
      try {
        const res = await fetch(
          "/location?t=" + encodeURIComponent(TOKEN),
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              latitude: position.coords.latitude,
              longitude: position.coords.longitude,
              accuracy: position.coords.accuracy
            })
          }
        );

        locate.textContent = "📍";

        if (res.ok) {
          locate.classList.remove("idle");
          locate.classList.add("sent");
          add("Location sent, sir.", "jarvis");
        } else {
          add("He couldn't use that position, sir.", "oops");
        }
      } catch (e) {
        locate.textContent = "📍";
        add("Couldn't send your location, sir.", "oops");
      }
    },
    (error) => {
      locate.textContent = "📍";
      add(
        error.code === error.PERMISSION_DENIED
          ? "Location permission was refused, sir."
          : "Couldn't get a location fix, sir.",
        "oops"
      );
    },
    { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 }
  );
});

const mute = document.getElementById("mute");
notify.addEventListener("click", () => {
  enableNotifications();
});
mute.addEventListener("click", () => {
  voiceOn = !voiceOn;
  mute.classList.toggle("off", !voiceOn);
  if (!voiceOn) player.pause();
  unlockAudio();
});

// ---- the wifi cameras' live views ------------------------------------------------------------

const feed = document.getElementById("feed");
const feedImg = document.getElementById("feed-img");
const feedTitle = document.getElementById("feed-title");
const feedList = document.getElementById("feed-list");
const feedNote = document.getElementById("feed-note");

const feedBack = document.getElementById("feed-back");

// The cameras the grid last showed, so BACK can open it again.
let gridCameras = [];

function watchUrl(name, fps) {
  return "/camera/watch?t=" + encodeURIComponent(TOKEN) + "&name=" + encodeURIComponent(name) +
    (fps ? "&fps=" + fps : "") + "&at=" + Date.now();
}

function stopTiles() {
  // Each tile's stream closes with its source, and JARVIS lets that camera stop when nothing watches it.
  feedList.querySelectorAll("img").forEach(img => img.removeAttribute("src"));
  feedList.innerHTML = "";
  feedList.classList.remove("grid");
}

function stopFeed() {
  stopMarks();
  // Without a source the stream's connection closes, and JARVIS lets the camera stop.
  feedImg.onload = null;
  feedImg.onerror = null;
  feedImg.removeAttribute("src");
  feedImg.style.display = "none";
  stopTiles();
}

// Every camera at once, a tile each at a few frames a second; tapping one opens it full screen.
function openGrid(listed) {
  gridCameras = listed.slice();
  stopFeed();
  feedImg.dataset.name = "";
  feedBack.classList.remove("shown");
  feedTitle.textContent = "CAMERAS";
  feedNote.textContent = "Tap a camera to make it big, sir.";
  feedList.classList.add("grid");

  listed.forEach(camera => {
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = "feed-tile";
    const img = document.createElement("img");
    img.alt = camera.title || camera.name;
    img.src = watchUrl(camera.name, 4);
    const label = document.createElement("span");
    label.textContent = camera.title || camera.name.toUpperCase();
    tile.appendChild(img);
    tile.appendChild(label);
    tile.addEventListener("click", () => openFeed(camera.name, camera.title, true));
    feedList.appendChild(tile);
  });

  feed.classList.add("open");
}

// ---- highlighted things over the feed: "highlight the bottle", asked of JARVIS --------------------

const feedMarks = document.getElementById("feed-marks");
let marksTimer = null;

function drawMarks(boxes) {
  // The picture's own rect inside the <img> (object-fit: contain leaves bars at the sides or top).
  const w = feedImg.clientWidth, h = feedImg.clientHeight;
  const nw = feedImg.naturalWidth, nh = feedImg.naturalHeight;

  if (!boxes.length || !w || !h || !nw || !nh || feedImg.style.display === "none") {
    feedMarks.style.display = "none";
    return;
  }

  const scale = Math.min(w / nw, h / nh);
  const pw = nw * scale, ph = nh * scale;
  const left = feedImg.offsetLeft + (w - pw) / 2, top = feedImg.offsetTop + (h - ph) / 2;
  const ratio = window.devicePixelRatio || 1;

  feedMarks.style.left = left + "px";
  feedMarks.style.top = top + "px";
  feedMarks.style.width = pw + "px";
  feedMarks.style.height = ph + "px";
  feedMarks.width = Math.round(pw * ratio);
  feedMarks.height = Math.round(ph * ratio);
  feedMarks.style.display = "block";

  const pen = feedMarks.getContext("2d");
  pen.setTransform(ratio, 0, 0, ratio, 0, 0);
  pen.clearRect(0, 0, pw, ph);
  pen.font = "bold 12px Consolas, monospace";

  boxes.forEach(item => {
    const [x1, y1, x2, y2] = item.box;
    let bx = x1 * pw, by = y1 * ph, bw = (x2 - x1) * pw, bh = (y2 - y1) * ph;
    if (bw < 2 || bh < 2) return;
    const line = item.outline || [];
    if (line.length >= 3) {
      // Its exact shape: the line hugs the thing, lightly filled.
      pen.beginPath();
      line.forEach(([x, y], i) => (i ? pen.lineTo(x * pw, y * ph) : pen.moveTo(x * pw, y * ph)));
      pen.closePath();
      pen.fillStyle = "rgba(255, 186, 72, 0.2)";
      pen.fill();
      pen.strokeStyle = "rgb(255, 186, 72)";
      pen.lineWidth = 2.2;
      pen.stroke();
      const xs = line.map(p => p[0] * pw), ys = line.map(p => p[1] * ph);
      bx = Math.min(...xs); by = Math.min(...ys);
      const text = item.label.toUpperCase() + " " + Math.round(item.score * 100) + "%";
      const tw = pen.measureText(text).width + 12;
      const ty = by - 18 < 0 ? by + 2 : by - 18;
      pen.fillStyle = "rgba(8, 14, 21, 0.82)";
      pen.fillRect(bx, ty, tw, 16);
      pen.fillStyle = "rgb(255, 186, 72)";
      pen.fillText(text, bx + 6, ty + 12);
      return;
    }
    pen.strokeStyle = "rgba(255, 186, 72, 0.45)";
    pen.lineWidth = 1;
    pen.strokeRect(bx, by, bw, bh);
    pen.strokeStyle = "rgb(255, 186, 72)";
    pen.lineWidth = 2.5;
    const span = Math.max(6, Math.min(18, bw / 4, bh / 4));
    [[bx, by, 1, 1], [bx + bw, by, -1, 1], [bx, by + bh, 1, -1], [bx + bw, by + bh, -1, -1]].forEach(([x, y, dx, dy]) => {
      pen.beginPath();
      pen.moveTo(x + span * dx, y);
      pen.lineTo(x, y);
      pen.lineTo(x, y + span * dy);
      pen.stroke();
    });
    const text = item.label.toUpperCase() + " " + Math.round(item.score * 100) + "%";
    const tw = pen.measureText(text).width + 12;
    const ty = by - 18 < 0 ? by + 2 : by - 18;
    pen.fillStyle = "rgba(8, 14, 21, 0.82)";
    pen.fillRect(bx, ty, tw, 16);
    pen.fillStyle = "rgb(255, 186, 72)";
    pen.fillText(text, bx + 6, ty + 12);
  });
}

async function pollMarks() {
  const name = feedImg.dataset.name;
  if (!name || !feed.classList.contains("open")) return;

  try {
    const res = await fetch("/camera/marks?t=" + encodeURIComponent(TOKEN) + "&name=" + encodeURIComponent(name));
    drawMarks(((await res.json()).boxes) || []);
  } catch (e) {
    drawMarks([]);
  }
}

function startMarks() {
  stopMarks();
  marksTimer = setInterval(pollMarks, 1000);
}

function stopMarks() {
  if (marksTimer) clearInterval(marksTimer);
  marksTimer = null;
  feedMarks.style.display = "none";
}

function openFeed(name, title, fromGrid) {
  // Already playing this camera (the alarm's tap and its notice both ask): leave it playing.
  if (feed.classList.contains("open") && feedImg.dataset.name === name && feedImg.getAttribute("src")) return;

  stopFeed();
  feedBack.classList.toggle("shown", !!fromGrid);
  feedImg.dataset.name = name;
  feedTitle.textContent = title || name.toUpperCase();
  feedNote.textContent = "Waking the camera, sir...";
  feedImg.style.display = "block";
  feedImg.onload = () => { feedNote.textContent = ""; };
  feedImg.onerror = () => { feedNote.textContent = "The camera stopped sending, sir."; };
  feedImg.src = watchUrl(name);
  feed.classList.add("open");
  startMarks();
}

// A sighting's best picture ("show me the last sighting"), full screen where a camera plays; the cross
// closes it as it closes a camera.
function openSighting(sighting) {
  stopFeed();
  feedBack.classList.remove("shown");
  feedImg.dataset.name = "";
  feedTitle.textContent = sighting.title || "SIGHTING";
  feedNote.textContent = "";
  feedImg.style.display = "block";
  feedImg.onload = null;
  feedImg.onerror = () => { feedNote.textContent = "I couldn't get that picture, sir."; };
  feedImg.src = sighting.url + (sighting.url.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(TOKEN)
    + "&at=" + Date.now();
  feed.classList.add("open");
}

function closeFeed() {
  stopFeed();
  feedBack.classList.remove("shown");
  feed.classList.remove("open");
}

feedBack.addEventListener("click", () => openGrid(gridCameras));

async function showCameras() {
  let listed = [];

  try {
    const res = await fetch("/cameras?t=" + encodeURIComponent(TOKEN));
    listed = ((await res.json()).cameras) || [];
  } catch (e) {
    add("I couldn't reach your PC for the cameras, sir.", "oops");
    return;
  }

  const online = listed.filter(camera => camera.online);

  if (!online.length) {
    add(listed.length ? "Your cameras are all offline, sir." : "No cameras are connected, sir.", "oops");
    return;
  }

  if (online.length === 1) {
    openFeed(online[0].name, (online[0].looks ? online[0].looks + " cam" : online[0].name).toUpperCase());
    return;
  }

  openGrid(online.map(camera => ({ name: camera.name, title: camera.name.toUpperCase() })));
}

document.getElementById("feeds-open").addEventListener("click", showCameras);

// An alarm tapped while this page was open: the service worker says which camera.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", event => {
    const camera = (event.data || {}).watch;
    if (camera && camera.name) openFeed(camera.name, camera.title);
  });
}

// An alarm tapped with no page open: the address says which camera, once.
(() => {
  const asked = new URLSearchParams(location.search);
  const name = asked.get("watch");
  if (!name) return;
  const title = asked.get("title");
  asked.delete("watch");
  asked.delete("title");
  history.replaceState(null, "", location.pathname + "?" + asked.toString());
  openFeed(name, title);
})();
document.getElementById("feed-close").addEventListener("click", closeFeed);

// A locked phone or a backgrounded tab is not watching: the stream ends, and the camera with it.
document.addEventListener("visibilitychange", () => {
  if (document.hidden && feed.classList.contains("open")) closeFeed();
});

async function openCamera() {
  if (!navigator.mediaDevices) {
    add("This browser won't give me the camera, sir.", "oops");
    return;
  }

  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" } },
      audio: false
    });

    preview.srcObject = cameraStream;
    cameraPanel.classList.add("open");
  } catch (e) {
    add(
      e && e.name === "NotAllowedError"
        ? "Camera permission was refused, sir."
        : "I couldn't open the camera, sir.",
      "oops"
    );
  }
}

function closeCamera() {
  cameraPanel.classList.remove("open");

  if (cameraStream) {
    cameraStream.getTracks().forEach(track => track.stop());
    cameraStream = null;
  }

  preview.srcObject = null;
  askingAboutFrame = null;
}

function grabFrame() {
  if (!preview.videoWidth) return null;

  const canvas = document.createElement("canvas");
  const width = Math.min(1280, preview.videoWidth);

  canvas.width = width;
  canvas.height = Math.round(
    preview.videoHeight * width / preview.videoWidth
  );

  canvas.getContext("2d").drawImage(
    preview,
    0,
    0,
    canvas.width,
    canvas.height
  );

  return canvas;
}

async function transcribeOnly(wav) {
  try {
    const res = await fetch(
      "/transcribe?t=" + encodeURIComponent(TOKEN),
      {
        method: "POST",
        headers: { "Content-Type": "audio/wav" },
        body: wav
      }
    );

    if (!res.ok) return "";

    const data = await res.json();

    return (data.heard || "").trim();
  } catch (e) {
    return "";
  }
}

async function fetchSpeechAudio(text) {
  try {
    const res = await fetch(
      "/audio?t=" + encodeURIComponent(TOKEN),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text })
      }
    );

    if (!res.ok) return null;

    return await res.blob();
  } catch (e) {
    return null;
  }
}

async function sendLook(canvas, question) {
  setState("busy", "thinking");

  const blob = await new Promise(resolve =>
    canvas.toBlob(resolve, "image/jpeg", 0.85)
  );

  if (!blob) {
    add("I couldn't read that picture, sir.", "oops");
    setState("live", "ready");
    return;
  }

  add(question || "What am I looking at?", "me");

  try {
    const res = await fetch(
      "/look?t=" + encodeURIComponent(TOKEN) +
      (question ? "&q=" + encodeURIComponent(question) : ""),
      {
        method: "POST",
        headers: { "Content-Type": "image/jpeg" },
        body: blob
      }
    );

    const data = await res.json().catch(() => ({}));

    if (!res.ok) {
      add(data.error || "I couldn't look at that, sir.", "oops");
    } else if (data.reply) {
      add(data.reply, "jarvis");

      const speechParts = Array.isArray(data.speak) && data.speak.length
        ? data.speak
        : [data.reply];

      // Fetch all TTS audio before starting playback.
      // This prevents a silence while the second sentence is being generated.
      const audioParts = await Promise.all(
        speechParts.map(part => fetchSpeechAudio(part))
      );

      for (let index = 0; index < speechParts.length; index++) {
        const part = (speechParts[index] || "").trim();

        if (!part) {
          continue;
        }

        await speak(part, audioParts[index]);
      }
    }
  } catch (e) {
    add("No reply from your PC, sir.", "oops");
  }

  setState("live", "ready");
}

cameraOpen.addEventListener("click", () => {
  unlockAudio();
  openCamera();
});

cameraClose.addEventListener("click", closeCamera);

shutter.addEventListener("click", async () => {
  const canvas = grabFrame();

  if (!canvas) return;

  closeCamera();
  await sendLook(canvas, "");
});

cameraAsk.addEventListener("click", async () => {
  if (recording) {
    cameraAsk.classList.remove("recording");
    await stopRecording();
    return;
  }

  askingAboutFrame = grabFrame();

  if (!askingAboutFrame) return;

  cameraAsk.classList.add("recording");
  unlockAudio();
  startRecording();
});

function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);

  function writeString(offset, value) {
    for (let i = 0; i < value.length; i++) {
      view.setUint8(offset + i, value.charCodeAt(i));
    }
  }

  writeString(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeString(36, "data");
  view.setUint32(40, samples.length * 2, true);

  let offset = 44;

  for (const sample of samples) {
    const clamped = Math.max(-1, Math.min(1, sample));

    view.setInt16(
      offset,
      clamped < 0
        ? clamped * 0x8000
        : clamped * 0x7fff,
      true
    );

    offset += 2;
  }

  return new Blob([buffer], { type: "audio/wav" });
}

function resample16k(samples, sourceRate) {
  const targetRate = 16000;

  if (sourceRate === targetRate) {
    return samples;
  }

  const ratio = sourceRate / targetRate;
  const outputLength = Math.max(
    1,
    Math.round(samples.length / ratio)
  );

  const output = new Float32Array(outputLength);

  for (let i = 0; i < outputLength; i++) {
    const position = i * ratio;
    const left = Math.floor(position);
    const right = Math.min(left + 1, samples.length - 1);
    const fraction = position - left;

    output[i] =
      samples[left] * (1 - fraction) +
      samples[right] * fraction;
  }

  return output;
}

function stopRecordingUI() {
  recording = false;

  mic.classList.remove("recording");
  mic.textContent = "🎙";

  send.disabled = !alive;
  box.disabled = false;

  if (processor) {
    processor.disconnect();
    processor.onaudioprocess = null;
    processor = null;
  }

  if (inputNode) {
    inputNode.disconnect();
    inputNode = null;
  }

  if (audioContext) {
    audioContext.close().catch(() => {});
    audioContext = null;
  }

  if (micStream) {
    micStream.getTracks().forEach(track => track.stop());
    micStream = null;
  }
}

async function startRecording() {
  if (recording || !alive) {
    return;
  }

  let phoneActive = false;

  try {
    // Claim the voice channel BEFORE opening the microphone.
    // This makes the PC microphone deaf during the entire interaction.
    phoneActive = await setPhoneActive(true);

    if (!phoneActive) {
      add(
        "I couldn't reserve the voice channel, sir.",
        "oops"
      );
      return;
    }

    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true
      }
    });

    audioContext = new AudioContext();

    await audioContext.resume();

    inputNode = audioContext.createMediaStreamSource(micStream);

    processor = audioContext.createScriptProcessor(
      4096,
      1,
      1
    );

    recordedChunks = [];
    recording = true;

    heardSpeech = false;
    startedAt = Date.now();
    lastSoundAt = startedAt;

    mic.classList.add("recording");
    mic.textContent = "■";

    send.disabled = true;
    box.disabled = true;

    setState("busy", "listening");

    processor.onaudioprocess = (event) => {
      if (!recording) {
        return;
      }

      const input = event.inputBuffer.getChannelData(0);

      recordedChunks.push(new Float32Array(input));

      // Drive the rings from what the microphone is actually hearing,
      // so the reactor responds to your voice rather than merely
      // showing that recording is switched on.
      let sum = 0;
      for (let i = 0; i < input.length; i++) sum += input[i] * input[i];

      const level = Math.sqrt(sum / input.length);
      setEnergy(level * 6);

      // Stop on your own silence rather than on a second tap. The
      // level is already being measured for the rings, so listening
      // for the end of a sentence costs nothing extra.
      const now = Date.now();

      if (level > SPEECH_LEVEL) {
        heardSpeech = true;
        lastSoundAt = now;
        return;
      }

      if (!heardSpeech) {
        // Nothing said at all yet. A tap with no sentence behind it
        // should give up rather than sit there recording the room.
        if (now - startedAt > SILENCE_BEFORE_SPEECH) {
          cancelRecording();
        }
        return;
      }

      if (now - lastSoundAt > SILENCE_AFTER_SPEECH) {
        stopRecording();
      }
    };

    inputNode.connect(processor);
    processor.connect(audioContext.destination);

  } catch (error) {
    console.error(error);

    if (phoneActive) {
      await setPhoneActive(false);
    }

    add(
      "Microphone access was denied or unavailable, sir.",
      "oops"
    );

    stopRecordingUI();
  }
}

// A tap with no sentence behind it. Everything is torn down and the
// voice channel handed back, but nothing is sent -- there is no
// command here, and asking the PC to transcribe a second of room noise
// wastes its time and yours.
function cancelRecording() {
  if (!recording) {
    return;
  }

  recording = false;
  recordedChunks = [];

  stopRecordingUI();
  setPhoneActive(false);
  setState("live", "ready");
}

async function stopRecording() {
  if (!recording) {
    return;
  }

  recording = false;

  const sourceRate = audioContext.sampleRate;

  const totalSamples = recordedChunks.reduce(
    (total, chunk) => total + chunk.length,
    0
  );

  const combined = new Float32Array(totalSamples);

  let offset = 0;

  for (const chunk of recordedChunks) {
    combined.set(chunk, offset);
    offset += chunk.length;
  }

  stopRecordingUI();


  // A tap rather than a hold. Caught here as well as on the server so
  // a stray touch costs nothing at all -- no upload, no transcription,
  // and no wait while the PC works out that a fraction of a second of
  // room noise wasn't a command. MIN_RECORD_SAMPLES is a third of a
  // second at whatever rate the microphone actually runs at.
  if (combined.length < sourceRate / 3) {
    // The channel was claimed on pointerdown, so it has to be given
    // back here too, or the PC microphone stays deaf with nothing
    // coming to release it.
    await setPhoneActive(false);
    setState("live", "ready");
    return;
  }

    if (askingAboutFrame) {
    const frame = askingAboutFrame;
    askingAboutFrame = null;

    cameraAsk.classList.remove("recording");
    closeCamera();

    const spoken = await transcribeOnly(
      encodeWav(resample16k(combined, sourceRate), 16000)
    );

    await setPhoneActive(false);

    if (!spoken) {
      add("I didn't catch the question, sir.", "oops");
      setState("live", "ready");
      return;
    }

    await sendLook(frame, spoken);
    return;
  }

  const samples = resample16k(combined, sourceRate);
  const wav = encodeWav(samples, 16000);

  setState("busy", "thinking");

  try {
    const controller = new AbortController();

    const timer = setTimeout(
      () => controller.abort(),
      60000
    );

    const response = await fetch(
      "/voice?t=" + encodeURIComponent(TOKEN),
      {
        method: "POST",
        headers: {
          "Content-Type": "audio/wav"
        },
        body: wav,
        signal: controller.signal
      }
    );

    clearTimeout(timer);

    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      add(
        data.error || "I couldn't process that, sir.",
        "oops"
      );

      setState("live", "ready");
      send.disabled = !alive;
      // Deliberately no box.focus() here: this is the voice path, and
    // focusing the text input is what makes the on-screen keyboard
    // appear every time the microphone is used.
      return;
    }

    if (data.heard) {
      add(data.heard, "me");
    }

    if (data.reply) {
      add(data.reply, "jarvis");

      if (data.sighting) {
        openSighting(data.sighting);
      } else if (data.cameras && data.cameras.length) {
        openGrid(data.cameras);
      } else if (data.camera) {
        openFeed(data.camera.name, data.camera.title);
      }

      if (data.link) {
        offerLink(
          data.link,
          data.reply.replace(/,? sir\\.?$/i, "")
        );
      }

      // Keep the PC microphone disabled while JARVIS's answer
      // is actually being played on the phone.
      await speak(data.reply);
    }

    await setPhoneActive(false);

    setState("live", "ready");

  } catch (error) {
    console.error(error);

    // Never leave the PC microphone locked if the phone request fails.
    await setPhoneActive(false);

    add(
      "No reply from your PC, sir. Is JARVIS still running?",
      "oops"
    );

    setState("", "JARVIS is not running");
    alive = false;
  }

  send.disabled = !alive;
  // Deliberately no box.focus() here: this is the voice path, and
  // focusing the text input is what makes the on-screen keyboard
  // appear every time the microphone is used.
}

// Tap to start, and it ends when you stop talking. Holding the button
// down was the previous behaviour and had a real cost on a phone: a
// long press is also the gesture for the text-selection bubble, which
// appeared over the top every time.
mic.addEventListener("click", (event) => {
  event.preventDefault();

  unlockAudio();

  if (recording) {
    // Tapping again is still allowed, for ending a sentence early
    // rather than waiting out the silence.
    stopRecording();
  } else {
    startRecording();
  }
});

// A long press on a button still raises the copy/paste bubble on
// Android, so it is refused outright here.
mic.addEventListener("contextmenu", (event) => event.preventDefault());

send.addEventListener("click", () => { unlockAudio(); submit(); });
box.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); unlockAudio(); submit(); }
});

// Notifications already allowed on this phone: JARVIS is told again on
// every visit, quietly, so a subscription the browser has renewed, or one
// JARVIS has lost, is put right without tapping the bell.
async function renewPush() {
  if (!("Notification" in window) || Notification.permission !== "granted" || !VAPID_PUBLIC_KEY) return;

  try {
    const registration = pushRegistration || await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription) return;

    const response = await fetch("/push/subscribe?t=" + encodeURIComponent(TOKEN), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription: subscription.toJSON() })
    });

    if (response.ok) {
      notify.textContent = "\\u{1F514}";
      notify.title = "notifications enabled";
    }
  } catch (e) {
    // The bell still works by hand.
  }
}

registerPushWorker().then(renewPush);
ping();
setInterval(ping, 5000);
</script>
</body>
</html>
"""


phone_server = PhoneServer()
