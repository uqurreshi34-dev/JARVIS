"""Show what JARVIS sees when asked to close an application.

Run with the app open:  python close_check.py netflix
"""

import sys

import psutil
import win32gui
import win32process

from actions.applications import ApplicationManager


target = " ".join(sys.argv[1:]).strip() or "netflix"

manager = ApplicationManager()
app = manager.find(target)

print(f"Looking for {target!r}")
print("-" * 66)

if not app:
    print("No Start menu entry matched that name.")
    print()
    print("Close names containing it:")
    for name in manager.applications:
        if target.casefold() in name.casefold():
            print(f"  {name}")
    raise SystemExit

print(f"  matched entry : {app.name!r}")
print(f"  app id        : {app.app_id}")
print(f"  kind          : {app.kind}")
print(f"  name tokens   : {sorted(app.tokens)}")
print()

print("Visible windows on the desktop:")
print("-" * 66)

found_any = False


def report(hwnd, _):
    global found_any

    if not win32gui.IsWindowVisible(hwnd):
        return

    title = win32gui.GetWindowText(hwnd)

    if not title:
        return

    _, pid = win32process.GetWindowThreadProcessId(hwnd)

    try:
        process = psutil.Process(pid)
        name = process.name()
    except Exception as error:
        name = f"<{error}>"

    strength = None

    try:
        strength = manager._match_strength(app, pid, [title])
    except Exception as error:
        strength = f"error: {error}"

    marker = ""

    if strength:
        marker = f"   <== MATCHES ({strength})"
        found_any = True

    # Only show windows that match, or that mention the target, to keep the
    # output readable.
    if strength or target.casefold() in title.casefold() or \
            target.casefold() in name.casefold():
        print(f"  pid {pid:>6}  {name:<28} {title[:34]!r}{marker}")


win32gui.EnumWindows(report, None)

print("-" * 66)

matches = manager._match_processes(app)

print(f"processes JARVIS would close: {list(matches) or 'none'}")

for pid, record in matches.items():
    print(f"  pid {pid}: {len(record['windows'])} window(s), "
          f"{'identity' if record['strong'] else 'title only'} match")

if not matches:
    print()
    print("Nothing matched, which is why close reports failure.")
    print("The window above that belongs to the app shows which process")
    print("actually owns it.")
