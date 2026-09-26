"""The file hologram: "show me my files", then numbers.

Runs in a sandboxed JARVIS folder. Checked:

- what is said: show, show a subfolder's files, close; numbers as digits,
  words, ordinals and "number three", with sound-alikes only after "file";
  and the numbers are claimed only while the hologram is showing, so "what
  time is it" or "set a timer for five minutes" still reach their commands;
- what is listed: folders by name, then files newest first, numbered; never
  JARVIS's own files, keys, sign-ins or hidden files, nor anything outside
  the JARVIS folder;
- what is said back: what a card is, its first lines (without markdown
  marks), an empty file, a blank Word document, a video
  with no text, a picture shown, a folder's contents, a number out of range;
- open: a folder steps inside and "go back" comes out; a document opens with
  its own program; a program or script is never run;
- pages of ten, with numbers that stay with their files; page words
  ("page one", "go to previous page") are pages, never a card; folders in a
  person's order, and go back returns to the page a folder was on;
- the panel draws the cards and a click names the card under it; the arrows
  show only where there is somewhere to go, and a tap on one turns the page;
  the SAY: line calls its example folder or file; settled frames do not
  redraw the cards, so the HUD keeps its pace;
- through commands: no model call, and "show me my files" is the hologram.

    python tools/test_file_hologram.py
"""

import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tools import sandbox  # noqa: E402  (must precede actions imports)

folder = sandbox.activate()

from actions import file_hologram as fh  # noqa: E402


failures = 0


def check(condition, message):
    global failures
    print(("PASS " if condition else "FAIL ") + message)
    failures += not condition


def write(relative, content="", when=None):
    path = os.path.join(folder, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode) as handle:
        handle.write(content)
    if when:
        os.utime(path, (when, when))
    return path


now = time.time()

for name, count in (("Reports", 3), ("Notes", 2), ("images", 1)):
    for index in range(count):
        write(f"{name}/item {index}.txt", "x", now - 5000 - index)

write("shopping list.txt", "Milk, two pints\n\nEggs\nBread\nButter\n", now - 100)
write("empty notes.txt", "", now - 200)
write("holiday.mp4", b"\0" * 2048, now - 300)
write("setup.bat", "echo hi", now - 400)
write("memory.txt", "JARVIS's own", now - 50)                  # protected
write("mcp.json.before-edit", "{\"token\": 1}", now - 60)      # holds keys
write("outlook_token_cache.json", "{}", now - 70)              # a sign-in
write(".jarvis-protocols.json", "{}", now - 80)                # hidden

shown = []
hidden = []
fh.set_listeners(on_view=shown.append, on_hide=lambda: hidden.append(True))

# ---- what is said, before it is showing ---------------------------------------------------

check(fh.asked("what's file three") is None and fh.asked("summarise file 3") is None,
      "with the hologram away, numbers are nobody's")
check(fh.asked("show me my files") == ("show", "") and fh.asked("Jarvis, show my files please") == ("show", ""),
      "'show me my files' shows the JARVIS folder")
check(fh.asked("show me my reports files") == ("show", "Reports") and fh.asked("show me the files in my notes folder") == ("show", "Notes"),
      "and a subfolder's files by its name")
check(fh.asked("show me my banana files") is None, "a folder that is not there is not a hologram request")

# ---- what is listed ---------------------------------------------------------------------------

said = fh.show("")
check(said == "Your JARVIS folder, sir: three folders and four files.", f"showing says what is there ({said!r})")
names = [card["name"] for card in shown[-1]["cards"]]
check(names == ["images", "Notes", "Reports", "shopping list.txt", "empty notes.txt", "holiday.mp4", "setup.bat"],
      f"folders by name, then files newest first ({names})")
check([card["number"] for card in shown[-1]["cards"]] == list(range(1, 8)), "numbered from one")
check(not any(name in names for name in ("memory.txt", "mcp.json.before-edit", "outlook_token_cache.json", ".jarvis-protocols.json")),
      "never JARVIS's own files, keys, sign-ins or hidden files")
check(fh.show("..") == "I can't find that folder, sir." and fh.show("../..") == "I can't find that folder, sir.",
      "nothing outside the JARVIS folder")
fh.show("")

# ---- numbers, as they are said -------------------------------------------------------------------

SAID = {
    "what's file three": ("describe", 3),
    "what is file 3": ("describe", 3),
    "what is number four": ("describe", 4),
    "summarise file three": ("preview", 3),
    "summarize file 4": ("preview", 4),
    "what's in file four": ("preview", 4),
    "what is file four about": ("preview", 4),
    "read the third file": ("preview", 3),
    "file won": ("preview", 1),
    "four": ("preview", 4),
    "open three": ("open", 3),
    "open number two": ("open", 2),
    "open file too": ("open", 2),
    "next page": ("page", 1),
    "previous page": ("page", -1),
    "go to previous page": ("page", -1),
    "go to the next page": ("page", 1),
    "go back a page": ("page", -1),
    "go back one page": ("page", -1),
    "page one": ("turn", 1),
    "page 2": ("turn", 2),
    "go to page to": ("turn", 2),
    "back to page one": ("turn", 1),
    "the second page": ("turn", 2),
    "page number three": ("turn", 3),
    "first page": ("turn", "first"),
    "last page": ("turn", "last"),
    "which page am i on": ("where", None),
    "go back": ("back", None),
    "close the files": ("close", None),
    "close the hologram": ("close", None),
}

for spoken, meant in SAID.items():
    check(fh.asked(spoken) == meant, f"{spoken!r} -> {fh.asked(spoken)}")

for spoken in ("what time is it", "set a timer for five minutes", "what is the file for", "open chrome",
               "remind me to buy two pints of milk", "what's the weather", "open the bbc news page"):
    check(fh.asked(spoken) is None, f"left alone while showing: {spoken!r}")

# ---- what is said back ----------------------------------------------------------------------------

said = fh.describe(3)
check(said == "Number three is Reports, a folder with three items, sir.", f"a folder ({said!r})")
said = fh.describe(4)
check(said.startswith("Number four is shopping list, a text file of ") and said.endswith("changed today, sir."),
      f"a file: what, how big, when ({said!r})")

said = fh.preview(4)
check(said == "Shopping list begins: Milk, two pints; Eggs; Bread.", f"its first three lines with words in ({said!r})")
check(shown[-1]["focus"]["lines"] == ["Milk, two pints", "Eggs", "Bread"] and shown[-1]["focus"]["number"] == 4,
      "and on the hologram")
check(fh.preview(5) == "Number five, empty notes, is empty, sir." and shown[-1]["focus"]["note"] == "THIS FILE IS EMPTY",
      "an empty file says so")
said = fh.preview(6)
check("there's no text in it to show" in said and "video" in said, f"a video has no text to show ({said!r})")
said = fh.preview(1)
check(said.startswith("images holds one item") and "Say open one to step inside." in said, f"a folder's contents ({said!r})")
check(fh.describe(12) == "There's no number twelve here, sir; they go up to seven.", "a number out of range says so")

# ---- open ------------------------------------------------------------------------------------------

opened = []
os.startfile = lambda path: opened.append(path)   # noqa: E731 -- Windows only; watched here

said = fh.open_card(7)
check("I won't run it from here" in said and not opened, f"a program or script is never run ({said!r})")
said = fh.open_card(4)
check(said == "Opening shopping list, sir." and opened == [os.path.join(folder, "shopping list.txt")],
      "a document opens with its own program")

said = fh.open_card(3)
check(said == "The Reports folder, sir: three files." and shown[-1]["trail"] == ["JARVIS", "Reports"],
      f"a folder steps inside ({said!r})")
check(fh.back() == "Your JARVIS folder, sir: three folders and four files.", "and go back comes out again")
check(fh.back() == "This is the top of your JARVIS folder, sir.", "no further than the top")

# ---- pages --------------------------------------------------------------------------------------------

for index in range(8):
    write(f"extra {index}.txt", f"extra {index}", now - 1000 - index)

said = fh.show("")
check(said.endswith("Say next page for more.") and shown[-1]["pages"] == 2 and len(shown[-1]["cards"]) == 10,
      f"ten to a page ({said!r})")
check(fh.page(1) == "Page two, sir: eleven to fifteen." and [c["number"] for c in shown[-1]["cards"]] == [11, 12, 13, 14, 15],
      "the next page, numbered on from ten")
check(fh.page(1) == "That's the last page, sir.", "and no page past the last")
check(fh.answer("page one") == "Page one, sir: one to ten." and shown[-1]["page"] == 0,
      "'page one' turns to page one, and does not describe card one")
check(shown[-1]["focus"] is None, "with nothing in focus")
check(fh.answer("page one") == "This is page one, sir.", "'page one' on page one says so")
check(fh.answer("page three") == "There are two pages, sir.", "a page that isn't there says how many there are")
check(fh.answer("last page") == "Page two, sir: eleven to fifteen.", "'last page' is the final page")
check(fh.answer("which page am i on") == "Page two of two, sir.", "and which page it is")
check(fh.answer("go to previous page") == "Page one, sir: one to ten.", "'go to previous page' turns back")
check(fh.answer("go to previous page") == "That's the first page, sir.", "and no page before the first")
fh.preview(12)
check(shown[-1]["page"] == 1 and shown[-1]["focus"]["number"] == 12, "asking about file twelve turns to its page")

# Folders in a person's order, and back to the page a folder was on.
import shutil  # noqa: E402

for index in range(1, 11):
    os.makedirs(os.path.join(folder, f"Archive {index}"))

fh.show("")
check([card["name"] for card in shown[-1]["cards"][:3]] == ["Archive 1", "Archive 2", "Archive 3"]
      and shown[-1]["cards"][9]["name"] == "Archive 10", "Archive 2 comes before Archive 10")
fh.answer("page two")
check(fh.answer("open thirteen").startswith("The Reports folder, sir"), "a folder on page two opens")
check(fh.answer("go back") == "Back in your JARVIS folder, sir: page two." and shown[-1]["page"] == 1,
      "and go back returns to page two, where it was")

for index in range(1, 11):
    shutil.rmtree(os.path.join(folder, f"Archive {index}"))

# A blank Word document is not "empty" (it weighs kilobytes), and markdown
# marks are not read out as part of a line.
from docx import Document  # noqa: E402

os.makedirs(os.path.join(folder, "Word"))
Document().save(os.path.join(folder, "Word", "blank.docx"))
write("Word/report.md", "# Porsche vs. BMW\n*Prepared for the cars folder*\n**Summary** in brief\n", now - 10)
fh.show("Word")
said = fh.preview(2)
check(said == "Report markdown file begins: Porsche vs. BMW; Prepared for the cars folder; Summary in brief.",
      f"markdown marks are left out of the first lines ({said!r})")
said = fh.preview(1)
check(said == "Number one, blank Word document, has no words in it yet, sir." and shown[-1]["focus"]["note"] == "NO WORDS IN IT YET",
      f"a blank Word document has no words yet ({said!r})")
shutil.rmtree(os.path.join(folder, "Word"))

fh.show("")
fh.page(1)
fh.preview(12)

check(fh.close() == "Files closed, sir." and hidden and not fh.showing(), "close the files puts it away")
check(fh.asked("what's file three") is None, "and the numbers are nobody's again")

# ---- the panel -------------------------------------------------------------------------------------------

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QImage, QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import files_panel  # noqa: E402

panel = files_panel.FilesPanel()
fh.set_listeners(on_view=panel.show_view.emit, on_hide=panel.hide_view.emit)
fh.show("")
fh.preview(4)
app.processEvents()
check(panel.isVisible(), "the hologram appears")
panel._shown_at -= 5
panel._focus_at -= 5

image = QImage(panel.size(), QImage.Format.Format_ARGB32)
image.fill(0)
panel.render(image, QPoint(0, 0))
lit = sum(1 for x in range(0, image.width(), 5) for y in range(0, image.height(), 5) if image.pixelColor(x, y).alpha() > 0)
check(lit > 2000, f"and is drawn ({lit} lit samples)")

# The SAY: line calls the example what it is, with a number on this page.
check(files_panel.hints(panel._view)[0].startswith("SAY: WHAT'S FOLDER 3 - SUMMARISE FOLDER 3 - OPEN 3"),
      f"on folders the hint says folder ({files_panel.hints(panel._view)[0]!r})")
check(files_panel.hints({"cards": [{"kind": "file", "number": 11}], "pages": 2, "page": 1, "trail": ["JARVIS"]})[0]
      == "SAY: WHAT'S FILE 11 - SUMMARISE FILE 11 - OPEN 11 - PREVIOUS PAGE - CLOSE FILES", "and file on files, from this page")

# Settled, the cards are drawn once, not every frame: drawing them all
# through the lean each frame slowed the HUD's reactor.
composed = []
real_compose = panel._compose
panel._compose = lambda now: (composed.append(now), real_compose(now))
panel._shown_at -= 5
panel._focus_at -= 5
panel.render(image, QPoint(0, 0))
before = len(composed)
for _ in range(5):
    panel._tick()
    panel.render(image, QPoint(0, 0))
check(len(composed) == before, f"settled frames reuse the drawn cards ({len(composed) - before} redraws in 5 frames)")
check(panel._timer.interval() == files_panel._STEADY_MS, "and the frame rate drops once nothing is moving")
panel._compose = real_compose

clicked = []
panel.card_clicked.connect(clicked.append)
target = panel._lean().map(panel.card_rect(2).center())


def mouse(kind, point):
    button = Qt.MouseButton.LeftButton
    return QMouseEvent(kind, point, panel.mapToGlobal(point), button,
                       Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button,
                       Qt.KeyboardModifier.NoModifier)


panel.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, target))
panel.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, target))
check(clicked == [3], f"a click names the card under it, lean and all ({clicked})")

# The arrows: page arrows only where there is a page that way, a back
# arrow only inside a folder, and a tap turns the page.
for index in range(3):
    write(f"more {index}.txt", f"more {index}", now - 3000 - index)

fh.show("")
app.processEvents()
panel.render(image, QPoint(0, 0))
check(sorted(panel._nav_rects) == ["next"], f"page one of two: only the right arrow ({sorted(panel._nav_rects)})")

navigated = []
panel.nav_clicked.connect(navigated.append)
arrow = panel._lean().map(panel._nav_rects["next"].center())
panel.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, arrow))
panel.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, arrow))
check(navigated == ["next"] and clicked == [3], f"a tap on the right arrow is next, not a card ({navigated})")

check(fh.navigate("next") == "Page two, sir: eleven to eighteen.", "and it turns the page")
app.processEvents()
panel.render(image, QPoint(0, 0))
check(sorted(panel._nav_rects) == ["previous"], f"the last page: only the left arrow ({sorted(panel._nav_rects)})")

for index in range(3, 13):
    write(f"more {index}.txt", f"more {index}", now - 3000 - index)

fh.show("")
fh.turn(2)
app.processEvents()
panel.render(image, QPoint(0, 0))
check(sorted(panel._nav_rects) == ["next", "previous"], f"a middle page: both arrows ({sorted(panel._nav_rects)})")

fh.show("Reports")
app.processEvents()
panel.render(image, QPoint(0, 0))
check(sorted(panel._nav_rects) == ["back"], f"inside a folder: the back arrow ({sorted(panel._nav_rects)})")
check(fh.navigate("back").startswith("Your JARVIS folder") and len(shown[-1]["trail"]) == 1, "and a tap on it comes out")
app.processEvents()
panel.render(image, QPoint(0, 0))
check("back" not in panel._nav_rects, "where the back arrow goes")

for index in range(13):
    os.remove(os.path.join(folder, f"more {index}.txt"))

fh.show("")
fh.close()
app.processEvents()
check(not panel.isVisible(), "and goes when the files are closed")

# ---- through commands ------------------------------------------------------------------------------------------

try:
    import commands
except Exception as error:   # a machine without JARVIS's full set of packages
    print(f"SKIP commands routing (could not import commands: {error})")
else:
    real_agent = commands.run_agent
    commands.run_agent = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called"))

    try:
        result = commands.handle_command("show me my files")
        check(result["intent"] == "file_hologram" and result["action"]().startswith("Your JARVIS folder, sir:"),
              "commands: 'show me my files' is the hologram, with no model call")
        result = commands.handle_command("summarise file four")
        check(result["intent"] == "file_hologram" and result["action"]().endswith("begins: Milk, two pints; Eggs; Bread."),
              "and 'summarise file four' reads its first lines")
        result = commands.handle_command("close the files")
        check(result["action"]() == "Files closed, sir.", "and closes")
        check(commands._fast_path("what's file three") is None or commands._fast_path("what's file three")["intent"] != "file_hologram",
              "with it away, 'what's file three' is not taken for it")
    finally:
        commands.run_agent = real_agent

sys.exit(1 if failures else 0)
