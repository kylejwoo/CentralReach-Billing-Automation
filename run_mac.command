#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  echo "Run setup_mac.command first."
  read -p "Press ENTER to close..."
  exit 1
fi
source .venv/bin/activate
python centralreach_claims.py
echo
read -p "Press ENTER to close..."
