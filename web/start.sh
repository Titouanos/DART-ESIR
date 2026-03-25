#!/usr/bin/env bash
set -e

echo "======================================================"
echo " DartVision Web Server"
echo "======================================================"
echo

# Install web-specific dependencies
pip install fastapi "uvicorn[standard]" python-multipart -q

echo
echo "Starting server at http://localhost:8000"
echo "Press Ctrl+C to stop."
echo

cd "$(dirname "$0")"
python server.py
