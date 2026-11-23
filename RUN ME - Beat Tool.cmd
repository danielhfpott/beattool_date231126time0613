@echo off
cd /d "%~dp0"
python -B beat_tool.py
if errorlevel 1 pause
