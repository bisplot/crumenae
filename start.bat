@echo off
rem Windows: double-click to start Crumenae (it opens in your browser).
cd /d "%~dp0"
where py >nul 2>nul && (py -m crumenae %*) || (python -m crumenae %*)
pause
