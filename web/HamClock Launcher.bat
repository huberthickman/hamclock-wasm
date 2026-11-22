@echo off
rem Windows: double-click to start the HamClock launcher (needs Python from python.org)
cd /d "%~dp0"

rem the python.org installer always adds "py"; "python" only if "Add to PATH" was ticked
set "PY="
where py >nul 2>nul && set "PY=py -3" && set "PYW=pyw -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python" && set "PYW=pythonw"
)
if not defined PY goto nopython

rem also catches Windows' stand-in "python" that only opens the Microsoft Store
%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 goto notk

rem start the launcher without a console window, and close this one
start "" %PYW% launcher.py
exit /b 0

:nopython
echo Python 3 was not found.
echo.
echo Install it from https://www.python.org/downloads/ (its installer includes
echo tkinter, which the launcher needs), then double-click this file again.
echo.
pause
exit /b 1

:notk
echo Python was found, but it can't run the launcher: it has no tkinter, or it is
echo only Windows' shortcut to the Microsoft Store.
echo.
echo Install Python from https://www.python.org/downloads/ (its installer includes
echo tkinter), then double-click this file again.
echo.
pause
exit /b 1
