"""Checks for the toy's music (internet radio, from the cloud's X-Jarvis-Play): the firmware plays the
station's MP3 stream itself, BOOT pauses and resumes it, "Hey Jarvis" quiets it for a command, and
the talk always gets the speaker back for its answers.

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
check("HTTP/1.0" in radio and "Icy-MetaData: 0" in radio and "hop < 4" in radio,
      "the stream asked for whole and without metadata in it, following at most three redirects")
check("MP3FindSyncWord" in radio and "ERR_MP3_INDATA_UNDERFLOW" in radio and "ERR_MP3_MAINDATA_UNDERFLOW" in radio
      and "speakerAt(info.samprate);" in radio and "* 0.5f" in radio and "RADIO_VOLUME" in radio,
      "MP3 decoded on the chip, at the station's own rate, stereo made mono, quieter than talk")
check("xSemaphoreTake(speakerLock, portMAX_DELAY);" in radio and "speakerAt(TALK_RATE);\n    xSemaphoreGive(speakerLock);" in radio,
      "one speaker: the music holds it while it plays and gives it back at the talk's rate")
check("speakerForTalk();\n    int answered = askAndPlay(got, button, named);\n    speakerDone();" in task
      and "speakerForTalk();\n        sayWhatWasSeen();\n        speakerDone();" in task
      and "radioHeld = true;" in radio[radio.index("static void speakerForTalk()"):],
      "every answer takes the speaker from the music first")
check("if (radioPlaying()) {" in task and 'radioState = RADIO_PAUSED;\n        Serial.println("[jarvis] radio: paused (BOOT)");' in task,
      "BOOT pauses the music")
check("RADIO_RESUME_TAP_MS" in task and 'radioState = RADIO_PLAYING;\n        Serial.println("[jarvis] radio: resumed (BOOT)");' in task,
      "and a brief press resumes it; held, it is a question as ever")
check("millis() - wakeHeardAt > 1000UL" in task and "radioHeld = true;\n      wakeHoldAt = millis();" in task,
      "Hey Jarvis over the music quiets it for the command")
check("RADIO_WAKE_HOLD_MS" in task and "radioHeld = false;   // the music, if any, carries on" in task,
      "and it comes back after the answer, or when nothing follows")
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
