@echo off
REM Runs the 3 agents once (use this from Windows Task Scheduler), or pass --schedule to keep running daily.
cd /d "%~dp0"
venv\Scripts\python -m agents.orchestrator %*
