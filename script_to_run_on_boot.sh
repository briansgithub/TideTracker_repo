#!/usr/bin/env bash 


echo "Running script_to_run_on_boot.sh"
ps aux | grep -i 'forked_wifi-connect-headless-rpi' | grep -v grep | awk '{print $2}' | xargs sudo kill
sleep 1
/usr/bin/python3 /home/pi/TideTracker_repo/0_boot_sense.py
echo "Completed running script_to_run_on_boot.sh"