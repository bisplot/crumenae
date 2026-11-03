@echo off
rem Windows: double-click to start CryptoLedger (it opens in your browser).
cd /d "%~dp0"
where py >nul 2>nul && (py -m cryptoledger %*) || (python -m cryptoledger %*)
pause
