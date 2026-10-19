Start of a new session. First, scan my Claude memory: list everything, then read /profile.md, /preferences.md, /topics/coding-standards.md, /areas/jarvis.md, /areas/askfiles.md and /people/cousin.md before you reply. Then read the latest commits in each repo (read-only git only: git log, git show; never git status or git diff on my machine).

## My projects (all on my Windows PC, linked folders)
- JARVIS: C:\Users\uqurr\Projects\JARVIS. A Windows Python voice assistant (main.py, voice.py, commands.py, phone.py, actions/detections.py, tools/run_tests.py, tools/test_detections.py). It has an ESP32-S3-CAM "house cam" in arduino/jarvis_camera/jarvis_camera.ino, built with PlatformIO. Envs: s3detect (normal) and s3profile (timings, -D FEMTO_PROFILE=1). tools/femto_build.py builds the model code into detect/ (git-ignored).
- FEMTO-ML: my cousin's ONNX to C compiler (Reuna-AI/FEMTO-ML). He has given me carte blanche. I cannot push to his GitHub (403), so changes go through my fork or patches. It runs YOLO26n int8 on the S3. My S3 work so far:
  - the fuse passes widen_logit_range, fuse_silu, int8_layout_paths, sigmoid_through_layout and fold_quantize_into_producer;
  - emitters for a TopK heap and a tiled Transpose;
  - an S3 SIMD conv kernel that uses the fast internal buffer, blocks of output channels and both cores, and is bit-exact against the ref;
  - femto_profile.c/.h, and tools/test_conv_kernels.c, which runs in QEMU.
- AskFiles: React Native/Expo file manager with native Kotlin modules, live on Play Store (com.askfiles.mobile).
- AskFiles-backend: Django on one free-tier Render service. It uses an X-API-Key header, has CORS locked down and is the AI fallback. JARVIS mobile piggybacks on modules/jarvis-network. The app and backend prompts must never contradict.

## Where JARVIS is now
- One look (picture to answer) takes about 2.6 s, down from 11 s.
- Unprompted announcements are PERFECT. Don't touch the board-side steady logic: 2 of 3 looks, person if seen this look or the last, sure at 0.6 or more, counts like "bottle x3".
- Current bug being fixed: asking "Jarvis, what has my house cam detected" gives "Nothing your house cam recognises" and then, after a pause, the cup. Things like the laptop and phone are never found when I ask.
  - Cause: look_now only asked the board once the board had answered a look before, so it never asked. It answered instantly from the old steady report.
  - Fix: look-fix.patch in the JARVIS root (md5 44da5ab9447d529bfa214a25a6ab0e71, applies cleanly to HEAD 5ae29eb, test_detections passes 46/46). It always asks the board and waits up to LOOK_WAIT 6 s. Only after the board fails to answer does it skip asking for NO_LOOK_RETRY 600 s.
  - JARVIS-side only, no reflash.
- Next, if I report results:
  - check whether the laptop and phone show up in the per-look scores printed on the monitor; if they never score, it is the model, not JARVIS;
  - possible false "someone is in view" (person scored 0.81 / 0.76 / 0.57 while pointed at bottles);
  - stray guesses (suitcase for a wire case, sink for a desk). An optional "that's not a suitcase" teach-by-location feature was offered, not built.
  - More FEMTO speed: the T7 transpose (about 82 ms), and convs are about 56% of the time.

## Rules for you
- Never touch secrets or .env.
- No hardcoding object names or env values.
- Give me patches. I apply and commit myself.
- Always give explicit numbered steps including: git apply, delete the patch, python tools/run_tests.py, how to run and what to ask, then git add -A, git commit -m with no double quotes, git push, wait for green Actions, merge to main.
- Run the full or affected tests yourself on a copy before saying done.
- Verify a committed patch's md5 on my machine (the commit sometimes writes a stale copy).
- Use surgical edits on big files.
- Don't guess at causes like "it was your arm"; check scores and logs first.
- Be concise and don't confuse me with step order.
