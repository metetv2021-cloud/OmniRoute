@echo off
rem DeepSeek Web API - Windows launcher. Double-click it: the first run opens a browser
rem window to sign in to DeepSeek, then the OpenAI-compatible API starts on
rem http://127.0.0.1:20129/v1. Extra arguments are passed through, e.g. "start.bat login".
setlocal
title DeepSeek Web API
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 deepseek_web_api.py %*
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3.9 or newer is required. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run this file again.
    pause
    exit /b 1
  )
  python deepseek_web_api.py %*
)
if errorlevel 1 pause
endlocal
