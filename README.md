# JARVIS

A voice assistant for Windows, with a holographic HUD, that does as much as
it can on your own PC for free and calls a language model only when a
request needs one.

Say "Jarvis" and ask. He opens and closes programs, sites and projects;
reads, writes and organises files; keeps notes, memory, reminders and a
calendar; watches the markets; researches and writes reports; drives
connected services (GitHub, OBS, TradingView and anything else that speaks
the Model Context Protocol); runs your own protocols ("initiate startup
protocol"); and talks to ESP32 boards on the wifi: room sensors with a 3D
house hologram and a record of every reading, and cameras that take a
picture only when asked.

## The rule that shapes it

Anything that can be answered locally is: a table of phrases and a set of
narrow, checked shapes take most commands with no model call at all, and
only what is left goes to a model, with providers that fail over to one
another. Outside text is always treated as data, never as instructions, and
anything that changes something outside JARVIS asks first unless you have
said it need not.

## Running it

Windows, Python 3.14.

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
copy installer\.env.example .env      # then fill in the keys for what you use
python main.py
```

The Vosk speech model goes in `model\` (not kept in git; any model from
[alphacephei.com/vosk/models](https://alphacephei.com/vosk/models)).
`installer\README.md` builds a Windows installer instead.

## Tests

```powershell
python tools/run_tests.py            # every suite
python tools/run_tests.py house      # only those with "house" in the name
```

Each suite in `tools/` runs on its own, offline, in a sandboxed JARVIS
folder, so nothing touches your own files. GitHub Actions runs the same on
every push to the working branch (`.github/workflows/tests.yml`); main is
only moved on to a branch that passed.

## The boards

`arduino/` holds the ESP32 sketches (room sensor, camera), built with the
Arduino IDE or PlatformIO; `python tools/esp32_setup.py` writes each board's
configuration. They report to JARVIS over HTTPS, checking his own
certificate authority.

## More

- `commands.md`: everything you can say, by topic.
- `JARVIS.md`: how each part works, and why.
- `python tools/build_blender_extension.py`: builds the Blender bridge for
  Blender's Install from Disk.
