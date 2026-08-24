# JARVIS

A local voice assistant for Windows. Everything it can do without a
language model, it does without one.

This file is the single source of truth: what it does, how it is built, and
the rules that keep it trustworthy. Keep it current — when a capability is
added, add it here.

---

## What it is

You say "Jarvis" and then an instruction. It listens through the microphone,
transcribes locally with Whisper, works out what you meant, does it, and
answers aloud in a British voice. A heads-up display shows what it heard,
what it is doing, and live machine telemetry.

**62 intents. 237 spoken phrases resolve locally with no API call.**

---

## The rule that shapes everything: the free path

Every command is matched against a table of known phrases first. Only if
nothing matches does it cost an API call.

```
you speak → Whisper (local) → phrase table (free) → fuzzy match (free)
                                    ↓ no match
                              language model (costs)
```

In practice almost everything is free: opening apps, files, notes,
clipboard, news, charts, spelling, screen control, volume, timers. The model
is a fallback for phrasing nobody anticipated, plus general questions.

**When adding a capability, add its phrases to the free path.** An intent
that only the model can reach is slower and costs money on every use.

---

## Capabilities

### Files — `actions/files.py`
Everything lives in `C:\Users\<you>\JARVIS\`. Nothing outside it can be
touched; every path is resolved and checked against that root.

Create (txt, md, docx, pdf, csv), read, append, copy, remove a line, list,
count, copy contents to the clipboard. A spoken name matches any extension,
so "business" finds `business.docx`. Word documents are read including
their tables.

### Notes — `actions/notes.py`
A timestamped list in `notes.txt`. Add, read, remove one entry, clear all.
Distinct from files: "add milk to my notes" is the notes skill, "copy my
notes" treats `notes.txt` as a file.

### Spelling — `actions/proofread.py`
Local dictionary, no API. Checks txt and docx including table text. Lists
findings, writes a report, or corrects in place — Word documents keep their
formatting because replacements happen run by run. `spelling-ignore.txt`
holds words to leave alone, permanently and across all files.

### Charts — `actions/charts.py`
Reads a CSV, asks which two columns, draws a bar or line chart with
matplotlib, shows it in a panel, offers to save. Numeric axes are sorted so
a line chart reads correctly.

### News — `actions/news.py`
BBC and Sky RSS, ten regions, no key. Headlines appear in a panel with live
Bitcoin and currency prices along the foot. Stories can be expanded and
their pictures shown.

### Camera — `actions/camera.py`
Captures through pygrabber (pure Python DirectShow). Sends one frame to a
vision model and says what it sees. Detects a too-dark frame locally and
says so rather than paying for an answer that cannot exist.

### Screen control — `actions/screen_control.py`
Windows UI Automation. Describes the active window, clicks a named control,
types into whatever has focus. Typing goes via the clipboard so special
characters cannot be interpreted as key combinations.

### Machine — `actions/system.py`, `desktop.py`, `screen.py`, `battery.py`
Time, weather, CPU and memory, volume and media keys, screenshots,
minimise and restore. Battery warnings are spoken unprompted at 80% while
charging, and at 50% and 30% on battery.

### Reminders — `actions/reminders.py`
Spoken timers that announce themselves when due.

---

## How a command travels

```
voice.py        hears you, checks the wake word
transcriber.py  turns audio into text (Whisper or Vosk)
commands.py     decides what you meant
actions/*.py    does it
speech.py       says the reply
hud.py          shows the state
```

`main.py` runs the loop on a worker thread. **Every Qt widget is driven by
signals from that thread — never called directly.** Calling a widget method
from the worker freezes the application.

---

## The rules that keep it safe

**Destructive things are exact-match only.** Clearing notes, clearing the
clipboard, cancelling reminders, hiding the news, saving a picture. A near
miss must never delete or overwrite. This rule exists because a fuzzy match
once wiped a notes file.

**Anything hard to undo asks first.** Overwriting a file, clicking a control
whose name suggests sending or deleting, correcting a document. The
confirmation re-finds the control at the moment you say yes, so it cannot
act on something that has since changed.

**Outside text is data, never instructions** — `actions/safety.py`. Folder
names, file contents and words held up to the camera can be written to look
like orders. They are filtered before reaching a model.

**Everything that changes something is logged** — `actions/journal.py`.
`jarvis-log.txt` in the JARVIS folder records every command, its route
(local or model), and every change. Full logs are archived, never
discarded.

**Pronouns resolve only when explicit and recent.** "copy it to my
clipboard" works for sixty seconds after naming something. Never for
removal or clearing, where a wrong guess costs data.

---

## Things learned the hard way

Each of these cost real time. They are here so they are not repeated.

**Optional dependencies are imported where they are used, not at the top.**
`faster_whisper`, `whisper`, `openai`, `pygrabber` are all optional. Hoisted
to the top of a file, a missing one takes the whole application down instead
of falling back.

**Prefer pure Python that calls Windows' own APIs.** Smart App Control
blocks unsigned third-party binaries — it blocked ctranslate2 and numba.
Microsoft's own DLLs are signed, so anything reaching them through `ctypes`
or `comtypes` always works. This is why capture uses pygrabber, not OpenCV.

**Measure before fixing.** An eight-second delay was blamed on
transcription, then synthesis, then the audio device. It was the microphone
still recording, because a fan kept the level above the endpoint threshold.
Every earlier fix addressed something that was not the problem.

**Speaking is slower than thinking.** Reading a timestamped filename aloud
takes fifteen seconds. Keep replies short; put detail on screen or in a
file.

**An empty reply is a failure, not a success.** A provider returning an
empty string was treated as an answer, so the fallback never ran and no
error appeared anywhere.

**A pending question must not swallow a real command.** If the next thing
said is a recognised command, do that and let the question lapse.

---

## Running it

```
python main.py
```

Say "Jarvis" then an instruction. "Jarvis, quit" shuts down cleanly —
Ctrl+C skips the cleanup.

Settings live in `.env`: provider keys, `STT_ENGINE`, `WHISPER_MODEL`,
voice, weather location.

### Diagnostics

| Script | Answers |
|---|---|
| `voice_lab.py` | which voice and delivery to use |
| `close_check.py` | why an application will not close |
| `audio_check.py` | whether cached speech is sound |
| `speed_test.py` | where the time in a reply goes |
| `test_console.py` | typed commands, no microphone |

`TIMING = True` in `main.py`, `voice.py`, `speech.py` and `transcriber.py`
prints where each stage's time goes.

---

## Still to do

- Fixing spelling in whatever is on screen, not just in a file
- Notices held for you when they fire while the app is closed
- Memory that survives a restart, so it knows you next time
- Interrupting it mid-sentence
