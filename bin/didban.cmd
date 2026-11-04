@echo off
rem Opens Claude Code in the Didban role, on Windows. Put this folder on PATH, or pin a shortcut to this file.
setlocal
set "repo=%~dp0.."
rem Each gh call is counted: Git Bash finds meter\gh first, as on Linux.
set "PATH=%repo%\meter;%PATH%"
set "GH_METER_CALLER=didban"
cd /d "%repo%"
"%repo%\.venv\Scripts\python.exe" -X utf8 -m kandu.didban %*
