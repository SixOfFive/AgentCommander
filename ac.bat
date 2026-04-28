@echo off
rem AgentCommander launcher (Windows)
rem Runs the CLI without requiring a global pip install — uses the source tree directly.

setlocal
set "AC_ROOT=%~dp0"
set "PYTHONPATH=%AC_ROOT%src;%PYTHONPATH%"

rem Pick the first available Python: py -3 (Windows launcher) → python → python3
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 -m agentcommander %*
  exit /b %ERRORLEVEL%
)
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  python -m agentcommander %*
  exit /b %ERRORLEVEL%
)
where python3 >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  python3 -m agentcommander %*
  exit /b %ERRORLEVEL%
)
echo Could not find Python (py / python / python3). Install Python 3.10+ and retry.
exit /b 1
