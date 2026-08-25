# What you can say to JARVIS

Every command starts with **"Jarvis"** unless a follow-up window is open.

Anything marked **free** costs nothing — no API call. That is nearly
everything. Only three things cost: a general question, asking the camera
what it sees, and a command phrased in a way not listed here (which falls
through to the language model).

Where a phrase is shown, close variations usually work too — "what's the
time" and "what time is it" both land in the same place.

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
| save the picture | keeps it |
| close the camera | releases it |

## Screen control

| Say | Does |
|---|---|
| what's on my screen | describes the window |
| click send | clicks it — asks first if risky |
| type hello world | types into whatever has focus |

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
clipboard, the calendar or reminders will not respond to a near miss. That
is deliberate.
