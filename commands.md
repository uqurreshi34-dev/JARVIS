# What you can say to JARVIS

Every command starts with **"Jarvis"** unless a follow-up window is open.
That rule is for the desk. On your phone, see below — the microphone
button is itself the invocation, so no wake word is needed there.

Anything marked **free** costs nothing — no API call. That is nearly
everything. General-knowledge questions, asking the camera what it sees,
and drafting a commit message need a live model request. A command phrased
in a way not listed here may also fall through to the language model.

Personal-memory questions are different: JARVIS retrieves remembered facts
locally using semantic memory, so a strong match skips the command-classifier
API call. When a language model is available, the final answer uses one model
request. If both language-model providers are unavailable, JARVIS can still
answer from the retrieved memory locally.

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
| tap 🔔 | once, to allow notifications |
| tap 📍 | sends where you are |

**No wake word on the phone.** Holding the microphone button already
says you're talking to him, which is the only job "Jarvis" ever did.

**A reply to something you asked from the phone comes back to the phone,
not the room.** Your desk stays silent for that one — it would only be
talking to an empty chair. The HUD there still shows what happened.

This does not apply to things he says on his own. He has no idea which
room you're in, so those go to both, a second or so apart.

**While you hold the microphone, the desk stops listening.** Speech meant
for your phone can't set off the PC, even in the same room. It starts
listening again once the reply has finished playing.

**Anything he says on his own reaches you.** Battery warnings, market
alerts, the morning diary — they wait on the phone until you look, with
the time he actually said them. He doesn't need the page open at the
time; open it later and they're still there.

**Tap 🔔 once and he'll notify you properly.** After that his
announcements arrive as Android notifications the moment they happen,
even with the page closed. The waiting list above still works
underneath, so a notification you miss isn't news you lose. He stays
quiet overnight, same as he does at the desk.

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
| set up a Python and React project | checks prerequisites, asks for a project name, then asks whether to use the recommended defaults |
| yes | uses the recipe defaults and creates the project |
| no | asks only the choices defined by the selected recipes |

### Project setup

New projects are created under `C:\Users\<you>\Projects\<project name>\`.

JARVIS checks the prerequisites declared by each selected recipe before doing anything. 
He asks for the project name, then asks whether to use the recommended defaults.

For Python, the default creates a `.venv` and upgrades pip. It does not choose an 
application framework or create application files.

For React, the default creates a JavaScript Vite project. Declining the defaults allows 
the recipe's own questions to choose TypeScript or other supported options.

Nothing is created until the setup has been approved.

## Compound tasks

JARVIS can handle several instructions in one utterance.

When every step is a known local action, the whole compound request stays
local and does not require a language-model call.

| Say | Does |
|---|---|
| open chrome and set the volume to 100 | opens Chrome, then sets volume to 100 |
| open chrome and open blender | opens both in order |
| mute and set volume to 50 | performs both local actions |

Semantic compounds can still use JARVIS's planner when a task needs reasoning
between steps.

| Say | Does |
|---|---|
| model Iron Man in Blender and make the legs blue | models first, then modifies the resulting scene |

## Notes

Notes are a timestamped list, separate from files.

| Say | Does |
|---|---|
| add milk to my notes | appends |
| read my notes | reads them back |
| what did I note about the boiler | finds notes on a topic, by meaning |
| do I have a note about my car | same — also "search my notes for…", "any notes on…" |
| remove milk from my notes | takes one out |
| clear my notes | removes all — asks first |

Topic searches are free and local. "Heating" finds the note about the
boiler; very broad words ("travel", "money") may miss, and now and then a
loosely related note is read out instead.

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

## Folders

| Say | Does |
|---|---|
| open the technology folder | opens it in Explorer |
| open jarvis folder | opens the JARVIS folder itself |
| close the technology folder | closes that Explorer window |
| list my folders | names the most recent four |
| how many folders do I have | count only |

Names are matched against what is really there, so "the technology folder"
finds `Technologies`. A folder is never created to satisfy a request — if it
doesn't exist, JARVIS says so and tells you what is there. If a folder exists
but isn't open, "close it" says so rather than failing silently.

Both listings count only your folders and files; JARVIS's own dot-prefixed
state is excluded from each.

## Persistent JARVIS-folder housekeeping

| Say | Does |
|---|---|
| keep my JARVIS folder organised | enables automatic local housekeeping |
| keep my JARVIS folder organized | same |
| stop keeping my JARVIS folder organised | disables automatic housekeeping |
| stop keeping my JARVIS folder organized | same |

Once enabled, JARVIS checks the JARVIS folder at startup and then every six hours.

If he finds clearly classifiable loose files, he organises them automatically using local rules. 
Existing destination folders are reused; missing category folders are created when needed; 
ambiguous files are left alone.

There is no language-model call for the automatic housekeeping check.

When automatic organisation changes files, JARVIS reports what he moved and returns to listening 
mode, so `undo` can immediately reverse the latest organisation transaction.

The setting survives restarts in `.jarvis-folder-guard.json`.

The manual `organise my JARVIS folder` command remains available when you want the broader planner 
to inspect the folder and propose an organisation first.

Say `stop keeping my JARVIS folder organised` or `stop keeping my JARVIS folder organized` to turn 
the automatic behaviour off.

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

## Quran

| Say | Does |
|---|---|
| recite surah 36 | the whole chapter, verse by verse |
| recite quran chapter 2 verse 255 | that one verse |
| play surah 112 | the whole chapter |
| read chapter 18 ayah 10 | that one verse |
| close the quran | stops the recitation and closes the page |
| stop reciting | the same |
| close the recitation | the same |

Quran, Qur'an, quraan and koran are all recognised, and so are surah, sura
and chapter. The chapter has to be a number — names are too easily
misheard to act on.

A verse the chapter does not have is refused with the real count, so
`recite surah 112 verse 40` gets told Al-Ikhlas has 4 verses.

The rest is on the projected page rather than spoken:

| Control | Does |
|---|---|
| reciter dropdown | changes voice from the next verse on |
| verse box | jumps to that verse |
| auto | carries on to the next verse when this one ends |
| pause | cuts the sound immediately, holds the place |
| stop | ends the recitation and closes the page |

Ticking auto after a single verse carries on from the next verse rather
than repeating the one just heard. Finishing a verse leaves the page up;
only stop takes it away. The HUD stop button ends the recitation too.
Stop and close work whether the recitation is still playing or has
already finished.

Anything he would have announced while reciting — a market alert, a
reminder — waits and is delivered when the session ends.

## The sky

Live aircraft within twenty-five miles, spoken and then drawn on a radar
face beside the HUD.

| Say | Does |
|---|---|
| what's flying overhead | counts them, names the nearest and the highest |
| show me the radar | the same, with the radar face |
| aircraft above me | the same |
| any jets nearby | the same |
| close the radar | puts it away and stops the feed |

He needs something that flies and somewhere to look, in either order, so
"book me a flight to Dubai" and "I'm flying to Spain on Tuesday" both
fall through to the usual routing.

Reading the face: you are the cross at the centre, north is at the top,
and the rings are quarters of the range. Where a mark sits is where the
aircraft is on the ground below — its height is the number beneath the
callsign, not its distance from the middle. So an aircraft crossing
directly over you slides through the centre without its altitude
changing at all.

| On the face | Means |
|---|---|
| amber mark | low — circuits, approach, helicopters |
| pale mark | climbing or descending through the middle altitudes |
| cyan mark | at cruise, thirty thousand feet and above |
| which way the delta points | the direction it is actually flying |
| ↑ or ↓ by the altitude | climbing or descending faster than 100 feet a minute |
| hovering a mark | names the ground underneath it |

Close it when you are not watching. The feeds are free and shared, and
the radar keeps asking them for as long as it is open.

## Protocols

A run of things he already does, in one sentence. **Free**: no model call.
Yours to change, in `protocols.json` in your JARVIS folder.

| Say | Does |
|---|---|
| initiate startup protocol | Sky Sports, BBC News and GitHub in the JARVIS Chrome, and your JARVIS project |
| initiate stream protocol | starts OBS, unmutes the mic, starts the replay buffer and recording |
| clean slate protocol | undoes whatever is engaged: closes only the tabs he opened, stops recording and the replay buffer, closes OBS, then asks "Shall I close the JARVIS project?" (yes closes it; no, or no answer, leaves it open) |
| clear the stream protocol | undoes just that one |

"Start up protocol" and "clean slate" alone work too. A step that fails is
named in his reply; the rest still happen.

## The sensors

Boards on the wifi (see JARVIS.md) report to a panel on top of the HUD,
and can be asked aloud. **Free**: no model call.

| Say | Does |
|---|---|
| what's the temperature in the room | that board's last temperature |
| how humid is it in the kitchen | its humidity |
| is anyone in the room | whether it has seen movement lately |
| is it cold in here | every room's temperature |
| how are the sensors | everything, a sentence per room |

A room is whatever the board is called (SENSOR_NAME), so a new board can
be asked about as soon as it reports. "What's the temperature outside"
names no board, so it is still the weather.

## Is he there

| Say | Does |
|---|---|
| you up | I'm here, sir. |
| you awake | Always here, sir. |
| you there | For you, sir. Always. |
| you still there | Never far, sir. |
| you with me | Right here, sir. |

Seven replies, never the same one twice running. Costs nothing and
answers instantly — being asked whether you are there is not a question
worth a model request.

## Thanks, praise and how he is

| Say | Does |
|---|---|
| thank you jarvis | You're welcome, sir. |
| thanks for that | My pleasure, sir. |
| cheers mate | Any time, sir. |
| well done | I do my best, sir. |
| good job jarvis | Kind of you to say, sir. |
| jarvis, you good? | Never better, sir. |
| how are you | Very well, sir. Thank you for asking. |

Improvise freely — "excellent, thank you so much", "brilliant, cheers",
"are you okay jarvis" all work. Free and instant. Anything more than the
pleasantry itself ("thanks, now open chrome") is treated as a command.
If a language model is resting after a rate limit, "you good?" says so.

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

From the phone, tap the camera button to open the rear camera. Tap the
shutter to capture a picture and have JARVIS describe it, or tap the camera
microphone to keep the current frame and ask a spoken question about that
exact picture. The question is transcribed without being run as a desktop
command, so "what am I holding?" is asked of the photograph itself.

When JARVIS is asked to keep a phone picture, he confirms first. Saying
"yes" saves it into `JARVIS\images\` with a short descriptive filename and
a timestamp; saying no leaves it unsaved. The saved filename is based on
what the vision model described, but no extra vision request is made just
to name the file. Camera replies on the phone keep the description and
"Shall I keep it?" as separate spoken utterances so there is a deliberate
breath between them.

## Screen control

Works on whatever window is focused — any application, including a
browser. "What's on my screen" is not a required first step; each command
below looks at the live window itself, every time.

Screen control is local-first. JARVIS tries Windows UI Automation first,
because it is free and precise. If UI Automation cannot find a visible target,
he captures the active window and uses the vision model once to locate it.
The visual result is cached briefly so confirmation does not cause a second
vision request. This same fallback works when the command comes from the
phone, so the phone can remotely control the PC without using its camera.

| Say | Does |
|---|---|
| what's on my screen | describes the window |
| click send | clicks it — asks first if risky |
| type hello world | types into whatever has focus |

Examples of visual fallback commands include **"click File"**, **"click
Edit"**, or **"click the green button"** when the requested target is visible
but not exposed through Windows UI Automation. Typing still uses the currently
focused control and does not require a vision request.

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

## Blender and 3D modelling

JARVIS can model, inspect and modify Blender scenes using natural language.

| Say | Does |
|---|---|
| model this in Blender | turns the current reference into a Blender model |
| inspect the Blender scene | reads the current scene and its objects |
| make the helmet blue | modifies the current Blender model |
| isolate the chest | isolates the requested part |
| restore the model | restores Blender visibility state where appropriate |
| restore original | returns to the preserved original Blender snapshot |

### Tripo

JARVIS can also reconstruct a subject through Tripo and hand the resulting
segmented model to Blender.

| Say | Does |
|---|---|
| model Iron Man in Tripo | reconstructs the subject and opens it in Blender |
| model this using Tripo | same, using the current modelling request |

The provider name does not need to be perfectly transcribed. JARVIS uses the
meaning of the modelling request to correct obvious speech-recognition
distortions rather than depending on a fixed list of misspellings.

After Tripo reconstruction, the model can be modified through the same Blender
natural-language commands above.

## Research and reports

JARVIS can research arbitrary subjects, compare them and create source-backed
Word reports.

You do not always need to say "write a report". A research/comparison request
combined with a save request is enough.

| Say | Does |
|---|---|
| compare iPhone with Android and save it | researches both, compares them and creates the report |
| research NVIDIA, compare it with AMD and save it | same |
| research Lamborghini, compare it with Ferrari, write the report and save it | same |

For much better sources, add a free Tavily key to `.env` in the JARVIS
repo folder: `TAVILY_API_KEY=tvly-...` (tavily.com, 1,000 searches a month,
no card). Without it, Bing is used. Either way, pages that are not about
what was searched are left out, and if nothing on-topic is found he says
so instead of writing an empty report.

Reports cite a source number for each claim, say when something rests on a
single website, and mark any figure that its cited source does not contain.

Research uses the normal JARVIS provider chain: Claude first, then Groq, then
Gemini if failover is needed.

JARVIS plans searches dynamically, gathers multiple sources and synthesises
the evidence into the final report. Individual pages can fail without
aborting the whole research task; unusable sources are skipped and research
continues with the others.

Reports include a Sources section listing the sources actually used.

### Research destinations

By default reports are saved directly in the JARVIS folder.

You can name a destination folder explicitly:

| Say | Does |
|---|---|
| save it in the Cars folder | creates/uses `JARVIS\Cars\` |
| save it in the AI folder | creates/uses `JARVIS\AI\` |
| save it | uses the normal JARVIS folder |

A missing folder is created automatically. Existing reports are not
overwritten; a new unique filename is used beside previous reports.

Destination folders are direct children of the JARVIS folder. JARVIS cannot
use the feature to escape the JARVIS root.


### Asking what your reports say

| Say | Does |
|---|---|
| what did my report say about deep sleep | reads the passage, and says which report |
| what does my ESP32 report say about batteries | searches that report only |
| search my reports for energy use | same as asking what they say |
| which reports mention insurance | names each report, with its date |
| what does my report say about Aston Villa vs Birmingham City | naming a report reads its summary |

Free and local: the passage is found by meaning and read as written, with
no model call. Reports in the JARVIS folder and any folder inside it are
searched, including Reports. Asking about a report never starts a new
research run; "research X and write a report" still does.

## Connected services

Services that speak MCP (GitHub, Notion, a database, Home Assistant and many
more) can be connected by listing them in `mcp.json` in the JARVIS folder,
in the same `mcpServers` format Claude Desktop and Cursor use. Keys go in
`.env` and are referred to as `${NAME}`.

| Say | Does |
|---|---|
| name the service in the request, e.g. what's in my github issues | JARVIS uses that service's tools to answer |
| investigate … / look into … | Agent Mode, which can also use connected services |
| name the service and ask for a change, e.g. open a github issue on JARVIS titled HUD flicker | reads back exactly what it would do and asks; yes runs it |
| start recording / stop recording | OBS, instantly: **free**, no model call |
| start the replay buffer, then clip that | OBS keeps the last 30 seconds, then saves them, says which file, and CLIP SAVED flashes: **free** |
| pause the recording | OBS, at once, no question |
| switch to the (scene name) scene / mute the mic | OBS |

Uses a model request. Asking only reads: JARVIS uses tools a service marks
as read-only. A change happens only for a tool you list in that service's
`allowed_actions` in `mcp.json`, never one the service marks destructive,
and only after you say yes to the read-back within two minutes. The HUD's
outer ring counts those two minutes down, and the strip under the HUD's
telemetry shows each service lighting up as it is used.

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
| what do you know about my car | only what's remembered about that, or says there's nothing |
| forget about London | removes it |
| what are my gym days | retrieves the remembered gym schedule semantically |
| when do I train | finds the same memory even though the wording differs |
| what's my project | retrieves the remembered project locally |
| do I have any projects | yes/no personal-memory question can be answered locally

"Remember that…" is optional. He confirms what he understood — if he says
the vague *"I'll remember that"* rather than *"Your location is Madrid"*,
that phrasing didn't land.

Memory retrieval is local and semantic. JARVIS compares the meaning of a
question with the facts in `memory.txt`, rather than requiring the question
to repeat the exact words used when the fact was stored. This means
paraphrases such as "when do I train?" and "what's my workout schedule?" can
retrieve the same remembered fact without a hard-coded list of synonyms.

A strong personal-memory question bypasses the normal command-classifier
model call. The answer normally uses one language-model request for natural
wording, but if both providers are unavailable the best local memory match
can still be spoken directly.

The semantic retrieval system contains no personal facts itself. Facts remain
in `memory.txt`; the local model is only used to find the relevant fact.

### Learning factual subjects

This uses a live language-model request once to learn useful factual reference data, 
then stores the results locally for later questions and comparisons.

| Say                        | Does                                                  |
| -------------------------- | ----------------------------------------------------- |
| learn about Mercedes       | learns up to six useful facts and stores them locally |
| research the BMW 3 Series  | same                                                  |
| look up facts about Toyota | same                                                  |

These are model-assisted learning commands, not part of the free path at learning time.

Learned facts go to `subjects.txt`, not `memory.txt`, so researching a
hundred subjects can never push out what you told him about yourself.
Questions about them are answered locally with no API call:

| Say | Does |
|---|---|
| audi a4 boot space | reads the one stored fact that answers it |
| compare the A4 with the A6 | side by side, when both are stored |
| is the BMW better than the Mercedes | same |

A question can name a stored subject without any one fact being a
confident enough answer. "bmw 3 series history" is one: six facts are
stored, and none of them matches the word *history* closely enough for
JARVIS to recite it as the answer, even though one mentions the 1975
launch. That goes to the model — and the model is given **those six facts**
as its context, so it answers about the car.

Only when the subject itself isn't in `subjects.txt` does the model fall
back to your personal memory instead. That distinction is what stopped
"bmw 3 series history" coming back as a summary of your own cars.


### Collections

JARVIS can learn that a memory is a collection rather than a single value. Once that 
collection is learned, its current items can be added, removed, replaced and queried 
locally without another language-model request.

| Say                                                        | Does                                                                  |
| ---------------------------------------------------------- | --------------------------------------------------------------------- |
| what are my cars                                           | lists the current members of the learned cars collection              |
| what am I reading                                          | queries the current members of a learned collection such as books     |
| remove ferraris from my cars                               | removes that item locally                                             |
| which of my cars is best suited to a long motorway journey | compares the cars using only locally learned factual evidence         |
| which car would be best for a long distance trip           | the natural singular form can target the same learned cars collection |

For comparisons, JARVIS ranks only collection members for which it has learned factual
data. Members with no factual data are not guessed about, and a near-tie is reported as
insufficient evidence rather than turned into a made-up winner.

The collection name and its current items are learned from language rather than hard-coded 
lists, so the same behaviour works for cars, books, foods, projects, or another collection 
JARVIS has learned.


## Calling, WhatsApp and texting

Only from the phone — your PC has no SIM. Ask at the desk and he says so
rather than pretending.

Put people in `contacts.txt` in your JARVIS folder, one per line:

    mum: +447700900123
    dad: +447700900456
    work: +441214960000

Numbers must start with `+` and the country code. **Add `contacts.txt` to
your `.gitignore`.**

| Say | Does |
|---|---|
| call mum | opens the dialler with her number ready |
| ring dad / phone work | same |
| whatsapp dad | opens his WhatsApp conversation |
| text mum saying running late | opens a text with the message written |
| who are my contacts | lists who he can reach |

He opens it; you tap to send or call. Android won't let a web page dial
or send on its own, which is also your last look at what's about to go
out in your name.

**He matches names by how they sound**, so "dan", "done" and "daddy" all
find Dad without any of them being written down. When he's not sure he
asks — *"did you mean dad?"* — and a plain **yes** is enough. Say a
different name instead and he'll use that. Nothing is ever dialled from
a name that isn't in your file.

## Where you are

Only the phone can answer this — your PC has no idea where you are. Tap
📍 to send your position, then ask.

| Say | Does |
|---|---|
| this is home | remembers where you're standing as home |
| where am I | tells you, and how far from home |
| how far am I from home | the distance |
| am I home | yes or how far off you are |

Set home once, standing in it. He needs a fresh 📍 tap before each
question — a phone stops reporting position the moment you switch away
from the page, so he tells you *when* the reading was taken rather than
pretending to know where you are this second.

Anywhere within about 120 metres counts as home. Phone GPS isn't precise
enough to do better, and a tighter radius would say you were out while
you sat in your own front room.

Note he won't take "I'm home" as setting home — that far more often
means you've just arrived, and getting it wrong would overwrite the one
thing everything else here depends on.

## Patterns

Habits JARVIS notices from commands you ask him to carry out, kept in `patterns.txt`,
separate from `memory.txt`. Memory is what you told him; a pattern is something he works out 
from the commands he has logged you asking him to perform; he does not monitor applications 
you open or actions you take manually. Free — built from the log he already
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
| show me your mind | the turning globe, pulsing with activity, with a voice halo while he speaks |
| hide your mind | closes it |

Clicking the reactor core in the HUD does the same.

## The log

| Say | Does |
|---|---|
| what did you do | the last thing he changed |
| what have you done today | counts for the day |
| when did I last ask about bitcoin | the latest time, and what you said |
| have I asked you about the weather before | yes or no, how often, the latest |
| how many times did I ask about github this week | a count |
| what did I ask you about the markets | the latest few, as you said them |

Everything that changes anything is written to `jarvis-log.txt`.
Commands sent from your phone go through the same path, so they're
logged there too.

Questions about your own history are free and local, and found by
meaning: "crypto" finds "what's the bitcoin price". Add "today",
"yesterday", "this week", "last week", "this month" or "last month" to
narrow it. The first search after a long break may take a moment while
new commands are encoded; after that it is instant.

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

**The phone won't let you grant microphone access, or notifications.** If Chrome says "this
site can't ask for your permission, close any bubbles or overlays from
other apps", something on your phone is drawing over the screen and
Android is blocking the prompt. It names no app, and it is often not one
you would suspect — a screen recorder, a blue light filter, chat bubbles,
a caller ID app. Look in Settings, Apps, Special access, Appear on top,
and turn off whatever is listed. Alternatively, tap Block deliberately,
then open the padlock menu in the address bar and set Microphone to
Allow — that route goes through settings rather than the prompt, so the
overlay doesn't block it.

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
