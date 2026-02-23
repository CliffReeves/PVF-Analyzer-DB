@echo off
echo ============================================
echo  RFQ Bid Manager - PVF
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Download from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Install / upgrade dependencies
echo Installing dependencies...
pip install flask openpyxl anthropic --quiet --upgrade

echo.
echo Starting server...
echo Open http://localhost:5050 in your browser.
echo Press Ctrl+C to stop.
echo.

:: Set API key if you want to pre-configure it
:: set ANTHROPIC_API_KEY=sk-ant-xxxx

python "%~dp0rfq_app.py"
pause
