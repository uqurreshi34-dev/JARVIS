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

import os
import secrets
import socket
import threading
import time

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from werkzeug.serving import make_server

import transcriber
from voice import set_phone_active

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


def ensure_certificate():
    """Create a local CA and a server certificate for JARVIS HTTPS."""

    if (
        os.path.exists(CERT_FILE)
        and os.path.exists(KEY_FILE)
        and os.path.exists(CA_FILE)
    ):
        return True

    print("[JARVIS] generating local HTTPS certificates...")

    try:
        import ipaddress
        from datetime import datetime, timedelta

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        hostname = local_address()
        now = datetime.utcnow()

        # -------------------------------------------------------------
        # 1. Create the local JARVIS Certificate Authority.
        # -------------------------------------------------------------

        ca_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        ca_subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Local CA"),
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
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.IPAddress(
                        ipaddress.ip_address(hostname)
                    ),
                ]),
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

        # Things JARVIS said unprompted that the phone hasn't collected
        # yet. Battery warnings, market alerts, pattern runs and the
        # morning diary are all announced to whoever is at the desk --
        # which is nobody, if you're in another room. Held here so the
        # phone can pick them up rather than them being said to an
        # empty chair.
        self._notices = []
        self._notices_lock = threading.Lock()

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

        with self._notices_lock:
            self._notices.append({
                "text": text,
                "at": time.strftime("%H:%M"),
            })

            # Bounded, because a JARVIS left running for days with no
            # phone connected should not accumulate for ever.
            if len(self._notices) > MAX_NOTICES:
                del self._notices[:-MAX_NOTICES]

    def take_notices(self):
        """Everything held since the last collection, clearing it."""
        with self._notices_lock:
            pending = list(self._notices)
            self._notices.clear()

        return pending

    def set_handler(self, handler):
        """Register what actually runs a command.

        Takes the spoken text, returns what JARVIS said back. Kept as a
        callback rather than importing commands directly so this module
        stays testable on its own, and so main.py keeps deciding how a
        command is actually run.
        """
        self._handler = handler

    @property
    def url(self):
        return f"https://{local_address()}:{self.port}/?t={self.token}"

    def _authorised(self):
        supplied = (
            request.args.get("t")
            or request.headers.get("X-Jarvis-Token")
            or ""
        )

        return secrets.compare_digest(supplied, self.token)

    def _register_routes(self):
        app = self._app

        @app.get("/")
        def page():
            return PAGE.replace("__TOKEN__", self.token), 200, {
                "Content-Type": "text/html; charset=utf-8"
            }

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

            return jsonify({"notices": self.take_notices()})

        @app.post("/phone-active")
        def phone_active():
            """Tell the desktop whether the phone owns the voice channel."""
            if not self._authorised():
                return jsonify({"error": "unauthorised"}), 403

            payload = request.get_json(silent=True) or {}
            active = bool(payload.get("active"))

            set_phone_active(active)

            return jsonify({"ok": True, "active": active})

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

            # Coerced rather than trusted: a handler returning something
            # unexpected should not take the whole request down with a
            # serialisation error, when sending back what we have and
            # letting the phone display it is perfectly serviceable.
            if reply is None:
                reply = ""
            elif not isinstance(reply, str):
                reply = str(reply)

            return jsonify({"heard": text, "reply": reply})

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

            set_phone_active(True)

            try:
                text = transcriber.transcribe_wav(audio)

                if not text:
                    return jsonify({
                        "error": "I couldn't make out what you said."
                    }), 400

                reply = self._handler(text)

                if reply is None:
                    reply = ""
                elif not isinstance(reply, str):
                    reply = str(reply)

                return jsonify({
                    "heard": text,
                    "reply": reply,
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

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        if not ensure_certificate():
            print(
                "[JARVIS] phone server not started because HTTPS "
                "certificate setup failed."
            )
            return

        try:
            # 0.0.0.0 so the phone can reach it over the local network.
            # HTTPS is required by mobile browsers for microphone access.
            self._server = make_server(
                "0.0.0.0",
                self.port,
                self._app,
                threaded=True,
                ssl_context=(CERT_FILE, KEY_FILE),
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
    <button id="mute" title="voice">&#128266;</button>
  </header>

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
const log = document.getElementById("log");
const box = document.getElementById("text");
const send = document.getElementById("send");
const mic = document.getElementById("mic");
const dot = document.getElementById("dot");
const state = document.getElementById("state");
const hint = document.getElementById("hint");

let alive = false;
let voiceOn = true;
let recording = false;
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

async function speak(text) {
  if (!voiceOn || !text) return;
  try {
    const res = await fetch("/audio?t=" + encodeURIComponent(TOKEN), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    });
    if (!res.ok) return;
    const blob = await res.blob();
    if (player.src) URL.revokeObjectURL(player.src);
    player.src = URL.createObjectURL(blob);
    await player.play();
  } catch (e) {
    // No voice is a small loss when the text is already on screen.
  }
}

function setState(kind, label) {
  dot.className = "dot" + (kind ? " " + kind : "");
  state.textContent = label;
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
  } else if (state.textContent !== "thinking") {
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
    const res = await fetch("/notices?t=" + encodeURIComponent(TOKEN));
    if (!res.ok) return;

    const data = await res.json();
    const notices = data.notices || [];

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
      speak(data.reply);
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

const mute = document.getElementById("mute");
mute.addEventListener("click", () => {
  voiceOn = !voiceOn;
  mute.classList.toggle("off", !voiceOn);
  if (!voiceOn) player.pause();
  unlockAudio();
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

  if (!combined.length) {
    // Nothing was captured -- a stray tap rather than a command. The
    // channel was still claimed on pointerdown, so it has to be given
    // back here too, or the PC microphone stays deaf with nothing
    // coming to release it.
    await setPhoneActive(false);
    setState("live", "ready");
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
      box.focus();
      return;
    }

    if (data.heard) {
      add(data.heard, "me");
    }

    if (data.reply) {
      add(data.reply, "jarvis");

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
  box.focus();
}

mic.addEventListener("pointerdown", (event) => {
  event.preventDefault();

  unlockAudio();
  startRecording();
});

mic.addEventListener("pointerup", (event) => {
  event.preventDefault();

  stopRecording();
});

mic.addEventListener("pointercancel", () => {
  stopRecording();
});

send.addEventListener("click", () => { unlockAudio(); submit(); });
box.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); unlockAudio(); submit(); }
});

ping();
setInterval(ping, 5000);
</script>
</body>
</html>
"""


phone_server = PhoneServer()
