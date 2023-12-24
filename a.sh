#!/usr/bin/env bash 
# A quick shell script for running the commands I need to run so often
set -x
git reset --hard
git pull
sudo chmod +x /home/pi/TideTracker_repo/*.sh
sudo chmod +x /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/*.sh
python3 0_boot_sense.py