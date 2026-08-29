# What you can say to JARVIS

Every command starts with **"Jarvis"** unless a follow-up window is open.
That rule is for the desk. On your phone, see below — the microphone
button is itself the invocation, so no wake word is needed there.

Anything marked **free** costs nothing — no API call. That is nearly
everything. Four things cost: a general question, asking the camera what
it sees, drafting a commit message, and a command phrased in a way not
listed here (which falls through to the language model).

Where a phrase is shown, close variations usually work too — "what's the
time" and "what time is it" both land in the same place.

---

## From your phone

JARVIS prints a URL when he starts. Open it on a phone on the same
network and bookmark it. Everything below works from there too — it is
the same JARVIS, not a smaller version of him.

| Do | Does |
|---|---|
| type a command, press Send | exactly as if you'd said it at the desk |
| hold 🎙, speak, release | same, spoken |
| tap 🔊 | turns the spoken reply on or off |

**No wake word on the phone.** Holding the microphone button already
says you're talking to him, which is the only job "Jarvis" ever did.

**The reply comes back to the phone, not the room.** Your desk stays
silent — it would only be talking to an empty chair. The HUD there still
shows what happened.

**While you hold the microphone, the desk stops listening.** Speech meant
for your phone can't set off the PC, even in the same room. It starts
listening again once the reply has finished playing.

**Anything he says on his own reaches you.** Battery warnings, market
alerts, the morning diary — they wait on the phone until you look, with
the time he actually said them. He doesn't need the page open at the
time; open it later and they're still there.

**A stray tap does nothing.** Too short to be a command, so it's ignored
rather than guessed at.

### Setting it up

The page is served over HTTPS, because mobile browsers refuse microphone
access otherwise. JARVIS generates the certificates himself on first
start. Install `jarvis-phone-ca.cer` on the phone as a CA certificate so
the browser trusts him. `jarvis-phone-key.pem` is the private key and
stays on the PC.

---

## Time, weather and the machine

| Say | Does |
|---|---|
| what time is it | date and time |
| what's the weather like | forecast for wherever you've said you are |
| how's my system | CPU, memory, disk, battery |
| how much battery do I have | battery only |
| take a screenshot | saves to your JARVIS folder |
| minimise everything | clears the desktop |
| bring my windows back | restores them |

## Volume and media

| Say | Does |
|---|---|
| mute / unmute | system volume |
| turn it up / turn it down | in steps |
| set volume to 40 | absolute |
| how loud is it | current level |
| pause / play | media keys |
| next track / previous track | skips |

## Applications and projects

| Say | Does |
|---|---|
| open chrome | any installed application |
| close outlook | closes it |
| I'm done with chrome | also closes it |
| open youtube | a website, when no app matches |
| open my project | your default project |
| my default project is JARVIS | sets it |

## Notes

Notes are a timestamped list, separate from files.

| Say | Does |
|---|---|
| add milk to my notes | appends |
| read my notes | reads them back |
| remove milk from my notes | takes one out |
| clear my notes | removes all — asks first |

## Files

Everything lives in `C:\Users\<you>\JARVIS\`. Nothing outside it is touched.

| Say | Does |
|---|---|
| create a text file called shopping list | also word, pdf, markdown, csv |
| add milk to my shopping list file | appends a line |
| read my shopping list | reads it aloud |
| what's in my business file | same, for any file |
| copy my shopping list | duplicates it |
| copy my shopping list to my clipboard | contents to clipboard |
| remove chocolate from my shopping list | takes one line out |
| list my files | names the most recent four |
| how many files do I have | count only |

Saying **"file"** always works. Leaving it off works when the file already
exists.

## Documents

Drag Word documents, PDFs, TXT, Markdown, CSV or JSON files directly onto the JARVIS HUD to load them into a temporary working set.

| Say / Do | Does |
|---|---|
| drag a document onto the HUD | loads it into the working set |
| drag several documents onto the HUD | loads all that fit |
| which document has the shorter termination period | compares the relevant documents |
| which contract has the higher fees | compares the relevant documents |
| which document contains confidentiality | finds the relevant document or documents |
| what does the Smith Consulting contract say about termination | answers from the loaded documents |
| what's in this document / what does this file say | answers from whichever documents are loaded, unnamed |
| what documents do I have | lists what's currently loaded, without clearing it |
| clear my documents | clears the working set |
| clear the documents | clears the working set |

Every document is capped to a fixed size individually before anything else happens, regardless of how many others are already loaded — a large file dropped second or third is truncated the same way a lone large file is, rather than refused. The working set as a whole holds up to eight documents and 60,000 extracted characters; a document that doesn't fit alongside what's already loaded is refused on its own, without disturbing the rest — it would always have fit by itself. Documents are extracted and classified locally; loading them does not make an API call.

The HUD shows the current working-set count when one or more documents are loaded, for example "3 / 8". The indicator disappears when there are no documents loaded.

JARVIS does not speak when documents are successfully dropped onto the HUD. He speaks when documents are cleared and when answering questions about them.

When comparing documents, JARVIS names the actual document rather than referring to them only as "document one" or "document two".

## Clipboard

| Say | Does |
|---|---|
| copy hello world to my clipboard | plain text |
| what's on my clipboard | reads it |
| clear my clipboard | empties it — exact phrase only |

## Spelling

Local dictionary, no API. Works on files and on whatever is on screen.

| Say | Does |
|---|---|
| proofread my letter | checks a file |
| proofread my screen | checks what you're writing |
| list them | reads the first four |
| fix them | corrects the file — asks first |
| copy the corrected text | screen text to clipboard |
| write a report | a report file with line numbers |
| add qurreshi to the ignore list | never flag that word again |

Word documents keep their formatting when corrected. Screen text goes to
the clipboard rather than being typed over your work.

## Images

Fetched from Unsplash — costs a request to find a photo, the same way the
camera costs a call to see one. Everything after that (rotating, resizing,
saving) is free: pure local pixel work, no network involved.

| Say | Does |
|---|---|
| show me an image of the Eiffel Tower | finds three candidates, shows them side by side |
| one / the second one / three | picks one — say or click a card |
| none of these | closes the picker without choosing |
| rotate the image 90 degrees | turns it — also "left", "counterclockwise", 180, 270 |
| make the image bigger / smaller | zooms in or out, visibly, in the panel |
| restore the image | back to exactly how it was fetched — undoes both the zoom and any rotation |
| save the image / save the picture | either phrase saves whichever is actually on screen — the fetched photo if one's showing, otherwise the camera's |
| close the image | dismisses the panel |

Saved images go into `JARVIS\images\`, created the first time it's needed.
Whatever rotation and zoom are in effect when you save is what ends up in
the file — the preview and the save always match.

If nothing is chosen or the reply isn't clear, JARVIS asks again rather
than silently giving up; the question stays open for a few minutes, so
going quiet and coming back later (even needing the wake word again)
still works.

## Charts

| Say | Does |
|---|---|
| plot my sales csv | asks which columns |
| names on x axis and salary on y axis | draws it |
| save the chart | keeps it, any time later |
| close the chart | dismisses the panel |

## News

| Say | Does |
|---|---|
| show me the news | ten UK headlines, in a panel |
| show me america news | also world, europe, tech, business, sport, science, politics, health |
| expand story 3 | reads it out |
| show me the picture for story 2 | shows the photograph |
| close the news | dismisses it |

## Calendar

JARVIS keeps his own calendar, and also writes to Outlook and an `.ics`.

| Say | Does |
|---|---|
| add business meeting to my calendar | asks the date |
| March 2nd 2027 | files it |
| add dentist to my calendar for tomorrow at 9am | in one go |
| what's on my calendar | the next four |
| remove business meeting from my calendar | asks which date |
| clear my calendar | removes all — asks first |

Dates understood: "tomorrow", "next Friday", "the twenty fifth of December",
"March the second twenty twenty seven", with times.

## Markets

| Say | Does |
|---|---|
| how are the markets | Bitcoin, Ethereum, XRP and currencies |
| tell me when bitcoin moves 2 percent | sets an alert |
| what are you watching | current alert settings |
| write me a report on bitcoin | Word report, 24 hours / week / month |

Prices are always exact, never rounded. Alerts fire on a move in either
direction, once per move.

## Camera

Costs an API call, since it needs a vision model.

| Say | Does |
|---|---|
| what am I holding | looks and tells you |
| how about now | looks again |
| what do you see | same |
| save the picture | keeps it — or saves a fetched image instead, if one's on screen; see Images below |
| close the camera | releases it |

## Screen control

Works on whatever window is focused — any application, including a
browser. "What's on my screen" is not a required first step; each command
below looks at the live window itself, every time.

| Say | Does |
|---|---|
| what's on my screen | describes the window |
| click send | clicks it — asks first if risky |
| type hello world | types into whatever has focus |

A freshly loaded Chrome tab can briefly show nothing to click while its
own accessibility tree wakes up; a click that misses for that reason is
retried once automatically before giving up.

## Browsing

Read-only: goes somewhere and reads what's there. Needs the JARVIS Chrome
shortcut running (see JARVIS.md) — a separate, persistent Chrome profile
so it can run alongside your normal browser rather than needing it closed.

Reading a page's actual text — an article, its headings, saving it to a
file — is not something screen control can do at all: it only ever reads
text boxes and fields, never rendered page content. This is what fills
that gap.

| Say | Does |
|---|---|
| browse to the bbc | opens it in the attached Chrome |
| search for the offside rule | web search |
| read this page | title, length, and the opening, spoken |
| what's on this page | its headings and links |
| what page am I on | title and site, no reading |
| save this page | text saved to your JARVIS folder |

Clicking and typing on a web page are still screen control, above, same
as any other application — browsing does not change how those work.

## Memory

Facts are kept in `memory.txt`, editable by hand.

| Say | Does |
|---|---|
| my name is AvidCoder | used in the greeting |
| I am from Madrid | weather follows it |
| I prefer short answers | shortens spoken answers |
| my default project is JARVIS | for "open my project" |
| my calendar is local | stops pushing to Outlook |
| what do you know about me | reads it back |
| forget about London | removes it |

"Remember that…" is optional. He confirms what he understood — if he says
the vague *"I'll remember that"* rather than *"Your location is Madrid"*,
that phrasing didn't land.

## Patterns

Habits JARVIS notices from what you actually do, kept in `patterns.txt`,
separate from `memory.txt`. Memory is what you told him; a pattern is
something he worked out by watching. Free — built from the log he already
keeps, no API call.

| Say | Does |
|---|---|
| what patterns have you noticed | lists them, and whether each is running |
| what have you noticed about me | same |
| yes (after he suggests one) | starts running it automatically |
| no (after he suggests one) | drops it, and won't ask again |
| forget the weather pattern | removes just that one |
| forget the bitcoin markets report pattern | removes just that one |
| forget all patterns | removes every one — exact phrase only, asks first |

He only mentions a habit after seeing it at least five times, across at
least four different days, at roughly the same time. Below that he says
nothing, so a few requests in one busy afternoon won't trigger anything.

Nothing runs automatically until you say yes. A suggestion is made once —
ignore it and he won't keep asking. Once confirmed, it runs at that time
each day and speaks the result, at most once a day.

Where the thing needs a subject, that's part of the habit: a nine o'clock
Bitcoin report and a five o'clock Ethereum report are two separate
patterns, not one. That's why the labels include the coin — so you always
know which one you're removing.

## Tasks

Define your own commands in `tasks.txt` in your JARVIS folder:

    task: tests
    run: pytest -q
    confirm: no

| Say | Does |
|---|---|
| what tasks do I have | lists what you've defined |
| run the tests | runs that task |
| run my build | same, for any name you defined |

He can only run what you wrote down. Nothing you say ever becomes part of
a command — speech only picks a name off your own list. Leave `confirm`
out and he'll ask first; set it to `no` for things like tests that are
safe to just run.

Tasks run in the background and he tells you when they finish, with the
last few lines of output. The full output goes to a file in your JARVIS
folder.

## Git

Set `JARVIS_REPO` in your `.env` to your repository folder — no quotes,
backslashes are fine. Reading is free; drafting a commit message costs a
call, since a model reads the diff.

| Say | Does |
|---|---|
| what have I changed | branch, staged, unstaged and untracked counts |
| what's staged | same |
| propose a commit message | drafts one from the staged diff, reads it, asks |
| commit my changes | same |
| yes | commits it |
| no | nothing happens, staging untouched |

**You stage, JARVIS commits, you push.** He never runs `git add`, `push`,
`reset` or anything else — the only thing he can do is commit exactly what
you already staged, and only after you say yes. `git log` in your own
terminal to see it land.

Signed commits work normally: he runs real git, so your GPG passphrase
prompt appears just as it would if you'd typed the command yourself.
That's also a useful tell — if a prompt appears, a real commit happened.

## The brain view

| Say | Does |
|---|---|
| show me your mind | the sphere, pulsing with activity |
| hide your mind | closes it |

Clicking the reactor core in the HUD does the same.

## The log

| Say | Does |
|---|---|
| what did you do | the last thing he changed |
| what have you done today | counts for the day |

Everything that changes anything is written to `jarvis-log.txt`.
Commands sent from your phone go through the same path, so they're
logged there too.

## Reminders

| Say | Does |
|---|---|
| remind me in ten minutes to call mum | spoken when due |
| what timers are running | lists them |
| cancel my reminders | cancels all — exact phrase only |

## Ending

| Say | Does |
|---|---|
| quit | shuts down cleanly |

Ctrl+C skips the cleanup, so prefer saying it.

---

## Things he says without being asked

- Battery at 80% charging, and at 50% and 30% on battery
- Disk below 10% free
- Memory above 92% for a sustained period
- A coin moving past your threshold
- What's in the diary for today and tomorrow, at startup
- Anything missed while he was closed, when he next starts
- A habit he's noticed, offered once — see Patterns
- Anything you've confirmed as a pattern, at its usual time

All of these reach your phone as well, and wait there until you look.

Nothing non-urgent between 10pm and 8am; it waits.

---

## When something doesn't work

**He says "That's beyond me for now, sir." or "I'm not equipped for that yet, sir."** The phrasing isn't in the
table above and the language model couldn't place it either. Try a phrase
from this file.

**He does the wrong thing.** Check the `You said:` line in the console —
usually the transcription, not the matching.

**A name is misheard.** Add it to `spelling-ignore.txt`; that list is also
fed to the transcriber, so it learns your vocabulary.

**Anything destructive needs the exact phrase.** Clearing notes, the
clipboard, the calendar, reminders or every noticed pattern will not
respond to a near miss. That is deliberate. Removing a single pattern by
name is not in that category — that one's specific enough to be safe.
