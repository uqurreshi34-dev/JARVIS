@echo off
REM ---------------------------------------------------------------------
REM  Start the Chrome that JARVIS can attach to.
REM
REM  WHY A SEPARATE PROFILE, AND NOT YOUR NORMAL ONE:
REM  From Chrome 136, --remote-debugging-port is IGNORED when it points at
REM  the default Chrome profile. Google closed that door deliberately, so
REM  that malware cannot attach to a real profile and read its saved
REM  passwords and cookies. The flag must now be paired with a
REM  --user-data-dir pointing somewhere non-standard, which is what this
REM  does. Without it Chrome starts normally, opens no port at all, and
REM  JARVIS reports that it cannot attach.
REM
REM  WHAT THIS MEANS IN PRACTICE:
REM  The first launch is a blank Chrome -- no history, no logins. Sign in
REM  to whatever you want JARVIS to be able to read (Gmail, GitHub...)
REM  ONCE, and it persists here permanently, because this directory is
REM  kept between runs rather than thrown away.
REM
REM  THE UPSIDE:
REM  A separate profile is a separate browser process, so this happily
REM  runs ALONGSIDE your normal Chrome. You no longer need to close
REM  anything first.
REM
REM  WORTH KNOWING:
REM  While this window is open, the debugging port has no password of its
REM  own. Anything running locally could attach to it. It listens on
REM  localhost only, so nothing on your network can reach it -- but treat
REM  this profile as the one JARVIS drives, and keep genuinely sensitive
REM  accounts (banking) in your normal Chrome instead.
REM ---------------------------------------------------------------------

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"

if not exist %CHROME% (
    set CHROME="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)

if not exist %CHROME% (
    echo Could not find chrome.exe. Edit this file and set CHROME to its path.
    pause
    exit /b 1
)

REM Kept out of the JARVIS project folder so it never lands in git.
set PROFILE=%LOCALAPPDATA%\JarvisChrome

if not exist "%PROFILE%" (
    mkdir "%PROFILE%"
    echo.
    echo  First run: this is a fresh Chrome profile.
    echo  Sign in to anything you want JARVIS to read. It will be
    echo  remembered next time.
    echo.
)

start "" %CHROME% --remote-debugging-port=9222 --user-data-dir="%PROFILE%"
