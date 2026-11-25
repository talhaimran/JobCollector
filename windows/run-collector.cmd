@echo off
rem Run by Task Scheduler; set it up with: .venv\Scripts\python -m collector schedule install
cd /d "%~dp0.."
if not exist logs mkdir logs
".venv\Scripts\python.exe" -m collector run >nul 2>> logs\collector.err.log
