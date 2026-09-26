"""Connect to each program-started service as JARVIS.exe would: with no console.

JARVIS.exe is built without a console window, so it has no error output
of its own. A service started as a program (the filesystem, OBS) is given
JARVIS's error output to write to; with none to give, it can fail before it
says a word -- while the same service works from python main.py.

Run it with pythonw, which, like JARVIS.exe, has no console:

    .venv\\Scripts\\pythonw.exe tools\\check_services_no_console.py

Each service is tried twice -- handed JARVIS's (missing) error output, as
before, and handed a log file, as JARVIS now does -- and the results are
written to mcp-check.txt in the JARVIS folder, then shown in a message box.
Nothing is changed, and no key or token is written anywhere.
"""

import asyncio
import os
import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import files, mcp_services  # noqa: E402


def _cause(error):
    return mcp_services._first_cause(error)


async def _try(target):
    from mcp import Client

    try:
        async with Client(target, read_timeout_seconds=20) as client:
            listed = await client.list_tools()
            return f"connected, {len(listed.tools)} tools"
    except BaseException as error:  # noqa: BLE001 -- the point is to see it
        cause = _cause(error)
        return f"FAILED: {type(cause).__name__}: {cause}"


def main():
    from mcp.client.stdio import StdioServerParameters, get_default_environment

    lines = [
        f"console: stdout={'none' if sys.stdout is None else 'present'}, "
        f"stderr={'none' if sys.stderr is None else 'present'}",
        f"python: {sys.executable}",
        "",
    ]

    for name, entry in mcp_services._read_config().items():
        entry = mcp_services._expand(entry) if not entry.get("url") else entry

        if entry.get("url") or not entry.get("command"):
            continue

        env = get_default_environment()
        env.update(entry.get("env") or {})
        parameters = StdioServerParameters(command=entry["command"], args=list(entry.get("args") or ()),
                                           env=env, cwd=entry.get("cwd"))

        before = asyncio.run(_try(parameters))
        after = asyncio.run(_try(mcp_services._transport(entry)))
        lines += [f"{name}:", f"  as before (JARVIS's error output): {before}",
                  f"  as now (a log file):               {after}", ""]

    report = "\n".join(lines)
    path = os.path.join(files.root(), "mcp-check.txt")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report)

    if sys.stdout is not None:
        print(report)

    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, report[:1500] + f"\n\nSaved to {path}", "JARVIS services check", 0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open(os.path.join(files.root(), "mcp-check.txt"), "w", encoding="utf-8") as handle:
            handle.write(traceback.format_exc())
        raise
