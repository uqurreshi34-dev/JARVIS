""""Hey Jarvis" heard on this PC by a small wake-word model, before anything is written out or sent.

Until November 2026 the desk found "Jarvis" by writing out everything the microphone heard -- with Groq
Whisper, on Groq's servers -- and then looking for the name in the words. Every sentence said in the room,
and the television, went to the cloud first. The toy never did that: its chip hears "Hey Jarvis" itself.
This is the same for the desk: openWakeWord's "hey jarvis" model listens to the sound itself, here, and only
what follows it (or what is said in a follow-up window) is written out.

The model is openWakeWord's three small ONNX files, run with onnxruntime (already used for memory and
Kokoro): a melspectrogram, Google's speech embedding, and the "hey jarvis" classifier. They are fetched
once from openWakeWord's own release into the JARVIS folder (models/wake), checked against their SHA-256,
and work offline from then on. The streaming steps below are openWakeWord's own (utils.AudioFeatures and
Model.predict, v0.4-0.6), written out here so no new package is needed: every 80 ms of audio adds 8 rows of
the melspectrogram and one embedding; the classifier reads the last 16 embeddings (about 1.3 s).

Licences: openWakeWord's code is Apache-2.0; its pre-trained models, "hey jarvis" among them, are
CC BY-NC-SA 4.0 -- free for personal, non-commercial use. A product sold with JARVIS would train its own
(openWakeWord trains one from synthetic speech, free).

Settings (.env):
    JARVIS_WAKE_MODEL      "hey_jarvis" (default) or "off": off, the name is found in the written-out words,
                           as before, and everything heard is transcribed
    JARVIS_WAKE_THRESHOLD  how sure the model must be, 0 to 1 (default 0.5, openWakeWord's own)
"""

import hashlib
import os
import threading
from collections import deque

import numpy as np

SAMPLE_RATE = 16000
FRAME = 1280                     # 80 ms: what the model steps by
_MEL_WINDOW = 76                 # melspectrogram rows per embedding
_MEL_STEP = 8                    # rows added per 80 ms
_MEL_KEEP = 10 * 97              # about ten seconds of rows
_FEATURES_KEEP = 120             # about ten seconds of embeddings
_FEATURES_READ = 16              # what the classifier reads: about 1.3 s

RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"

# The three files, as released by openWakeWord v0.5.1, and their SHA-256 (checked 9 November 2026).
FILES = {
    "melspectrogram": ("melspectrogram.onnx", "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f"),
    "embedding": ("embedding_model.onnx", "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f"),
    "hey_jarvis": ("hey_jarvis_v0.1.onnx", "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb"),
}

DEFAULT_THRESHOLD = 0.5


def wanted():
    """True unless JARVIS_WAKE_MODEL turns the wake-word model off."""
    return (os.getenv("JARVIS_WAKE_MODEL") or "hey_jarvis").strip().casefold() not in ("off", "0", "no", "none")


def threshold():
    try:
        return min(max(float(os.getenv("JARVIS_WAKE_THRESHOLD") or DEFAULT_THRESHOLD), 0.05), 0.99)
    except ValueError:
        return DEFAULT_THRESHOLD


def folder():
    """Where the files are kept: models/wake in the JARVIS folder."""
    path = os.path.join(os.path.expanduser("~"), "JARVIS", "models", "wake")
    os.makedirs(path, exist_ok=True)
    return path


def _sha256(path):
    digest = hashlib.sha256()

    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)

    return digest.hexdigest()


def fetch(where=None, download=None):
    """The three files' paths, fetched and checked first when they are missing or wrong. Raises when
    they cannot be had (no internet the first time): the desk then works as before, without it."""
    where = where or folder()
    paths = {}

    for key, (name, digest) in FILES.items():
        path = os.path.join(where, name)

        if not os.path.exists(path) or _sha256(path) != digest:
            if download is None:
                import requests

                def download(url):
                    answer = requests.get(url, timeout=30)
                    answer.raise_for_status()
                    return answer.content

            data = download(RELEASE + name)

            if hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError(f"{name} is not the file openWakeWord released (its SHA-256 differs)")

            partial = path + ".part"

            with open(partial, "wb") as handle:
                handle.write(data)

            os.replace(partial, path)
            print(f"[JARVIS] wake word: fetched {name}")

        paths[key] = path

    return paths


class Detector:
    """openWakeWord's streaming "hey jarvis", fed 16-bit mono audio at 16 kHz in blocks of any size."""

    def __init__(self, paths):
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        providers = ["CPUExecutionProvider"]
        self._mel = onnxruntime.InferenceSession(paths["melspectrogram"], options, providers=providers)
        self._embed = onnxruntime.InferenceSession(paths["embedding"], options, providers=providers)
        self._wake = onnxruntime.InferenceSession(paths["hey_jarvis"], options, providers=providers)
        self._wake_input = self._wake.get_inputs()[0].name
        self.reset()

    def reset(self):
        """Forget what was heard: after JARVIS speaks, so his own voice cannot linger in the buffers."""
        self._raw = deque(maxlen=SAMPLE_RATE * 10)
        self._waiting = np.zeros(0, dtype=np.int16)
        self._mels = np.ones((_MEL_WINDOW, 32), dtype=np.float32)
        self._features = self._embeddings(np.zeros(SAMPLE_RATE * 10, dtype=np.int16))
        self.score = 0.0

    def _melspectrogram(self, samples):
        spec = np.squeeze(self._mel.run(None, {"input": samples.astype(np.float32)[None, :]})[0])
        return spec / 10 + 2

    def _embeddings(self, samples):
        spec = self._melspectrogram(samples)
        windows = [spec[i:i + _MEL_WINDOW] for i in range(0, spec.shape[0], _MEL_STEP)
                   if spec[i:i + _MEL_WINDOW].shape[0] == _MEL_WINDOW]
        batch = np.array(windows, dtype=np.float32)[..., None]
        return self._embed.run(None, {"input_1": batch})[0].squeeze()

    def _step(self, frame):
        """One 80 ms frame in; the classifier's score for the last 1.3 s out."""
        self._raw.extend(frame.tolist())
        recent = np.array(list(self._raw)[-(FRAME + 160 * 3):], dtype=np.int16)
        self._mels = np.vstack((self._mels, self._melspectrogram(recent)))[-_MEL_KEEP:]
        window = self._mels[-_MEL_WINDOW:].astype(np.float32)[None, :, :, None]

        if window.shape[1] == _MEL_WINDOW:
            embedding = self._embed.run(None, {"input_1": window})[0].squeeze()
            self._features = np.vstack((self._features, embedding))[-_FEATURES_KEEP:]

        features = self._features[-_FEATURES_READ:][None].astype(np.float32)
        return float(np.squeeze(self._wake.run(None, {self._wake_input: features})[0]))

    def feed(self, block):
        """The highest score in [block] (bytes or int16 samples); also kept as self.score."""
        samples = np.frombuffer(block, dtype=np.int16) if isinstance(block, (bytes, bytearray)) else block
        self._waiting = np.concatenate((self._waiting, samples.astype(np.int16)))
        best = 0.0

        while len(self._waiting) >= FRAME:
            frame, self._waiting = self._waiting[:FRAME], self._waiting[FRAME:]
            best = max(best, self._step(frame))

        self.score = best
        return best


_state = {"detector": None, "why": "not started", "lock": threading.Lock()}


def start(on_ready=None):
    """Load the model in the background (fetching it the first time). [on_ready] gets the Detector."""
    if not wanted():
        _state["why"] = "off (JARVIS_WAKE_MODEL=off)"
        return

    def load():
        try:
            detector = Detector(fetch())
        except Exception as error:   # noqa: BLE001 - without it the desk works as before
            _state["why"] = f"unavailable ({type(error).__name__}: {error})"
            print(f"[JARVIS] wake word model {_state['why']}; finding the name in the words instead")
            return

        with _state["lock"]:
            _state["detector"], _state["why"] = detector, "ready"

        print(f"[JARVIS] wake word: \"Hey Jarvis\" heard on this PC (threshold {threshold():.2f}); "
              "nothing else is transcribed")

        if on_ready:
            on_ready(detector)

    threading.Thread(target=load, name="wake-model", daemon=True).start()


def status():
    """For the boot screen and the status report: "ready", "off (...)", "unavailable (...)"."""
    return _state["why"]
