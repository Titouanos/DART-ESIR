@echo off
echo =====================================================
echo  DartVision Web Server
echo =====================================================
echo.

REM Install web-specific dependencies
pip install fastapi "uvicorn[standard]" python-multipart -q

echo.
echo Starting server at http://localhost:8000
echo Press Ctrl+C to stop.
echo.

cd /d "%~dp0"
python server.py
