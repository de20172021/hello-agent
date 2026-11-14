@echo off
setlocal
rem Pick an interpreter: project .venv -> venv -> py launcher -> python on PATH.
rem No hard-coded absolute paths, so it works on any machine or user name.

set "PYEXE=%~dp0.venv\Scripts\python.exe"
if exist "%PYEXE%" goto :run

set "PYEXE=%~dp0venv\Scripts\python.exe"
if exist "%PYEXE%" goto :run

where py >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py"
    goto :run
)

set "PYEXE=python"

:run
if /i "%PYEXE%"=="py" (
    py -3 "%~dp0run.py" %*
) else (
    "%PYEXE%" "%~dp0run.py" %*
)
if errorlevel 1 pause
