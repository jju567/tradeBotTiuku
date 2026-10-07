@echo off
REM ==============================================================================
REM scripts/run_daily_sync.bat — Scheduled Daily Walk-Forward Sync for all 12 portfolios
REM Executes a single walk-forward cycle across P1–P12 at market close.
REM ==============================================================================

cd /d "%~dp0\.."

set LOG_DIR=logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "tokens=1-3 delims=/.- " %%a in ("%date%") do set TODAY=%%c-%%b-%%a
set LOG_FILE=%LOG_DIR%\daily_sync_%TODAY%.log

echo [%date% %time%] Starting tradeBotTiuku Daily Walk-Forward Sync... >> "%LOG_FILE%"
python main_controller.py --run-once >> "%LOG_FILE%" 2>&1
set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% equ 0 (
    echo [%date% %time%] ✅ Daily Sync completed successfully across all 12 portfolios. >> "%LOG_FILE%"
) else (
    echo [%date% %time%] ❌ Daily Sync encountered errors (Exit Code %EXIT_CODE%). >> "%LOG_FILE%"
)

exit /b %EXIT_CODE%
