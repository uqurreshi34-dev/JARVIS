[33m1de6600[m[33m ([m[1;36mHEAD[m[33m -> [m[1;32mmain[m[33m, [m[1;31morigin/main[m[33m, [m[1;31morigin/HEAD[m[33m)[m feat: add git commit support and recent journal reading
[33me42e55b[m feat: add git commit support and commit message drafting
[33md373079[m docs: Patterns in both .md files; wire pattern intents into llm.py
[33m81ecaf5[m fix: persist pattern subject; audit of pattern-feature wiring
[33m2226424[m feat: give Jarvis ability to detect user patterns
[33m4681e8e[m feat: behavioural pattern detection (actions/patterns.py) + main.py wiring
[33m8c25778[m feat: bulletproof the TTS cache — sidecars, corruption recovery, disk-bounded LRU eviction, and a real prewarm/speak cache lock
[33md8e2323[m feat: bulletproof the TTS cache — sidecars, corruption recovery, disk-bounded LRU eviction, and a real prewarm/speak cache lock
[33mc2df96b[m fix: notification-count titles, tub mishearing, .co. pronunciation
[33m3843ae7[m fix: tab-focus check was self-defeating, visibly stole focus itself
[33m7b00933[m fix: clear my documents wasn't registered; .md docs updated
[33m3a7aa06[m fix: document questions misrouted to unknown / describe_screen
[33mf857501[m fix: Polish multi-document working set Show filenames, add drag/drop glow, and prevent document text becoming commands
[33m5338ff9[m fix: safety.quote() gap, per-document truncation, glow-ring, list_documents
[33ma841e1e[m feat: Add temporary multi-document knowledge working set Support document questions, comparison, synthesis and clearing
[33m065dd6e[m fix: 'close/hide the picture' ignored the image feature entirely
[33mfb9e25f[m fix: bare 'close image' and 'save image' misrouted to camera/shrink
[33ma972b8d[m fix: 'close the image' hid the wrong panel, orphaning the picker
[33m2180e83[m fix: 'the blue one' no longer selects photo #1
[33ma283634[m docs: document the image feature and picker in commands.md and JARVIS.md
[33m4a3a0e3[m feat: three-image picker for show_image, pick by voice or click
[33m8882af9[m Merge branch 'main' of https://github.com/uqurreshi34-dev/JARVIS
[33m17fdb79[m Delete scripts directory
[33mfc952df[m Delete .github/workflows/apply-image-choices.yml
[33m11e0b6f[m Add workflow for image-choice integration
[33m4e07afe[m Add robust image-choice integration patch
[33m91b3955[m Add safe additive image-choice patcher
[33ma29732d[m Add three-image selection panel
[33md8b9356[m Add three-image choice state
[33mbe0a2d8[m revert: remove accidental image chooser workflow
[33meec92a9[m chore: run three-image voice chooser patch
[33m97f5e12[m fix: Image now increases or decreases in size as per user request
[33mdde46cb[m feat: Give Jarvis the ability to show images the user requests and enlarge, shrink or rotate them before saving to the Jarvis/images folder
[33m1d246b7[m fix: Allow Jarvis to pronounce digits correctly - e.g read 44 as fourty four, not fofor
[33mb19f840[m fix: Allow Jarvis to tell the difference between 'scores' and 'scores and fixtures' so he clicks on te correct element
[33m1204c5a[m feat: read-only browser control via attached Chrome; fix screen-control matching and cold-tree misses
[33me062dfb[m chore: change jarvis test dialogue to something more verbose
[33m8ac8053[m chore: add commands.md so that I can remember the growing list of prompts to Jarvis
[33m3d1aa0b[m Wire spoken market summary, add COMMANDS.md reference
[33md565e76[m Speak crypto prices exactly, never rounded
[33m893a510[m feat: Jarvis can now write reports on last 24 hr bitcoin, ethereum and XRP movements, with a brief summary on monthly and weekly history baked into the report. He can also inform user when any of those coins crosses a threshold as defined by the user - whether that's a price increase or decrease.
[33m3aa01e5[m Watch Bitcoin, Ethereum and XRP with adjustable move alerts
[33m7d14e4e[m feat: expand Jarvis' basic commands vocabularly
[33m5e726a9[m feat: expand Jarvis' basic commands vocabularly
[33ma314a5f[m Accept more location phrasings and confirm what was understood
[33m0d8839a[m Standalone sphere brain view, clamped alpha, dark backing
[33mce1a741[m Standalone sphere brain view, clamped alpha, dark backing
[33m9382d39[m feat: Jarvis can filter out names and add them to the ignore list
[33m8135caf[m Delete Outlook events by stored id, keeping calendar sources in sync
[33m4527358[m feat: Jarvis can now autopopulate outlook calendars
[33m81b32e7[m feat: Jarvis can now autopopulate outlook calendars
[33md0386be[m feat: Add calendar reading and adding ability to Jarvis
[33m0a919dc[m Add calendar with spoken dates, ics export and confirmations
[33m0fb4ed2[m Add unprompted observations with quiet hours and held notices
[33mb91858a[m Weather follows remembered location with geocoding and cache
[33md2a25c8[m Add durable memory and a greeting that uses your name
[33m276ce54[m Handle Word table markers in screen text and clipboard output
[33mb9d2448[m Preserve capitalisation in corrections, speak small numbers as words
[33m7614851[m Proofread screen text, reading only the focused editable area
[33m769b716[m chore: update requirements.txt
[33mb53e13a[m Add log summary and JARVIS.md as single source of truth
[33m4fb8508[m Fix Word documents in place keeping formatting, read table text
[33ma1224e7[m Proofread follow-ups work standalone, announce count for long lists
[33m5bbe250[m Log every write action centrally, fall over on empty vision replies
[33m3cee7c7[m fix: Allow Jarvis to fall back to Gemini if Groq fails during camera ops. Also allow Jarvis to save a chart, even if user doesn't answer 'yes' immediately.
[33mf290a6d[m Log plot_chart, save_chart, hide_chart, and take_screenshot to the journal
[33m6fc30bd[m fix: improve Jarvis' log file reading ability
[33m3295b8a[m chore: add log file for everything Jarvis does
[33m6c0cf92[m Fix stale click element, add injection guard and audit journal
[33m812fd8f[m Fix: allow camera to settle before asking Jarvis to interpret, otherwise it's too dark to recognise
[33m17361f1[m Preserve verbatim punctuation for make_note, append_file, and look — same normalisation bug as type_text/copy_to_clipboard
[33m66f4f82[m Fix send_keys escaping/timing corruption; recover punctuation lost during command normalisation for type_text and copy_to_clipboard
[33m0230a8f[m Add screen reading and control ability to Jarvis
[33m3d3533a[m Add screen reading and control ability to Jarvis
[33m57446b9[m Add camera picture saving ability to Jarvis
[33m1a3659e[m Add camera opening and image reading ability to Jarvis
[33m1f470a7[m Add narrow pronoun memory, handle dropped objects in clipboard commands
[33m049b5c5[m Remove single entries from notes and files, including Word documents
[33m068aa48[m Add line graph plotting ability and varied phrasing to Jarvis
[33m2ae25fd[m Add csv reading and subsequent graph plotting abilities to Jarvis
[33mce0dba6[m Anchor projection beam to both window edges
[33me42d329[m Add headline images, HUD anchoring and projection beam
[33me6dc8fb[m Add live market strip to the news panel
[33m8fd7ee8[m Add news story expansion with panel highlighting
[33m5441f85[m Add news headlines panel with region filters on the free path
[33m996ef47[m Close browser-hosted apps by window, keeping other browser windows open
[33m4b9c28a[m Close UWP apps by window not process, name files when listing
[33m0f17534[m Clamp HUD text, prefer installed apps over websites, never close a browser by title
[33mba3e55d[m Allow notes to be treated as a file for copy and clipboard
[33me85151f[m Fix HUD telemetry overlap, add file to clipboard, append to Word docs
[33m65bcea9[m Match spoken names to any file extension, append to Word documents
[33m6d2398e[m Detect code rather than reading it aloud, relax append phrasing
[33mf9d0879[m Append without saying file, report missing files instead of guessing
[33mffb7952[m Add file read and copy to the free path, gated on the file existing
[33mc9b4232[m Fix noisy-room endpointing, correct LLM prompt, remove debug tracing
[33m7732907[m fix: battery prompts only when thresholds crossed
[33m8b401d2[m File operations, battery warnings, spoken confirmations, speed work
[33m8debe6f[m Add sandboxed file operations, battery warnings, spoken confirmations
[33m6dc248f[m Add proactive battery warnings at 80, 50 and 30 percent
[33m8d21e85[m fix: train Jarvis to recognise his own name
[33ma71a37b[m Add live waveform meter to HUD, generalise split app name recovery
[33mb5f92d7[m fix: improve broken app words recognition - e.g. 'im done with out luck' should close outlook
[33mf68efa5[m Fix note questions creating notes, fix HUD stuck on speaking
[33m87c807e[m Report follow-up window expiry on every audio block
[33m671b36f[m Freeze follow-up window at speech start, not transcription end
[33mc17215b[m Add swappable speech engines with local Whisper support
[33mc0edb6b[m Relax confidence filter, add follow-up window, configurable weather location
[33m285400a[m Fix wake word stripping on mishearings, add note phrasings
[33me6400e0[m Add notes with local capture and playback
[33mc188a59[m Add fuzzy fast-path matching and more system status phrasings
[33m803cf5a[m Local timer and clipboard parsing, fuzzy fast-path matching
[33m4357470[m Adaptive endpoint delay for short commands, more clipboard phrasings
[33md40c4cc[m Add local timer parsing, provider cooldown, fuller HUD replies
[33ma96e412[m fix: add requirements.txt to github - so collaborators can see the packages needed
[33m71ff7d3[m Add multi-provider LLM pool with automatic failover
[33me439eb8[m Add local fast path, time-aware greeting, concurrent actions
[33m817dd4b[m Add clipboard and screenshot skills, grammar-assisted wake detection
[33m63f66a1[m feat: add screenshot capability to Jarvis
[33m3c9fc06[m Add timers and reminders with speech serialisation
[33m78a559b[m Rebuild HUD as Iron Man style reactor with live telemetry
[33mcf32f80[m Add general question answering
[33m23ce020[m Add close_project for individual Cursor windows
[33ma24270c[m feat: add desktop control capabilities to Jarvis
[33mc4c00d1[m Add volume, media, and window controls
[33mabe908b[m Add Cursor project skills, require clear action verb in intent
[33ma099a6d[m Add wake word gating with self-hearing suppression
[33m14cae30[m Add weather and time capabilities to Jarvis
[33m6630086[m Add query intent architecture with time skill
[33ma8f91aa[m Make HUD ring pulse in time with JARVIS voice amplitude
[33ma73d2b1[m Filter low-confidence noise from listener, add startup launcher
[33m217db86[m Add PyQt6 arc-reactor HUD with live state, move voice loop to worker thread
[33mc70fe83[m Replace SAPI5 voice with edge-tts neural voice and local fallback
[33m09d05e9[m fix: remove apps.txt
[33m999a77f[m Add website intent, fix Outlook close and pgAdmin close reliability
[33m7b6efff[m Improve dynamic application window detection
[33m2c4f267[m Add LLM-powered natural language application control
[33mc840e5c[m Improve dynamic application window detection
[33m23365b3[m Add application launch and close actions
[33mb629d03[m Build core voice assistant loop
[33m3b5dffe[m Add offline voice recognition
[33m199804a[m Add modern audio dependencies
[33m262c254[m Initial JARVIS setup
