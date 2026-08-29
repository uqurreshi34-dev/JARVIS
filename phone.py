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

from flask import Flask, jsonify, request

from werkzeug.serving import make_server


# The port the phone connects to. Well above anything privileged, and
# unlikely to collide with a dev server already running.
DEFAULT_PORT = 8765

TOKEN_ENV = "JARVIS_PHONE_TOKEN"
PORT_ENV = "JARVIS_PHONE_PORT"

# How long a single command may take before the phone gives up waiting.
# Generous, since a command can involve a model call.
REQUEST_TIMEOUT = 60


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

        self._app = Flask(__name__)
        self._register_routes()

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
        return f"http://{local_address()}:{self.port}/?t={self.token}"

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

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        try:
            # 0.0.0.0 so the phone can reach it; the token is what
            # actually guards this, not the bind address.
            self._server = make_server(
                "0.0.0.0", self.port, self._app, threaded=True
            )
        except OSError as error:
            print(
                f"[JARVIS] could not start the phone server on port "
                f"{self.port}: {error}"
            )
            return

        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
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
</style>
</head>
<body>
  <header>
    <span class="dot" id="dot"></span>
    <h1>JARVIS</h1>
    <span id="state">connecting</span>
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
    <button id="send">Send</button>
  </footer>

<script>
const TOKEN = "__TOKEN__";
const log = document.getElementById("log");
const box = document.getElementById("text");
const send = document.getElementById("send");
const dot = document.getElementById("dot");
const state = document.getElementById("state");
const hint = document.getElementById("hint");

let alive = false;

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

send.addEventListener("click", submit);
box.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); submit(); }
});

ping();
setInterval(ping, 5000);
</script>
</body>
</html>
"""


phone_server = PhoneServer()
