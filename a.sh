#!/usr/bin/env bash
# Quick Update and Run Script

echo "--- Pulling latest code ---"
git pull

echo "--- Updating environment permissions ---"
# Ensure all app directories and scripts are accessible
sudo chmod +x *.sh
sudo find ./app -name "*.py" -exec chmod +x {} \;

echo "--- Starting TideTracker (as root) ---"
sudo python3 main.py
