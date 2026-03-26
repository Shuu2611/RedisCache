@echo off
REM Helper script to run benchmark with venv Python

echo ========================================
echo Running Optimized Benchmark
echo ========================================
echo.

cd /d "%~dp0.."

echo Using Python from virtual environment...
.venv\Scripts\python.exe benchmark.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Benchmark failed!
    echo.
    echo Common issues:
    echo   1. MongoDB not running: mongod --dbpath C:\data\db
    echo   2. Redis not running: redis-server
    echo   3. Packages not installed: pip install pymongo redis hiredis
)

pause
