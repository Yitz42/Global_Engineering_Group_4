@echo off
rem Start the MeshLearn teacher hub on Windows: double-click this file.
rem The first start sets everything up (needs internet once, about a minute). After that it
rem works offline. Anything you add is passed to the hub, e.g.  start-hub.bat --radio-channel 3
setlocal
cd /d "%~dp0"

set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set "PY=python"
if not defined PY (
  echo MeshLearn needs Python 3.9 or newer. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer. Then double-click start-hub again.
  goto :stop
)

if not exist ".venv\Scripts\python.exe" (
  echo First start: setting MeshLearn up. This needs internet once and takes about a minute.
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Couldn't set up Python's private folder ^(.venv^).
    goto :stop
  )
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip >nul 2>nul
)

".venv\Scripts\python.exe" -c "import meshtastic" >nul 2>nul
if errorlevel 1 (
  echo Installing the radio software ^(meshtastic^)...
  set "PYTHONIOENCODING=utf-8"
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --no-compile -r requirements-radio.txt
  if errorlevel 1 (
    echo Couldn't install the radio software. Check the internet connection and start again.
    goto :stop
  )
)

echo Starting the MeshLearn hub...
".venv\Scripts\python.exe" -m meshlearn hub --radio-serial auto --host 0.0.0.0 --open %*
if errorlevel 1 echo The hub stopped. The lines above say why.

:stop
echo.
pause
