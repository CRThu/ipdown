@echo off
chcp 65001 >nul
uv run "%~dp0sangfor_auth.py" %*
pause
