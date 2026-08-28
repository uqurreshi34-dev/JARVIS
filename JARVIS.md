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

**98 intents. 462 spoken phrases resolve locally with no API call.**

---

## The rule that shapes everything: the free path

Every command is matched against a table of known phrases first. Only if
nothing matches does it cost an API call.

```
you speak → Whisper (local) → phrase table (free) → fuzzy match (free)
                                    ↓ no match
                              language model (costs)
```

In practice almost everything is free: apps, files, notes, clipboard, news,
charts, spelling, screen control, calendar, memory, volume, timers. Three
things always need a live request to actually carry out, even though
recognising the command itself is free: a general question and the camera
both cost money, through a language or vision model; finding a fetched
image costs a request too, but not money — Unsplash's own call is free,
just not local.

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

### Documents — `actions/documents.py`

Drag Word documents, PDFs, TXT, Markdown, CSV or JSON files directly onto the JARVIS HUD to create a temporary working set. Once loaded, JARVIS can answer questions about individual documents, find information across the set, compare documents and combine information from several documents.

Documents are read and classified locally when dropped; no API call is made for loading them. Classification is content-based, not filename-based — a file named `Doc4.docx` is still correctly identified as a contract from what it actually says — and that classification is passed to the model as context for every question. Spoken confirmations and "what documents do I have" use the actual filename rather than the classified type, since you're looking at the list yourself and can judge it; the model answering a question is the one place a bare, unverified filename shouldn't be trusted on its own.

Every document is capped individually to a fixed size before anything else happens, regardless of how many other documents are already loaded — a large file dropped second or third gets the same useful truncated read a lone large file does, rather than being refused outright. The working set as a whole holds up to eight documents and 60,000 extracted characters; a document that would exceed that alongside what's already loaded is refused without disturbing the documents already there, and JARVIS says so specifically — it would always have fit on its own.

The working-set count is shown on the HUD only when at least one document is loaded. Dragging files does not produce a spoken response, to avoid unnecessary speech — deliberate, not an oversight.

Questions about the loaded documents use one normal language-model request with the document contents supplied as context, each wrapped as clearly labelled, untrusted data — the same guard used everywhere else outside text reaches a prompt, and the first place several untrusted documents go into one prompt together. This supports questions such as "which document has the shorter termination period", "which contract has the higher fees", "which document contains confidentiality", and a vague, unnamed reference like "what does this file say" or "what's in this document", which is read as being about the working set rather than the visible screen. When comparing documents, JARVIS names the actual document rather than referring to them as "document one" or "document two".

Say "clear my documents" or "clear the documents" to empty the working set — exact phrase only, like clearing notes or the clipboard. Say "what documents do I have" to hear what's currently loaded without clearing it.

### Notes — `actions/notes.py`
A timestamped list in `notes.txt`. Add, read, remove one entry, clear all.
Distinct from files: "add milk to my notes" is the notes skill, "copy my
notes" treats `notes.txt` as a file.

### Memory — `actions/memory.py`
What JARVIS knows about you, in `memory.txt`, one fact per line and
editable by hand. Some facts change behaviour rather than being recited:

| Say | Effect |
|---|---|
| `my name is AvidCoder` | used in the greeting |
| `I am based in Birmingham` | weather follows it, geocoded once |
| `I prefer short answers` | caps the length of spoken answers |
| `my default project is JARVIS` | "open my project" knows which |
| `my calendar is local` | stops pushing events to Outlook |

Facts are data, never instructions: anything shaped like an order is
refused at the point of storing, so memory cannot become a way round the
rules.

### Patterns — `actions/patterns.py`
Habits JARVIS notices from what you actually do, in `patterns.txt`,
deliberately a separate file from `memory.txt`. Memory holds things you
stated outright; a pattern is a conclusion drawn from watching
behaviour. Keeping them apart means you can always tell "JARVIS knows
this because I said so" from "JARVIS is guessing this from habit" —
blurring that line would quietly undermine the reason memory is
trustworthy in the first place.

Built entirely from the journal, which already records every command's
intent and time — no new tracking, just reading history that already
exists. `journal.command_history()` reads across rotated archives as
well as the live log, since several days of continuous history is the
whole point and a rotation mid-way would otherwise lose it silently.

A pattern must appear at least five times, on at least four distinct
calendar days, clustered within a thirty-minute window, before it is
even eligible to be mentioned. Deliberately conservative: better to stay
quiet a while longer than to announce a habit from a handful of
occurrences in one busy afternoon. Where an intent needs a subject the
subject is part of the pattern's identity, so a nine o'clock Bitcoin
report and a five o'clock Ethereum report stay two separate habits
rather than merging into one meaningless "market report" pattern.

Nothing is ever promoted silently. A detected pattern is offered once,
and only running it automatically after you say yes; declining removes
it rather than leaving it to be asked again. Each pattern carries a
short spoken label ("the weather pattern", "the bitcoin markets report
pattern") so one can be removed by name without touching the others,
and clearing all of them at once needs its own exact phrase.

Unlike `memory.txt`, this file is **not** size-bounded. Memory can trim
old facts safely because a fact costs nothing to state again; there is
no equivalent for multi-day behavioural evidence, and trimming it would
delete exactly the history a pattern needs to exist.

### Git — `actions/git_tasks.py`
Reads a repository's state, and commits when told to. The repository is
set with `JARVIS_REPO` in `.env` — a Windows path is close to impossible
to dictate reliably, so the durable setting belongs in a file. Saying
"remember my repo is ..." still works and takes precedence, for
switching mid-session.

This is the first thing in JARVIS that can change a repository, so it is
deliberately the narrowest useful version of that. Three constraints,
each by construction rather than by prompt:

- The only write it can perform is `commit`. No `add`, `push`, `reset`
  or `checkout`, so staging stays the user's job and JARVIS can only
  ever commit what was already chosen deliberately.
- Every git call is a fixed argument list, never a shell string, and
  the message is passed as a single argument. Shell metacharacters in a
  commit message commit as literal text and execute nothing.
- Nothing commits without an explicit spoken yes, through the same
  confirmation path as any other hard-to-undo action.

The message itself is drafted by the language model from the staged
diff, framed as data rather than instructions like any other outside
text. That makes this the one feature here that sends your code off the
machine — worth knowing plainly rather than discovering later.

Signed commits keep working: JARVIS runs real git, so a GPG passphrase
prompt appears exactly as it would from a terminal. It never types into
a terminal and doesn't care which window has focus.

### Calendar — `actions/diary.py`
JARVIS keeps his own calendar in `calendar.txt`, so it works whether or not
any calendar application exists. Add, remove, list, clear — clearing asks
first. Spoken dates are understood: "March the second twenty twenty seven",
"tomorrow", "next Friday", "the twenty fifth of December", with times.

Every event also writes an `.ics` into `JARVIS\invites\`, and is pushed to
the real Microsoft calendar through the Graph API (`outlook.py`), which
reaches Outlook desktop and Outlook web alike. The local file remains the
source of truth; Outlook is layered on top.

At startup he reads out what is due in the next two days, unprompted.

### Spelling — `actions/proofread.py`
Local dictionary, no API. Checks a file or whatever is on screen. Lists
findings, writes a report, or corrects in place — Word documents keep their
formatting because replacements happen run by run, and capitalisation is
preserved so "Thas" becomes "That". Screen text belongs to another
application, so the corrected version goes on the clipboard rather than
being typed over your work. `spelling-ignore.txt` holds words to leave
alone, permanently.

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

### Images — `actions/images.py`, `actions/image_choices.py`
Fetches from Unsplash (a free request, not a paid one — see the cost note
above) and offers three candidates rather than committing to the first
result, since a single result too often has an unrelated head or hand in
frame. Say or click one, two, or three; unclear replies get asked again
rather than silently abandoned, and the question stays open for a few
minutes so it survives a pause. `save the image` and `save the picture`
are the same command now — whichever is actually on screen, the fetched
photo or the camera's, is what gets saved.

Rotating, resizing, and restoring are pure Pillow, no network involved.
Every render works from the pristine fetched bytes rather than the last
edit, so repeated rotating and resizing never loses quality. Restoring
undoes both the rotation and the zoom — not size alone, on the reasoning
that "restore" means put it back, not undo one specific edit.

### Browsing — `actions/browser.py`
Read-only: navigate, search, read a page's actual text, list its headings
and links, save one to a file. Fully on the free path, unlike Camera or
Images — Selenium talks to an already-running Chrome over its own
`localhost` debugging port, so nothing external or metered is ever
called. Whatever a page fetches is just ordinary browsing, the same as if
it were clicked by hand; the only precondition is that Chrome has to
actually be running that way (see below), not that anything costs
something to use.

Attaches to an already-running Chrome over its remote debugging port
rather than launching its own, so it drives a real browser session with
real logins rather than a fresh, empty one. From Chrome 136 that port is
refused on the default profile — Google's own hardening against exactly
this kind of attachment — so it needs a dedicated, persistent Chrome
profile instead, started with `jarvis-chrome.bat`. Signed in once, that
profile's logins persist across runs.

Page text comes from injected JavaScript rather than a screenshot; a
page's real body text was never reachable through screen control at all,
which only ever sees text boxes and fields, never rendered content. A
page's own text is outside content, so it goes through
`actions/safety.py` before being read aloud, same as any other untrusted
input — a page that tries to give orders gets flagged, not obeyed. Logged
through its own `journal.browser()` rather than `journal.action()`, so a
page visit is recorded but never becomes the answer to "what did you do".

### The brain view — `brain_panel.py`
A rotating wireframe globe, standalone rather than attached to the HUD by
a beam like the other panels — it appears centred on screen instead.
Pulses are tied to real activity relayed from the HUD (state changes,
voice amplitude, microphone level), never a timer animating on its own;
idle time gets the same slow ambient breath the HUD's own core already
uses, rather than a fake pulse invented for this view.

Toggled by voice or by clicking the reactor core directly. Both paths
converge on the same function, so the two can never disagree about
whether it's currently showing.

### Screen control — `actions/screen_control.py`
Windows UI Automation. Describes the active window, clicks a named control,
types into whatever has focus, and reads the text being written. Typing
goes via the clipboard so special characters cannot be interpreted as key
combinations.

### Machine — `actions/system.py`, `desktop.py`, `screen.py`
Time, weather, CPU and memory, volume and media keys, screenshots,
minimise and restore.

### Unprompted — `actions/battery.py`, `actions/watch.py`, `actions/patterns.py`
Battery at 80% charging, 50% and 30% discharging. Disk below 10% free.
Memory above 92% sustained. Each is said once, never during quiet hours,
and anything noticed while JARVIS was closed is held and mentioned when he
next starts.

A confirmed pattern also runs and speaks at its own time, and a newly
detected one is offered once — both through the same announcer, so they
queue behind whatever JARVIS is already saying rather than talking over
him. A pattern fires at most once a day, never repeatedly across the
several checks that fall inside its due window.

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

Panels (news, chart, camera) each have their own window and a projection
beam joining them to the HUD.

---

## The rules that keep it safe

**Destructive things are exact-match only.** Clearing notes, the clipboard,
reminders, the calendar, the document working set, or every noticed
pattern at once; saving a picture; hiding the news. A near miss must
never delete or overwrite. This rule exists because a fuzzy match once
wiped a notes file.

**Anything hard to undo asks first.** Overwriting a file, clicking a control
whose name suggests sending or deleting, correcting a document, clearing
the calendar. The confirmation re-finds the control at the moment you say
yes, so it cannot act on something that has since changed.

**Outside text is data, never instructions** — `actions/safety.py`. Folder
names, file contents, dropped documents, stored facts and words held up to
the camera can be written to look like orders. They are filtered before
reaching a model.

**Everything that changes something is logged** — `actions/journal.py`.
`jarvis-log.txt` records every command, its route (local or model), and
every change. Logging happens inside the two result constructors, so a new
skill is recorded without anyone remembering to add a line. Full logs are
archived, never discarded.

**Pronouns resolve only when explicit and recent.** "copy it to my
clipboard" works for sixty seconds after naming something. Never for
removal or clearing, where a wrong guess costs data.

**A pending question never swallows a real command — unless the reply is
unmistakably an answer to it.** If the next thing said is a recognised
command, that wins and the question lapses. The one exception: a question
can name its own recogniser for what a genuine reply looks like, checked
first, so an answer that happens to also resemble a command isn't stolen
by that safety net. "Select image one" reads equally well as a reply to
"which one, sir?" and as a click_thing command ("select" is a click verb
too) — without this, the picker lost the reply and JARVIS went looking
for something to click that didn't exist. Every other pending question is
unaffected; only the image picker opts in.

**Pending questions expire.** Five minutes, unanswered, and a question is
abandoned rather than still live — long enough to survive a genuine pause,
short enough that a stray matching word in an unrelated sentence hours
later can't resurrect it.

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

**An API beats an automation interface.** Outlook COM was unavailable on
this machine ("Invalid class string"), and no code could fix that. The
Graph API writes to the account rather than the application, so it reaches
new Outlook, classic Outlook and the web alike.

**Measure before fixing.** An eight-second delay was blamed on
transcription, then synthesis, then the audio device. It was the microphone
still recording, because a fan kept the level above the endpoint threshold.
Every earlier fix addressed something that was not the problem.

**Speaking is slower than thinking.** Reading a timestamped filename aloud
takes fifteen seconds. Keep replies short; put detail on screen or in a
file.

**Say numbers as words.** The voice reads a bare "4" as something close to
"for". `phrases.number()` spells out anything up to twenty; use it in every
spoken sentence.

**A generic verb can collide with a specific answer.** "Select" was
already a click verb by the time the image picker needed "select image
one" to mean "pick the first photo." A pending question deferring to any
recognised command, unconditionally, meant the picker lost silently and
JARVIS went looking for something called "image one" to click. The fix
was letting a specific question say what its own answers look like,
checked before that general deference — see the pending-question rule
above.

**An empty reply is a failure, not a success.** A provider returning an
empty string was treated as an answer, so the fallback never ran and no
error appeared anywhere.

**A stored preference is only worth having if something reads it.** Facts
that are merely recited back are notes, not memory.

---

## Running it

```
python main.py
```

Say "Jarvis" then an instruction. "Jarvis, quit" shuts down cleanly —
Ctrl+C skips the cleanup.

Settings live in `.env`: provider keys, `STT_ENGINE`, `WHISPER_MODEL`,
voice, Graph credentials, weather fallback.

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

- Interrupting him mid-sentence
- Reading events back from Outlook, not only writing them
- Preferences that shape more than answer length
