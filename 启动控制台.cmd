@echo off
rem ===========================================================================
rem  MockRoute Console launcher  (Windows)
rem
rem  Double-click this file to open the GUI.
rem  A visible console window is intentional: if the GUI fails to start, the
rem  error stays on screen instead of vanishing.
rem
rem  Interpreter lookup order: py launcher -> python on PATH.
rem  (Do NOT hardcode an absolute path here; it would break on other machines.)
rem ===========================================================================
setlocal
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (
  where python >nul 2>nul && set PY=python
)
if not defined PY (
  echo.
  echo [ERROR] Python 3 not found.
  echo         Install it from https://www.python.org/downloads/
  echo         and make sure "Add python.exe to PATH" is checked.
  echo.
  pause
  exit /b 1
)

echo Starting MockRoute Console ...
%PY% "tools\gui.py"
if errorlevel 1 (
  echo.
  echo [ERROR] GUI exited with an error. Read the message above.
  pause
)
endlocal
