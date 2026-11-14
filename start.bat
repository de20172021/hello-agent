@echo off
setlocal
rem Resolve a Python that ACTUALLY has the project deps installed.
rem A candidate is accepted only when `import uvicorn, fastapi` succeeds,
rem so a bare base-Anaconda python is skipped instead of crashing later.
rem
rem Order:
rem   1. %HELLOAGENT_PYTHON%      (per-session override)
rem   2. .python-path             (one line, next to this script, gitignored)
rem   3. .venv\Scripts\python.exe / venv\Scripts\python.exe
rem   4. conda / miniconda envs under the user profile
rem   5. py launcher, then python on PATH

if not defined HELLOAGENT_PYTHON goto :f_file
call :try "%HELLOAGENT_PYTHON%"
if not errorlevel 1 goto :run

:f_file
if not exist "%~dp0.python-path" goto :f_venv
set "FILEPY="
set /p FILEPY=<"%~dp0.python-path"
set "FILEPY=%FILEPY:"=%"
if not defined FILEPY goto :f_venv
call :try "%FILEPY%"
if not errorlevel 1 goto :run

:f_venv
call :try "%~dp0.venv\Scripts\python.exe"
if not errorlevel 1 goto :run
call :try "%~dp0venv\Scripts\python.exe"
if not errorlevel 1 goto :run

:f_conda
for /d %%D in ("%USERPROFILE%\.conda\envs\*") do (
    call :try "%%D\python.exe"
    if not errorlevel 1 goto :run
)
for /d %%D in ("%USERPROFILE%\anaconda3\envs\*" "%USERPROFILE%\miniconda3\envs\*") do (
    call :try "%%D\python.exe"
    if not errorlevel 1 goto :run
)

:f_launcher
call :try py
if not errorlevel 1 goto :run
call :try python
if not errorlevel 1 goto :run

echo.
echo [!] No Python with the project dependencies was found.
echo     Tried: HELLOAGENT_PYTHON, .python-path, .venv, venv,
echo            conda envs under %%USERPROFILE%%, "py", "python".
echo.
echo     Fix it in any of these ways:
echo       1. Install deps into the interpreter you want to use:
echo            python -m pip install -r "%~dp0requirements.txt"
echo       2. Create a project venv (start.bat will pick it up):
echo            python -m venv .venv
echo            .venv\Scripts\python -m pip install -r requirements.txt
echo       3. Pin an existing env once (saved to .python-path):
echo            echo C:\path\to\env\python.exe^> "%~dp0.python-path"
echo       4. Or override for this session only:
echo            set HELLOAGENT_PYTHON=C:\path\to\env\python.exe
echo.
pause
exit /b 1

:run
echo Using interpreter: %PYSHOWN%
%PYEXE% "%~dp0run.py" %*
if errorlevel 1 pause
exit /b 0

:try
if "%~1"=="" exit /b 1
%1 -c "import uvicorn, fastapi" >nul 2>nul
if errorlevel 1 exit /b 1
set "PYEXE=%1"
set "PYSHOWN=%~1"
exit /b 0
