@echo off
setlocal

set "CHROME="

if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if not defined CHROME (
    echo JARVIS Chrome could not find Google Chrome.
    echo Install Google Chrome, then run this launcher again.
    exit /b 1
)

start "JARVIS Chrome" "%CHROME%" ^
    --remote-debugging-port=9222 ^
    --user-data-dir="%LocalAppData%\JarvisChrome"
