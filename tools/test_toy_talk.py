"""Talking to JARVIS through the toy cam, hands-free: "Jarvis, ..." to the toy, and the answer comes out of it.

    python tools/test_toy_talk.py

No board, microphone, speaker or model: the recording is made here, what was heard and what is said
back are set by hand, and the server is called as the board calls it. Checked:

- speech.to_pcm16: any voice's samples become 16-bit mono PCM at 16 kHz -- the right length, the
  right pitch, mono from stereo, nothing past full scale;
- POST /toy/talk, BOOT held: only with the token; size and type checked before anything is heard; a
  recording too short, or nothing made out, answered aloud with why; a command run as the phone's
  are (the desk quiet meanwhile, and listening again after), its reply sent back as raw PCM;
- hands-free: what the toy heard unasked is turned away in silence unless the wake grammar hears his
  name and the transcript starts with it -- never transcribed when the grammar does not; his name
  alone is answered "Yes, sir?"; a follow-up within the window needs no name, and after it, it does;
- the toy's firmware: only on DFRobot's board; listening for speech against the room's quiet, with
  half a second kept from before it began; recorded into PSRAM, sent to /toy/talk over its own
  connection checked against JARVIS's authority, played as it arrives; TLS memory from PSRAM.
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
raised = np.frombuffer(speech.to_pcm16(tone, rate, 16000, peak=0.95), dtype="<i2").astype(np.float32) / 32767.0
check(0.93 < np.max(np.abs(raised)) <= 0.95, "asked for a peak, a quiet voice is raised to it")
whisper = np.frombuffer(speech.to_pcm16(tone * 0.01, rate, 16000, peak=0.95), dtype="<i2").astype(np.float32) / 32767.0
check(np.max(np.abs(whisper)) < 0.03, "but at most four times: near-silence is not made a shout")
check(speech.to_pcm16(np.zeros(100, dtype=np.float32), 16000, 16000, peak=0.95) == b"\x00\x00" * 100,
      "and silence stays silence")

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
    peaks = []
    phone.speech.pcm16 = lambda text, rate=16000, peak=None: (peaks.append(peak) or spoken.append((text, rate))
                                                             or b"\x01\x00" * 8)

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

    # ---- hands-free: what the toy heard unasked ----

    server.set_handler(handler)
    woken, transcribed = [], []
    phone.wake_heard = lambda pcm: woken.pop(0)
    real_transcribe = phone.transcriber.transcribe_wav

    def counting(audio):
        transcribed.append(1)
        return heard_texts.pop(0)

    phone.transcriber.transcribe_wav = counting

    def hear(audio):
        return client.post("/toy/talk", data=audio, headers={
            "X-Jarvis-Token": "right-token", "X-Jarvis-Camera": "toy cam",
            "Content-Type": "audio/wav", "X-Jarvis-Toy": "sound"})

    handled.clear()
    spoken.clear()
    woken.append(False)
    answer = hear(recording(1.5))
    check(answer.status_code == 204 and not answer.data and not transcribed and not handled and not spoken,
          "the room's chatter, no name in it: turned away in silence, never transcribed")

    woken.append(True)
    heard_texts.append("oh yeah that was good")
    answer = hear(recording(1.5))
    check(answer.status_code == 204 and len(transcribed) == 1 and not handled,
          "the grammar heard a name the transcript does not have: still silence")

    woken.append(True)
    heard_texts.append("yesterday I told my brother that jarvis is clever")
    check(hear(recording(1.5)).status_code == 204 and not handled, "his name in the middle is not him called")

    woken.append(True)
    heard_texts.append("Jarvis, what time is it")
    replies.append("It's five o'clock, sir.")
    answer = hear(recording(1.5))
    check(answer.status_code == 200 and handled == ["what time is it"] and spoken[-1][0] == "It's five o'clock, sir.",
          "'Jarvis, what time is it': the command without his name, answered on the toy")

    heard_texts.append("and tomorrow's weather")
    replies.append("Rain, sir.")
    answer = hear(recording(1.5))
    check(answer.status_code == 200 and handled[-1] == "and tomorrow's weather" and not woken,
          "a follow-up straight after needs no name, and is not even checked for one")

    server._toy_follow_until = 0.0
    woken.append(False)
    check(hear(recording(1.5)).status_code == 204 and handled[-1] == "and tomorrow's weather",
          "once the window has passed, it does again")

    woken.append(True)
    heard_texts.append("Jarvis")
    answer = hear(recording(1))
    check(answer.status_code == 200 and spoken[-1][0] == "Yes, sir?" and server._toy_follow_until > 0,
          "his name alone: 'Yes, sir?', and the next words need no name")
    heard_texts.append("play some music")
    replies.append("Playing, sir.")
    hear(recording(1.5))
    check(handled[-1] == "play some music", "so the command after it is taken")

    def heard_on_board(audio):
        return client.post("/toy/talk", data=audio, headers={
            "X-Jarvis-Token": "right-token", "X-Jarvis-Camera": "toy cam",
            "Content-Type": "audio/wav", "X-Jarvis-Toy": "wake"})

    server._toy_follow_until = 0.0
    heard_texts.append("what time is it")
    replies.append("It's six o'clock, sir.")
    answer = heard_on_board(recording(1.5))
    check(answer.status_code == 200 and handled[-1] == "what time is it" and not woken
          and server._toy_follow_until > 0,
          "31 October 2026: Jarvis heard on the toy's chip, the name not in the recording: still his, "
          "not checked again, and follow-ups open")
    server._toy_follow_until = 0.0
    heard_texts.append("Jarvis")
    check(heard_on_board(recording(1)).status_code == 200 and spoken[-1][0] == "Yes, sir?",
          "and his name alone, heard on the chip: 'Yes, sir?'")

    server._toy_follow_until = 0.0
    check(hear(recording(0.1)).status_code == 204, "heard unasked and too short: silence, not a complaint")
    check(peaks and all(peak == phone.TOY_TALK_PEAK == 0.95 for peak in peaks),
          "every answer raised to the toy's loudness")

    # ---- "Jarvis, mute" and "Jarvis, unmute", said to the toy ----

    handled.clear()
    woken.append(True)
    heard_texts.append("Jarvis, mute.")
    answer = hear(recording(1))
    check(answer.status_code == 200 and spoken[-1][0] == "Muted, sir. Say Hey Jarvis, unmute, when you want me." and server._toy_muted
          and not handled, "'Jarvis, mute': muted, said so, and not passed on as a command")
    woken.append(True)
    heard_texts.append("Jarvis, what is the time")
    check(hear(recording(1.5)).status_code == 204 and not handled, "muted, 'Jarvis, what is ...' gets nothing")
    woken.append(True)
    heard_texts.append("Jarvis")
    check(hear(recording(1)).status_code == 204, "nor his name alone")
    heard_texts.append("what is the time")
    replies.append("Six, sir.")
    check(talk(recording(1.5)).status_code == 204 and not handled, "nor BOOT held")
    woken.append(True)
    heard_texts.append("Jarvis, un-mute.")
    answer = hear(recording(1))
    check(answer.status_code == 200 and spoken[-1][0] == "I'm listening again, sir." and not server._toy_muted,
          "'Jarvis, un-mute': listening again")
    woken.append(True)
    heard_texts.append("Jarvis, what is the time")
    check(hear(recording(1.5)).status_code == 200 and handled == ["what is the time"], "and answers again")
    woken.append(True)
    heard_texts.append("Jarvis, I love you")
    replies.append("And I'm very fond of you, sir.")
    answer = hear(recording(1.5))
    check(answer.headers.get("X-Jarvis-Mood") == "affection", "'I love you': the answer is marked warm, for a red ring")
    woken.append(True)
    heard_texts.append("Jarvis, what is the time")
    replies.append("Seven, sir.")
    answer = hear(recording(1.5))
    check(answer.headers.get("X-Jarvis-Mood") == "normal", "and the next, ordinary answer is not")
    check(phone.PhoneServer._toy_mute_asked("Unmute please.") == "unmute"
          and phone.PhoneServer._toy_mute_asked("be quiet") == "mute"
          and phone.PhoneServer._toy_mute_asked("mute the television") is None,
          "'unmute please', 'be quiet'; 'mute the television' is a command, not the toy muted")

    # ---- "Jarvis, switch to cloud mode" / "... PC mode", said to the toy ----

    handled.clear()
    woken.append(True)
    heard_texts.append("Jarvis, switch to cloud mode.")
    answer = hear(recording(1.5))
    check(answer.status_code == 200 and answer.headers.get("X-Jarvis-Switch") == "cloud"
          and spoken[-1][0].startswith("Switching to cloud mode") and not handled,
          "'Jarvis, switch to cloud mode': said so, the toy told to switch, and not passed on as a command")
    woken.append(True)
    heard_texts.append("Jarvis, switch to P.C. mode please")
    answer = hear(recording(1.5))
    check(answer.headers.get("X-Jarvis-Switch") == "pc" and spoken[-1][0] == "I'm already in PC mode, sir.",
          "'switch to PC mode', heard by the PC: already there")
    woken.append(True)
    heard_texts.append("Jarvis, what is the time")
    replies.append("Eight, sir.")
    answer = hear(recording(1.5))
    check("X-Jarvis-Switch" not in answer.headers, "and an ordinary answer switches nothing")
    asked = phone.PhoneServer._toy_switch_asked
    check(asked("Cloud mode.") == "cloud" and asked("go back to computer mode") == "pc"
          and asked("what is cloud mode") is None and asked("switch on the PC") is None,
          "'cloud mode', 'go back to computer mode'; 'what is cloud mode' is a question, not a switch")

    phone.transcriber.transcribe_wav = real_transcribe

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
      "talking is built only for DFRobot's board, and started with it")
task = talk_part[talk_part.index("void talkTask"):talk_part.index("static void *tlsCalloc")]
check("quietLevel * 0.98f + level * 0.02f" in task and "loudRun < TALK_START_FRAMES" in task
      and "TALK_PREROLL_FRAMES = 25" in talk_part and "quietRun >= TALK_END_FRAMES" in task
      and "TALK_MOST_MS = 10000UL" in talk_part,
      "it listens for speech against the room's own quiet, keeps half a second from before, ten seconds at most")
check('"X-Jarvis-Toy", button ? "button" : (named ? "wake" : "sound")' in talk_part and "code == 204" in talk_part
      and "TALK_BUTTON_PIN = 0;" in talk_part and "letGo(TALK_AFTER_ANSWER_MS);" in task,
      "says whether BOOT was held; plays nothing for a 204; and does not hear its own answer")
read_part = talk_part[talk_part.index("static float readFrame"):talk_part.index("static void letGo")]
check("out = in - dcIn + DC_KEEP * dcOut;" in read_part and "frame[i] = (int16_t)out;" in read_part
      and "total += fabsf(out);" in read_part,
      "the microphone's steady offset is taken out before its level is judged, and from what is sent")
check("TALK_MIC_CLOCK_PIN = 38" in talk_part and "TALK_MIC_DATA_PIN = 39" in talk_part
      and "TALK_SPEAKER_BCLK_PIN = 45" in talk_part and "TALK_SPEAKER_LRC_PIN = 46" in talk_part
      and "TALK_SPEAKER_DOUT_PIN = 42" in talk_part and "I2S_MODE_PDM_RX" in talk_part,
      "on DFRobot's microphone and speaker pins")
check("MALLOC_CAP_SPIRAM" in talk_part and '"/toy/talk"' in talk_part and "https.begin(link, url)" in talk_part
      and "NetworkClientSecure &link = talkLink;" in talk_part
      and "talkLink.setCACert(JARVIS_CA);" in talk_part and "setInsecure" not in talk_part,
      "into PSRAM, then to /toy/talk over its own connection, checked against JARVIS's authority")
check("taken += talkSpeaker.write(chunk, have);" in talk_part and "getStreamPtr()" in talk_part,
      "and the answer played as it arrives, not held whole, counting what the speaker took")
start_part = talk_part[talk_part.index("void startTalk()"):]
check("static void chime()" in talk_part and start_part.index("chime();") < start_part.index("xTaskCreatePinnedToCore(talkTask"),
      "a chime on the speaker when it is ready: heard, the speaker works")
setup = sketch[sketch.index("void setup() {"):sketch.index("void loop() {")]
check("mbedtls_platform_set_calloc_free(tlsCalloc, tlsFree);" in talk_part and "MALLOC_CAP_SPIRAM" in
      talk_part[talk_part.index("static void *tlsCalloc"):] and setup.index("talkTlsInPsram();") < setup.index("joinWifi();"),
      "TLS's larger pieces from PSRAM, set before the first connection")
check("TALK_RATE = 16000" in talk_part and "TOY_TALK_RATE = 16000" in (ROOT / "phone.py").read_text(encoding="utf-8"),
      "the board and JARVIS agree on the rate")

body = talk_part[talk_part.index("// The toy's body"):talk_part.index("// mbedTLS's memory")]
check("TOY_RING_PIN = 43" in body and "TOY_SERVO_PIN = 44" in body and "rmtInit(TOY_RING_PIN" in body,
      "the light ring on Gravity 43, the servo on 44")
check("LEDC_TIMER_3" in body and "LEDC_CHANNEL_7" in body and "ledcAttach" not in body.replace("ledcAttach cannot", "")
      .replace("`ledcAttach`", "").replace("Arduino's ledcAttach", "").replace("-- ledcAttach would", ""),
      "the servo on its own LEDC timer, never the camera's timer 0")
task = talk_part[talk_part.index("void talkTask"):talk_part.index("// The toy's body")]
check(task.index("toyMood = TOY_LISTENING;") < task.index("toyMood = TOY_THINKING;") < task.index("askAndPlay(got, button, named)")
      < task.index("toyMood = TOY_IDLE;", task.index("askAndPlay(got, button, named)"))
      and "toyMood = fond ? TOY_SPEAKING_FOND : TOY_SPEAKING;" in talk_part[talk_part.index("static int askAndPlay"):],
      "hearing you, waiting, speaking as the answer plays, off after")
setup = sketch[sketch.index("void setup() {"):sketch.index("void loop() {")]
check(setup.index("startToyBody();") < setup.index("startTalk();"), "the body is started before the talk")

tracking = body[body.index("void toySawPeople"):body.index("static uint8_t toyScale")]
check("seen[i].label == 0 && area > biggest" in tracking and "toyTurnSign = -toyTurnSign;" in tracking
      and "TOY_HEAD_LEAST" in tracking and "TOY_FOLLOW" in tracking,
      "the head turns toward the nearest person, never too far, and learns which way is toward")
detect = sketch[sketch.index("void detectTask"):sketch.index("void startDetection")]
check("toySawPeople(looked, lookedCount);" in detect, "fed by every look detection takes")
anim = body[body.index("void toyBodyTask"):]
check("toyScale(255, amount), toyScale(180, amount)" in anim and "toyRing(0, 0, TOY_BRIGHT);" in anim
      and "toyRing(0, toyScale(255, pulse), 0);" in anim and "TOY_SPEAKING_FOND" in anim,
      "thinking breathes amber, hearing stays blue, speech pulses green -- red when it is warm")
check('https.header("X-Jarvis-Mood") == "affection"' in talk_part and "toyVoice = level" in talk_part,
      "the ring pulses with his voice, red for an answer JARVIS marks warm")

# ---- PC mode and cloud mode, on the toy ----

cloud_part = sketch[sketch.index("// ---- PC mode and cloud mode"):sketch.index("int reportOnline() {")]
check('#if defined(JARVIS_BOARD_DFR1154) && __has_include("toy_cloud.h")' in sketch
      and sketch.count("#define JARVIS_TOY_CLOUD") == 1,
      "the cloud only on the toy, and only once tools/toy_cloud_setup.py has written toy_cloud.h")
check('toyPrefs.getBool("cloud", true)' in cloud_part and 'toyPrefs.putBool("cloud", cloud)' in cloud_part,
      "the mode kept in the toy's flash, cloud until told otherwise")
check("return toyCloudMode || !pcReachable;" in cloud_part, "PC mode with the PC not answering: the cloud")
check('cloudUrl("/health")' in cloud_part and "lastDelivered = millis();" in cloud_part,
      "the cloud answering counts as JARVIS reached, so the board does not restart for want of the PC")
ask = talk_part[talk_part.index("static int askAndPlay"):talk_part.index("static int playAnswer(HTTPClient &https, bool fond) {")]
check('"X-Jarvis-Fallback", "pc"' in ask and "cloud && !toyCloudMode" in ask,
      "falling back from the PC, the toy says so, and the cloud tells you")
check("return askAndPlay(pcm, button, named, true);" in ask and "!cloud && code < 0" in ask,
      "a PC that stops answering mid-question: the same question asked of the cloud")
check('https.header("X-Jarvis-Switch")' in ask and 'saveToyMode(switched == "cloud");' in ask,
      "'switch to ... mode' answered by either side is kept")
check("cloud ? TOY_CLOUD_TOKEN : JARVIS_TOKEN" in ask, "each side gets its own token")
check(sketch.count("setCACert(TOY_CLOUD_CA);") == 3 and "setInsecure()" not in sketch.replace("never setInsecure()", ""),
      "the cloud checked against its own authorities, on every connection to it -- never setInsecure")
seen_part = sketch[sketch.index("void sendSeen"):sketch.index("// FEMTO-ML runs each conv")]
check('cloudUrl("/camera/seen")' in seen_part and "toyAnnounceDue = true;" in seen_part,
      "what the toy sees goes to the cloud in cloud mode, which says when there are words for it")
face_part = sketch[sketch.index("static void sendFace"):sketch.index("void reserveDetection")]
check('cloudUrl("/camera/face")' in face_part, "and the faces it finds")
check("sayWhatWasSeen();" in task and 'cloudUrl("/toy/announce")' in talk_part,
      "the words fetched and said between recordings, never over anyone")
loop = sketch[sketch.index("void loop() {"):]
check(loop.index("if (toyCloudMode) {") < loop.index("reportOnline()") and "pcReachable = online == HTTP_CODE_OK;" in loop,
      "cloud mode asks the PC nothing; PC mode's heartbeat says whether the PC is there")
check('"X-Jarvis-Faces"' in ask and "toyFacesWanted = facesWanted > 10 ? 10 : facesWanted;" in ask,
      "'learn my face': the cloud asks for faces, at most ten")
faces_part = detect[detect.index("#ifdef JARVIS_FACES"):]
check("if (toyFacesWanted > 0) {" in faces_part and "toyFacesWanted--;" in faces_part
      and faces_part.index("faceSent = false;") < faces_part.index("sendFace(face);"),
      "and the face finder sends one from each look with someone in it until they are given")
check("bool arriving = personHere && !faceSent && faceLooks < FACE_LOOKS;" in faces_part
      and "(arriving || learning || probing)" in faces_part and "learning = toyFacesWanted > 0;" in faces_part,
      "while learning, the face finder runs on every look, even when YOLO sees no person")
check("toyLearnUntil = millis() + TOY_LEARN_MS;" in ask and "toyFacesWanted = 0;" in faces_part,
      "and learning stops after a minute, as the cloud's does")
check(ask.index("int played = playAnswer(https, fond);") < ask.index("toyFacesWanted = facesWanted > 10 ? 10 : facesWanted;"),
      "learning starts once look at me has been said, so he is facing it and the ring shows white")
check("probing = true;" in faces_part and "toyUsesCloud() && millis() - lastProbe >= FACE_PROBE_MS" in faces_part
      and "lastFaceSent = millis();" in faces_part and "(arriving || learning || probing)" in faces_part,
      "in cloud mode faces are looked for every 10 s, person or not, and one in view sent each minute")
check("} else if (toyFacesWanted > 0) {" in anim and "toyRing(toyScale(255, amount), toyScale(255, amount), toyScale(255, amount));" in anim,
      "the ring breathes white while it learns a face")
check('"\\"say\\":true"' in face_part and "toyAnnounceDue = true;" in face_part,
      "a known face greeted by name, said on the toy")
ca = (ROOT / "arduino" / "jarvis_camera" / "toy_cloud_ca.h").read_text(encoding="ascii")
check(ca.count("-----BEGIN CERTIFICATE-----") == 4 and "TOY_CLOUD_CA[]" in ca,
      "the cloud's authorities: Google's and Let's Encrypt's roots, public, in git")
ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
check("arduino/jarvis_camera/toy_cloud.h" in ignored, "the toy's cloud token kept out of git")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
