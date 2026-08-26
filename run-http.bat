@echo off
REM ---------------------------------------------------------------------------
REM Fantasy Football Assistant -- plain HTTP launcher (Windows)
REM
REM Same app as run.bat, but served over plain HTTP on port 8788 so your
REM browser shows NO "not private" certificate warning. This is your own
REM permanent local server -- open http://localhost:8788
REM
REM (Use run.bat instead if you need HTTPS, e.g. for the Yahoo sign-in flow.)
REM ---------------------------------------------------------------------------
cd /d "%~dp0"

set FF_FORCE_HTTP=1
set FF_PORT=8788

echo.
echo   Starting the Fantasy Football Assistant (HTTP)...
echo   When it's ready, open:  http://localhost:8788
echo   (Your browser opens automatically. Press Ctrl+C here to stop.)
echo.

REM Prefer the Python launcher (py), which avoids the Microsoft Store stub.
where py >nul 2>nul
if %errorlevel%==0 (
    py app.py
    goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python app.py
    goto :end
)

echo.
echo   Python 3 was not found on this computer.
echo   Install it from https://www.python.org/downloads/
echo   IMPORTANT: on the first install screen, check "Add python.exe to PATH".
echo   Then double-click run-http.bat again.
echo.
pause

:end
