@echo off
REM ============================================================
REM  Fantasy Football Assistant - desktop launcher
REM  Starts the local server (if not already running) and opens
REM  the app in your browser. Close the "Fantasy Football" window
REM  to stop the server.
REM ============================================================
cd /d "%~dp0"

REM --- Find a real Python interpreter (skip the Microsoft Store stub) ---
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py"
)
if not defined PY (
    echo Python 3 was not found. Install it from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH" on the first screen.
    pause
    goto :eof
)

REM --- If the server is already up on 8787, just open the browser ---
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('localhost',8787); $c.Close(); exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
    start "" "https://localhost:8787"
    goto :eof
)

REM --- Otherwise start the server; app.py opens the browser itself ---
start "Fantasy Football Assistant" "%PY%" app.py
