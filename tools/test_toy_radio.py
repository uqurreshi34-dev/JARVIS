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
check("static int radioTo16k(" in radio and "7000.0f / rate" in radio and "for (int k = 0; k < 2; k++) {" in radio
      and "keep = rate" not in radio,
      "one rate on the speaker: any station's rate low-passed (two Butterworth stages at 7 kHz, not the one-pole "
      "filter that let cymbals through as crackle, 1 November 2026) and read off at 16 kHz, so the echo canceller "
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
      and "RADIO_DUCKED = 0.03f" in radio and "radioDucked ? RADIO_DUCKED : RADIO_VOLUME" in radio
      and "radioHeld" not in sketch,
      "Hey Jarvis over the music ducks it, still playing, for the command (as Alexa does); low enough that its "
      "words are not taken for the command (1 November 2026, 'pause' heard as something else at 0.08)")
check("RADIO_WAKE_HOLD_MS" in task and "radioDucked = false;   // the music, if any, back up (or doing what it was told)" in task
      and task.count("radioDucked = false;") == 3,
      "and it is back up after the answer, when nothing follows, or when what was said stays on the toy")
check(sketch.count("echoHeard(") >= 3 and "echoHeard(chunk, have);" in sketch,
      "what the speaker plays, music and answers, kept to be taken back out of the microphone")
check('#include "esp_aec.h"' in sketch and "aec_pro_create(16, 1, 5)" in radio and "aec_pro_create(16, 1, 4)" not in sketch
      and "echo: canceller ready" in radio,
      "1 November 2026, the canceller never ran: Espressif's echo canceller (ESP-SR, already in the core) in mode 5, "
      "as this core's library makes only modes 5 and 6 and gave nothing for 4")
check("ECHO_LAG = 1760" in radio and "echoRead = written - echoLag;" in radio and "echoRead = echoWritten - echoLag;" in radio,
      "its reference starts 110 ms behind the speaker, from where the music was last found after that")
check("if (ahead < (uint32_t)count) {" in radio and "ahead < ECHO_LAG + count" not in sketch,
      "1 November 2026, stuck at 6 dB: the reference quiet only when the music is not written yet, never "
      "whenever it was under ECHO_LAG ahead, which fed the canceller quiet each time the music came in a burst")
line = radio[radio.index("static void lineUpEcho()"):radio.index("static float cancelEcho(")]
check("ECHO_SLOTS = 500" in radio and "ECHO_SEARCH = 4000" in radio and "ECHO_LEAD = 160" in radio
      and "best < 0.4f || playedLevel < 30.0f" in line and "bool agrees = abs(bestOff - echoLastOff) <= 4 * ECHO_SLOT;" in line
      and "(!agrees && best < 0.6f)" in line
      and "echoRead -= move;" in line and "if (echoSlots >= ECHO_SLOTS) {\n        lineUpEcho();" in radio,
      "the toy finds the music in what it heard itself, each half second (250 ms either way), and keeps its copy "
      "10 ms ahead of the echo on a clear match or two fair ones that agree (1 November 2026: a second's "
      "windows, 150 ms either way and two to agree never lined up on the toy)")
check("match %.2f with the music %.1f ms behind its copy" in radio and "times sound lost for " in radio and "being read late" in radio
      and "ms of each 20 to" in radio and "micEvents.on_recv_q_ovf = micOverflowed;" in sketch
      and "talkBusyMicros += micros() - busyFrom;" in sketch,
      "the ten-second line says how clearly the music was found and where, whether the microphone lost sound for "
      "being read late, and how long each frame takes to hear")
check("ECHO_RING = 32768" in radio, "two seconds of the music kept: a second to measure, and the echo's way")
check("aecAfresh = true;" in radio[radio.index("static void speakerDone()"):] and "radioState != RADIO_PLAYING || aecAfresh" in radio,
      "after each answer the canceller starts its place again: the microphone was not read while the toy spoke")
check("if (radioPlaying()) {\n      lastDelivered = now;\n    } else {\n      cloudPing();" in sketch,
      "1 November 2026, 'BIGNUM - Memory allocation failed': no cloud ping while music plays -- the music arriving "
      "shows the network works, and a second secure connection ran internal memory out")
check("lined up %u times" in radio and "pieces with no music yet to compare" in radio,
      "the ten-second line says how often it lined up, and how often it had nothing to compare")
check('the echo canceller %s' in radio and 'echoCanceller ? "on" : "OFF"' in radio,
      "whether the canceller is on said each time the music starts, not only at start-up")
check("radioWaits = true;" in radio and "radioWaits = false;" in radio
      and radio.index("if (radioWaits) {") < radio.index("int ready = client.available();"),
      "1 November 2026, 'esp-aes: Failed to allocate memory' in the station's connection: the music reads "
      "nothing while the talk asks the cloud, so one secure connection is busy at a time")
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
check('xTaskCreatePinnedToCore(radioTask, "radio", 12288, nullptr, 2, nullptr, 0)' in sketch
      and "WithCaps(radioTask" not in sketch and "speakerLock = xSemaphoreCreateMutex();" in sketch,
      "the music on its own task on core 0, leaving core 1 to the wake word and the echo canceller (together each "
      "20 ms of sound took 22 ms to hear), above detection's priority so it is never kept waiting (it crackled), "
      "its stack in internal memory: lwIP stops the board if a connection is opened from a stack in PSRAM "
      "(1 November 2026)")
detect = sketch[sketch.index("void detectTask(void *) {"):]
check("#ifdef JARVIS_TOY_CLOUD\n    if (radioPlaying()) {\n      continue;\n    }\n#endif" in detect[:3000],
      "1 November 2026, sound lost 50 to 90 times every ten seconds: camera detection (half of it on core 1) "
      "rests while music plays, faces with it")
check("bool trusted = echoFoundAt && millis() - echoFoundAt < 3000UL;" in radio
      and "memcpy(frame, asHeard, sizeof(asHeard));" in radio and "echoFoundAt = millis();" in radio,
      "the canceller's sound used only while it has found the music in the last three seconds; else what was "
      "heard goes on as it was (out of line, it took half of the voice with the music)")
check("(%.1f in the canceller)" in radio and "aecMicros += micros() - aecFrom;" in radio,
      "the time the canceller itself takes, logged")
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
