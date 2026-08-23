"""Manual test for screen_control.type_text's send_keys escaping.

Run this, then click into a text field (Notepad, a browser address bar,
anything with a blinking cursor) within the countdown before each sample
types out. Check each line lands exactly as printed, with no menus,
dialogs, or dropped characters triggered along the way.
"""

import time

from actions import screen_control  # adjust if your import path differs

SAMPLES = [
    "50% off",
    "a+b",
    "call me (later)",
    "file~name",
    "ctrl^c literally",
    "me+jarvis@test.com",
    "{curly}",
    "hello world",  # plain text, sanity check for no regression
]


def main():
    if not screen_control.available():
        print("screen_control reports unavailable — check pywinauto/pywin32 install")
        return

    for sample in SAMPLES:
        print(f"\nClick into a text field. Typing {sample!r} in 3 seconds...")
        time.sleep(3)
        screen_control.type_text(sample)
        time.sleep(1)  # gap so lines don't run together


if __name__ == "__main__":
    main()
