#!/bin/bash
cd "$(dirname "$0")"
echo "Starting SmartVibe Leads..."

# Install deps if needed
pip3 install -q -r requirements.txt

python3 app.py
