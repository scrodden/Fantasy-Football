@echo off
REM Fantasy Football Assistant launcher (Windows)
cd /d "%~dp0"

REM Prefer the Python launcher (py) which avoids the Microsoft Store stub.
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
echo   Then double-click run.bat again.
echo.
pause

:end
