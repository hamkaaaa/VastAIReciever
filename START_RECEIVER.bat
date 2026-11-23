@echo off
cd /d "%~dp0"
if exist "VastRenderHub.exe" (
    start "" "%~dp0VastRenderHub.exe"
) else (
    python receiver.py
    pause
)
