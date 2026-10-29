"""Talking to JARVIS through the toy cam: hold BOOT, speak, let go, and the answer comes out of the toy.

    python tools/test_toy_talk.py

No board, microphone, speaker or model: the recording is made here, what was heard and what is said
back are set by hand, and the server is called as the board calls it. Checked:

- speech.to_pcm16: any voice's samples become 16-bit mono PCM at 16 kHz -- the right length, the
  right pitch, mono from stereo, nothing past full scale;
- POST /toy/talk: only with the token; size and type checked before anything is heard; a recording
  too short, or nothing made out, answered aloud with why; a command run as the phone's are (the
  desk quiet meanwhile, and listening again after), its reply sent back as raw PCM for the speaker;
- the toy's firmware: push-to-talk only on DFRobot's board, on BOOT, recorded into PSRAM, sent to
  /toy/talk over its own connection checked against JARVIS's authority, and played as it arrives.
"""

import io
import struct
import sys
import types
import wave
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402  (must precede actions imports)

sandbox.activate()

RESULTS = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(f"{'PASS' if condition else 'FAIL'} {label}")


# ---- the voice, made for the toy's speaker ----------------------------------------------------------

for name in ("sounddevice",):
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = MagicMock(name=name)

import speech  # noqa: E402

rate = 24000
tone = np.sin(2 * np.pi * 440.0 * np.arange(rate) / rate).astype(np.float32) * 0.5
pcm = speech.to_pcm16(tone, rate, 16000)
samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
check(len(pcm) == 2 * 16000, f"a second of 24 kHz voice is a second at 16 kHz, 16-bit ({len(pcm)} bytes)")
spectrum = np.abs(np.fft.rfft(samples))
check(abs(int(np.argmax(spectrum)) - 440) <= 1, "at the same pitch: a 440 Hz tone is still 440 Hz")
check(0.45 < np.max(np.abs(samples)) < 0.52, "and the same loudness")
stereo = np.stack([tone, tone], axis=1)
check(speech.to_pcm16(stereo, rate, 16000) == pcm, "stereo is made mono")
loud = speech.to_pcm16(np.array([2.0, -2.0, 0.0], dtype=np.float32), 16000, 16000)
check(struct.unpack("<3h", loud) == (32767, -32767, 0), "nothing past full scale; at its own rate, unchanged")
check(speech.to_pcm16(np.zeros(0, dtype=np.float32), rate, 16000) == b"", "no samples, no bytes")

# ---- the server, as the board calls it --------------------------------------------------------------

try:
    import pywebpush  # noqa: F401
except ImportError:
    sys.modules["pywebpush"] = types.SimpleNamespace(webpush=None, WebPushException=Exception)

sys.modules.setdefault("vosk", types.SimpleNamespace(KaldiRecognizer=MagicMock(), Model=MagicMock()))

import transcriber  # noqa: E402

transcriber.build_engine = lambda model, block: types.SimpleNamespace(name="test", active=False,
                                                                        reset=lambda: None, feed=lambda d: None)

try:
    import phone
except Exception as error:
    print(f"SKIP the toy's endpoint (could not import phone: {error})")
    phone = None


def recording(seconds):
    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * int(16000 * seconds))

    return buffer.getvalue()


if phone:
    server = phone.PhoneServer(token="right-token")
    client = server._app.test_client()
    heard_texts, handled, active, spoken = [], [], [], []
    phone.transcriber.transcribe_wav = lambda audio: heard_texts.pop(0)
    phone.set_phone_active = active.append
    phone.speech.pcm16 = lambda text, rate=16000: (spoken.append((text, rate)) or b"\x01\x00" * 8)

    def handler(text):
        handled.append(text)
        return replies.pop(0)

    replies = []
    server.set_handler(handler)

    def talk(audio, token="right-token", kind="audio/wav"):
        return client.post("/toy/talk", data=audio,
                           headers={"X-Jarvis-Token": token, "X-Jarvis-Camera": "toy cam", "Content-Type": kind})

    check(talk(recording(1), token="wrong").status_code == 403, "refused without JARVIS's token")
    check(talk(b"\x00" * (phone.MAX_TOY_TALK_BYTES + 1)).status_code == 413 and not spoken,
          "a recording past twelve seconds refused before it is read")
    check(talk(recording(1), kind="audio/mpeg").status_code == 400 and not spoken, "only WAV")

    answer = talk(recording(0.1))
    check(answer.status_code == 200 and "too short" in spoken[-1][0] and not handled,
          "a stray press answered aloud: too short, hold the button while you speak")

    heard_texts.append("")
    talk(recording(1))
    check("couldn't make out" in spoken[-1][0] and not handled, "nothing made out: said so")

    heard_texts.append("you")
    talk(recording(1))
    check("stray word" in spoken[-1][0] and not handled, "a lone filler word is not a command")

    heard_texts.append("what time is it")
    replies.append("It's twenty to five, sir.")
    active.clear()
    answer = talk(recording(1.5))
    check(handled == ["what time is it"] and spoken[-1] == ("It's twenty to five, sir.", 16000),
          "a command is run as the phone's are, and its reply spoken at 16 kHz")
    check(answer.status_code == 200 and answer.data == b"\x01\x00" * 8
          and answer.headers["Content-Type"].startswith("audio/L16; rate=16000"),
          "sent back as raw 16-bit PCM, for the speaker to play as it comes")
    check(active == [True, False], "the desk microphone quiet while it is handled, listening again after")

    heard_texts.append("call mum")
    replies.append({"spoken": "Calling mum on your phone, sir.", "link": "tel:123"})
    talk(recording(1.5))
    check(spoken[-1][0] == "Calling mum on your phone, sir.", "a phone action's words are said, its link ignored")

    def broken(text):
        raise RuntimeError("model down")

    server.set_handler(broken)
    heard_texts.append("what's the weather")
    active.clear()
    answer = talk(recording(1.5))
    check(answer.status_code == 200 and "went wrong" in spoken[-1][0] and active == [True, False],
          "a command that fails is said to have, and the desk listens again")

# ---- the toy's firmware ------------------------------------------------------------------------------

sketch = (ROOT / "arduino" / "jarvis_camera" / "jarvis_camera.ino").read_text(encoding="utf-8")
talk_part = sketch[sketch.index("// Talking to JARVIS through the toy"):sketch.index("void setup() {")]
check(talk_part.startswith("// Talking to JARVIS through the toy") and
      sketch[:sketch.index("// Talking to JARVIS through the toy")].rstrip().endswith("#if defined(JARVIS_BOARD_DFR1154)")
      and "startTalk();" in sketch[sketch.index("void setup() {"):sketch.index("void loop() {")],
      "push-to-talk is built only for DFRobot's board, and started with it")
check("TALK_BUTTON_PIN = 0;" in talk_part and "digitalRead(TALK_BUTTON_PIN) == LOW" in talk_part
      and "TALK_MOST_MS = 10000UL" in talk_part,
      "recorded only while BOOT is held, ten seconds at most")
check("TALK_MIC_CLOCK_PIN = 38" in talk_part and "TALK_MIC_DATA_PIN = 39" in talk_part
      and "TALK_SPEAKER_BCLK_PIN = 45" in talk_part and "TALK_SPEAKER_LRC_PIN = 46" in talk_part
      and "TALK_SPEAKER_DOUT_PIN = 42" in talk_part and "I2S_MODE_PDM_RX" in talk_part,
      "on DFRobot's microphone and speaker pins")
check("MALLOC_CAP_SPIRAM" in talk_part and '"/toy/talk"' in talk_part and "https.begin(talkLink, url)" in talk_part
      and "talkLink.setCACert(JARVIS_CA);" in talk_part and "setInsecure" not in talk_part,
      "into PSRAM, then to /toy/talk over its own connection, checked against JARVIS's authority")
check("talkSpeaker.write(chunk, have);" in talk_part and "getStreamPtr()" in talk_part,
      "and the answer played as it arrives, not held whole")
check("TALK_RATE = 16000" in talk_part and "TOY_TALK_RATE = 16000" in (ROOT / "phone.py").read_text(encoding="utf-8"),
      "the board and JARVIS agree on the rate")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
