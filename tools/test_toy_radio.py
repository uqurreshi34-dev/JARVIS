"""Checks for the toy's music (internet radio, from the cloud's X-Jarvis-Play): the firmware plays the
station's MP3 stream itself, BOOT pauses and resumes it, "Hey Jarvis" ducks it for a command, the
echo canceller takes the toy's own sound out of what it hears, and the talk always gets the speaker
for its answers.

    python tools/test_toy_radio.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = []


def check(ok, label):
    RESULTS.append((bool(ok), label))
    print(("PASS " if ok else "FAIL ") + label)


sketch = (ROOT / "arduino" / "jarvis_camera" / "jarvis_camera.ino").read_text(encoding="ascii")
radio = sketch[sketch.index("// ---- Music: internet radio"):sketch.index("// Sends [pcm] bytes of recording")]
task = sketch[sketch.index("void talkTask(void *)"):sketch.index("static void chime()")]
ask = sketch[sketch.index("static int askAndPlay("):sketch.index("static int playAnswer(HTTPClient &https, bool fond) {")]

check(radio.startswith("// ---- Music") and "#ifdef JARVIS_TOY_CLOUD" in sketch[sketch.index("// ---- Music") - 40:
                                                                              sketch.index("// ---- Music")],
      "music only with the toy's cloud brain, which finds the stations")
check('"X-Jarvis-Play"' in ask and "collectHeaders(wanted, 4)" in ask
      and ask.index('String play = https.header("X-Jarvis-Play");') < ask.index("int played = playAnswer(https, fond);")
      < ask.index("radioCommand(play);"),
      "the cloud's X-Jarvis-Play read before the answer ends the request, and acted on once it has been said")
check('play == "stop"' in radio and 'play == "pause"' in radio and 'play == "resume"' in radio
      and 'play.startsWith("https://")' in radio, "a station's address, or pause, resume and stop")
check(radio.index("client.stop();\n    client.setCACertBundle(radioRoots, radioRootsEnd - radioRoots);")
      < radio.index("client.connect(host.c_str(), port)"),
      "1 November 2026, 'No CA Chain is set': the roots set again after stop(), which wipes them, before each connection")
check("setCACertBundle(radioRoots, radioRootsEnd - radioRoots)" in radio and "setInsecure" not in radio
      and '!url.startsWith("https://")' in radio,
      "only HTTPS stations, checked against the core's own bundle of public roots, never setInsecure")
check("client.setTimeout(RADIO_ANSWER_MS);" in radio and "RADIO_ANSWER_MS = 8000UL" in radio
      and "client.setTimeout(8);" not in radio,
      "1 November 2026, 'the station answered 0': the wait for its answer in milliseconds, as Stream counts it")
check("HTTP/1.0" in radio and "Icy-MetaData: 0" in radio and "hop < 4" in radio,
      "the stream asked for whole and without metadata in it, following at most three redirects")
check("MP3FindSyncWord" in radio and "ERR_MP3_INDATA_UNDERFLOW" in radio and "ERR_MP3_MAINDATA_UNDERFLOW" in radio
      and "int made = radioTo16k(pcm, samples, info.samprate, out);" in radio and "speakerAt" not in sketch
      and "RADIO_VOLUME = 0.5f" in radio,
      "MP3 decoded on the chip, stereo made mono, made 16 kHz like the talk, quieter than talk")
check("static int radioTo16k(" in radio and "keep = rate > TALK_RATE * 1.5f" in radio,
      "one rate on the speaker: any station's rate low-passed and read off at 16 kHz, so the echo canceller "
      "hears what the speaker plays")
check("if (xSemaphoreTake(speakerLock, 0) == pdTRUE) {\n      talkSpeaker.write((uint8_t *)out, made * 2);\n"
      "      echoHeard((uint8_t *)out, made * 2);\n      xSemaphoreGive(speakerLock);" in radio
      and "portMAX_DELAY" not in radio[radio.index("void radioTask"):],
      "one speaker: while the talk speaks, the music's pieces are let go and it carries on live underneath")
check("speakerForTalk();\n    int answered = askAndPlay(got, button, named);\n    speakerDone();" in task
      and "speakerForTalk();\n        sayWhatWasSeen();\n        speakerDone();" in task,
      "every answer takes the speaker from the music first")
check("if (radioPlaying() && !radioDucked) {" in task
      and 'radioState = RADIO_PAUSED;\n        Serial.println("[jarvis] radio: paused (BOOT)");' in task,
      "BOOT pauses the music")
check("RADIO_RESUME_TAP_MS" in task and 'radioState = RADIO_PLAYING;\n        Serial.println("[jarvis] radio: resumed (BOOT)");' in task,
      "and a brief press resumes it; held, it is a question as ever")
check("millis() - wakeHeardAt > 1000UL" in task and "radioDucked = true;\n      wakeHoldAt = millis();" in task
      and "RADIO_DUCKED = 0.08f" in radio and "radioDucked ? RADIO_DUCKED : RADIO_VOLUME" in radio
      and "radioHeld" not in sketch,
      "Hey Jarvis over the music ducks it, still playing, for the command (as Alexa does)")
check("RADIO_WAKE_HOLD_MS" in task and "radioDucked = false;   // the music, if any, back up (or doing what it was told)" in task
      and task.count("radioDucked = false;") == 3,
      "and it is back up after the answer, when nothing follows, or when what was said stays on the toy")
check(sketch.count("echoHeard(") >= 3 and "echoHeard(chunk, have);" in sketch,
      "what the speaker plays, music and answers, kept to be taken back out of the microphone")
check('#include "esp_aec.h"' in sketch and "aec_pro_create(16, 1, 4)" in radio and "ECHO_LAG = 1200" in radio
      and "echo: canceller ready" in radio,
      "Espressif's echo canceller (ESP-SR, already in the core), 16 ms frames, in PSRAM, its reference lagged to "
      "meet the echo")
check(task.count("cancelEcho(frame)") == 2
      and task.index("float cleaned = cancelEcho(frame);") < task.index("listenForWake(frame);"),
      "1 November 2026, Hey Jarvis heard over the music only with the speaker held away: every frame cleaned of "
      "the toy's own sound before the ear hears it, waiting and recording")
check("dB quieter" in radio, "how much quieter the canceller makes what is heard, logged every ten seconds")
check("loudest * 0.25f > endBelow ? loudest * 0.25f : endBelow" in task,
      "with music playing, a command ends when the voice drops away, not when the room is silent")
check("static float cancelEcho(int16_t *) { return -1.0f; }" in sketch and "static void echoHeard(const uint8_t *, size_t) {}" in sketch,
      "without the cloud brain there is no music and nothing to cancel")
check("loudRun = 0;\n        continue;" in task or "loudRun = 0;\n        continue;\n      }" in task,
      "the music itself is never recorded as speech")
check("radioState != RADIO_PLAYING" in task, "what the camera sees waits until the music is paused or stopped")
check("RADIO_TRIES" in radio and 'radioState = RADIO_OFF;' in radio, "a station that keeps failing is let go")
check('xTaskCreatePinnedToCore(radioTask, "radio", 12288, nullptr, 1, nullptr, 1)' in sketch
      and "WithCaps(radioTask" not in sketch and "speakerLock = xSemaphoreCreateMutex();" in sketch,
      "the music on its own task, started with the talk, its stack in internal memory: lwIP stops the board "
      "if a connection is opened from a stack in PSRAM (1 November 2026)")
check("if (!decoder) {" in radio and "heap_caps_malloc_extmem_enable(32);" in radio
      and "MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT" not in radio[radio.index("void radioTask"):],
      "1 November 2026, the next question's secure connection out of internal memory: the decoder made "
      "only when a station first plays, and all of the music's memory in PSRAM")
check("WAKE_TENSOR_ARENA, MALLOC_CAP_SPIRAM" in sketch and "WAKE_TENSOR_ARENA, MALLOC_CAP_INTERNAL" not in sketch,
      "and the wake word's working memory in PSRAM too, keeping internal memory for the connections")
check("radioAsked++" not in sketch and "toyFacesWanted--" not in sketch,
      "no ++ or -- on the shared (volatile) counters: the build is free of warnings")

failed = [label for ok, label in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed")
sys.exit(1 if failed else 0)
