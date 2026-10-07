"""Whose voice it is: JARVIS acts only on the voice enrolled with tools/enroll_voice.py.

The wake word alone cannot tell you from a video, a phone on speaker or a
friend: anyone who says "Jarvis" is obeyed. So every utterance the
microphone hears is first compared with your voiceprint -- a 256-number
summary of how you sound, made from a few clips of you speaking -- and
anything that does not sound like you is dropped before it is transcribed.
It is not sent to the cloud transcriber, so it costs nothing and nobody
else's words leave the room.

The comparison uses WeSpeaker's ResNet34 speaker model (trained on
VoxCeleb), run with onnxruntime. It is downloaded once, by the enrolment
tool, from sherpa-onnx's GitHub releases and checked against a fixed
SHA-256 before it is used; nothing is fetched while JARVIS runs.

voiceprint.json in the JARVIS folder holds the voiceprint and its
settings:

    enabled     false to switch the check off (tools/enroll_voice.py --off)
    threshold   how alike, from 0 to 1, a voice must be to count as yours;
                raise it if others get through, lower it if you are missed

With no voiceprint JARVIS answers anyone, as before, and says so at
start-up. With one he answers only you -- and if the model cannot run,
nobody, rather than everybody, until it is fixed or switched off.

The phone app is not checked here: it is already paired to you.
"""

import hashlib
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timezone

import numpy as np

from actions import files


SAMPLE_RATE = 16000

MODEL_NAME = "voice-id-model.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "wespeaker_en_voxceleb_resnet34_LM.onnx")
MODEL_SHA256 = "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012"
DOWNLOAD_SECONDS = 120

VOICEPRINT_NAME = "voiceprint.json"

# Cosine similarity to the voiceprint. Clips of one speaker score about
# 0.5 to 0.9 against each other with this model, different speakers 0.1 to
# 0.45; a voiceprint averaged over several clips raises your own scores.
DEFAULT_THRESHOLD = 0.5
LOWEST_THRESHOLD = 0.2

# Below this, other people's voices scored as high as some of your own in
# testing: the lowest threshold worth suggesting.
SAFE_THRESHOLD = 0.4
HIGHEST_THRESHOLD = 0.95

# Every time your voice is let through, and every miss this close below the
# threshold, goes in jarvis-log.txt with its score, so the threshold can be
# set from evidence (python tools/enroll_voice.py --report). Clear misses --
# a video, someone across the room -- are left out, or they would fill it.
NEAR_MISS = 0.15
JOURNAL_KIND = "voice"

# The model's features: Kaldi filterbanks, as WeSpeaker computes them.
_FRAME = 400          # 25 ms
_SHIFT = 160          # 10 ms
_FFT = 512
_MEL_BINS = 80
_LOW_HZ = 20.0
_PREEMPHASIS = 0.97

# Frames this far below the loudest one are the room, not you, and only
# blur the voiceprint: they are left out, unless that leaves too little.
QUIET_DB = 30.0
MIN_FRAMES = 30

_SCALE = 32768.0      # the model expects samples at 16-bit scale


def _mel(hz):
    return 1127.0 * np.log(1.0 + hz / 700.0)


def _mel_bank():
    edges = np.linspace(_mel(_LOW_HZ), _mel(SAMPLE_RATE / 2), _MEL_BINS + 2)
    bins = _mel(np.arange(_FFT // 2 + 1) * SAMPLE_RATE / _FFT)
    bank = np.zeros((_MEL_BINS, _FFT // 2 + 1))

    for index in range(_MEL_BINS):
        low, centre, high = edges[index:index + 3]
        bank[index] = np.maximum(0.0, np.minimum((bins - low) / (centre - low), (high - bins) / (high - centre)))

    bank[:, -1] = 0.0   # Kaldi leaves out the Nyquist bin
    return bank


_BANK = _mel_bank()
_WINDOW = 0.54 - 0.46 * np.cos(2 * np.pi * np.arange(_FRAME) / (_FRAME - 1))


def features(audio):
    """80 log-mel filterbanks per 10 ms of speech, quiet frames dropped, mean taken off; None if too short."""
    samples = np.asarray(audio, dtype=np.float64).reshape(-1) * _SCALE

    if len(samples) < _FRAME:
        return None

    count = 1 + (len(samples) - _FRAME) // _SHIFT
    frames = samples[np.arange(_FRAME)[None, :] + _SHIFT * np.arange(count)[:, None]]
    frames = frames - frames.mean(axis=1, keepdims=True)
    energy_db = 10 * np.log10(np.maximum(np.mean(frames ** 2, axis=1), 1e-10))

    frames = np.concatenate([frames[:, :1] * (1 - _PREEMPHASIS), frames[:, 1:] - _PREEMPHASIS * frames[:, :-1]], axis=1)
    power = np.abs(np.fft.rfft(frames * _WINDOW, _FFT)) ** 2
    banks = np.log(np.maximum(power @ _BANK.T, np.finfo(np.float32).eps))

    loud = energy_db >= energy_db.max() - QUIET_DB

    if loud.sum() >= MIN_FRAMES:
        banks = banks[loud]
    elif len(banks) < MIN_FRAMES:
        return None

    return (banks - banks.mean(axis=0)).astype(np.float32)


# ---- the model -----------------------------------------------------------------------------------

def model_path():
    base = files.root()
    return os.path.join(base, MODEL_NAME) if base else None


def _sha256(path):
    digest = hashlib.sha256()

    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)

    return digest.hexdigest()


def download_model(report=print):
    """Fetch the model once, refusing anything that is not exactly the file expected. Returns its path."""
    path = model_path()

    if path is None:
        raise RuntimeError("the JARVIS folder is not available")

    if os.path.exists(path) and _sha256(path) == MODEL_SHA256:
        return path

    report(f"Downloading the voice model (about 26 MB) from {MODEL_URL.split('/')[2]}...")
    handle, temporary = tempfile.mkstemp(prefix="voice-id-", suffix=".part", dir=os.path.dirname(path))

    try:
        with os.fdopen(handle, "wb") as out, urllib.request.urlopen(MODEL_URL, timeout=DOWNLOAD_SECONDS) as response:
            for chunk in iter(lambda: response.read(1 << 20), b""):
                out.write(chunk)

        if _sha256(temporary) != MODEL_SHA256:
            raise RuntimeError("the downloaded voice model is not the expected file (its SHA-256 differs); not used")

        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)

    return path


class Model:
    """The speaker model, loaded from the JARVIS folder after its SHA-256 is checked."""

    def __init__(self, path=None):
        path = path or model_path()

        if not path or not os.path.exists(path):
            raise RuntimeError(f"{MODEL_NAME} is not in the JARVIS folder; run python tools/enroll_voice.py")

        if _sha256(path) != MODEL_SHA256:
            raise RuntimeError(f"{MODEL_NAME} is not the expected file (its SHA-256 differs); run"
                               " python tools/enroll_voice.py to fetch it again")

        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.log_severity_level = 3
        self._session = onnxruntime.InferenceSession(path, options, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def embed(self, audio):
        """A unit vector for how the voice in [audio] (floats, -1 to 1, 16 kHz) sounds; None if too short."""
        banks = features(audio)

        if banks is None:
            return None

        vector = self._session.run(None, {self._input: banks[None]})[0][0].astype(np.float64)
        length = np.linalg.norm(vector)
        return vector / length if length else None


def centroid(vectors):
    mean = np.mean(np.asarray(vectors, dtype=np.float64), axis=0)
    return mean / np.linalg.norm(mean)


# ---- the voiceprint ----------------------------------------------------------------------------

def voiceprint_path():
    base = files.root()
    return os.path.join(base, VOICEPRINT_NAME) if base else None


def load_voiceprint():
    """The saved voiceprint and its settings, or None if there is none. Raises ValueError if it is unreadable."""
    path = voiceprint_path()

    if not path or not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError) as error:
        raise ValueError(f"{VOICEPRINT_NAME} could not be read ({error})") from None

    vector = np.asarray(saved.get("voiceprint") or [], dtype=np.float64)
    threshold = saved.get("threshold", DEFAULT_THRESHOLD)

    if (vector.shape != (256,) or not np.isfinite(vector).all() or not np.linalg.norm(vector)
            or not isinstance(saved.get("enabled", True), bool)
            or isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not LOWEST_THRESHOLD <= threshold <= HIGHEST_THRESHOLD):
        raise ValueError(f"{VOICEPRINT_NAME} is not a voiceprint JARVIS understands; enrol again with"
                         " python tools/enroll_voice.py")

    if saved.get("model_sha256") != MODEL_SHA256:
        raise ValueError(f"{VOICEPRINT_NAME} was made with a different voice model; enrol again with"
                         " python tools/enroll_voice.py")

    clips = saved.get("clip_vectors")

    if clips is not None:
        clips = np.asarray(clips, dtype=np.float64)

        if clips.ndim != 2 or clips.shape[1] != 256 or not np.isfinite(clips).all():
            raise ValueError(f"{VOICEPRINT_NAME} has clips JARVIS does not understand; enrol again with"
                             " python tools/enroll_voice.py")

    saved["clip_vectors"] = clips
    saved["voiceprint"] = vector / np.linalg.norm(vector)
    saved.setdefault("enabled", True)
    saved["threshold"] = float(threshold)
    return saved


def save_voiceprint(changes, replace=False):
    """Merge [changes] into voiceprint.json (or [replace] it), written whole or not at all."""
    path = voiceprint_path()

    if path is None:
        raise RuntimeError("the JARVIS folder is not available")

    saved = {}

    if os.path.exists(path) and not replace:
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)

    saved.update(changes)

    if isinstance(saved.get("voiceprint"), np.ndarray):
        saved["voiceprint"] = [round(float(value), 6) for value in saved["voiceprint"]]

    if saved.get("clip_vectors") is not None:
        saved["clip_vectors"] = [[round(float(value), 6) for value in vector] for vector in saved["clip_vectors"]]

    saved["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    handle, temporary = tempfile.mkstemp(prefix="voiceprint-", suffix=".part", dir=os.path.dirname(path))

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(saved, out, indent=1)

        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


# ---- the check ---------------------------------------------------------------------------------

class Gate:
    """Called with each utterance's audio: True if it is your voice."""

    def __init__(self, voiceprint, threshold, model):
        self.voiceprint, self.threshold, self._model = voiceprint, threshold, model
        self.last_score = None

    def score(self, audio):
        vector = self._model.embed(audio)
        return None if vector is None else float(vector @ self.voiceprint)

    def __call__(self, audio):
        try:
            score = self.score(audio)
        except Exception as error:
            print(f"[ignored] the voice check failed ({error})")
            return False

        self.last_score = score

        if score is None:
            print("[ignored] too short to tell whose voice it is")
            return False

        seconds = len(audio) / SAMPLE_RATE

        if score < self.threshold:
            print(f"[ignored] not your voice (sounds {score:.2f} like you; {self.threshold:.2f} needed)")

            if score >= self.threshold - NEAR_MISS:
                _record(f"near miss: sounds {score:.2f} like you, {self.threshold:.2f} needed, {seconds:.1f}s", "ignored")

            return False

        _record(f"yours: sounds {score:.2f} like you, {self.threshold:.2f} needed, {seconds:.1f}s", "heard")
        return True


def _record(detail, outcome):
    from actions import journal

    journal.write(JOURNAL_KIND, detail, outcome)



class Closed:
    """Enrolled, but the check cannot run: nothing is taken as you until it is fixed or switched off."""

    def __init__(self, reason):
        self.reason = reason

    def __call__(self, audio):
        print(f"[ignored] the voice check cannot run ({self.reason})")
        return False


def gate(model_factory=Model):
    """The check for voice.py's engine: a Gate, a Closed gate, or None when no voiceprint is in use."""
    try:
        saved = load_voiceprint()
    except ValueError as error:
        print(f"[JARVIS] voice check: {error}. Ignoring everything heard until it is fixed, or"
              " switched off with python tools/enroll_voice.py --off.")
        return Closed(str(error))

    if saved is None:
        print("[JARVIS] voice check off: anyone who says his name is answered. Run python tools/enroll_voice.py"
              " so JARVIS answers only you.")
        return None

    if not saved["enabled"]:
        print("[JARVIS] voice check switched off in voiceprint.json: anyone who says his name is answered.")
        return None

    try:
        model = model_factory()
    except Exception as error:
        print(f"[JARVIS] voice check: {error}. Ignoring everything heard until it can run, or it is"
              " switched off with python tools/enroll_voice.py --off.")
        return Closed(str(error))

    print(f"[JARVIS] voice check on: answering only your voice (threshold {saved['threshold']:.2f}).")
    return Gate(saved["voiceprint"], saved["threshold"], model)
