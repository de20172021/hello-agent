@echo off
setlocal
set "PYEXE=C:\Users\de201\.conda\envs\llm-run\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%~dp0run.py" %*
if errorlevel 1 pause
