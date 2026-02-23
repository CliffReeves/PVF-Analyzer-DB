#!/bin/bash
echo "============================================"
echo " RFQ Bid Manager - PVF"
echo "============================================"
echo

# Move to the script's directory
cd "$(dirname "$0")"

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Please install Python 3.9+."
    exit 1
fi

echo "Installing / verifying dependencies..."
pip3 install flask openpyxl anthropic --quiet --upgrade

echo
echo "Starting server at http://localhost:5050"
echo "Press Ctrl+C to stop."
echo

# Optionally pre-set your API key:
# export ANTHROPIC_API_KEY="sk-ant-xxxx"

python3 rfq_app.py
