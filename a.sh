#!/usr/bin/env bash
# A quick shell script for running the commands I need to run often
set -x

# Reset Git repository
git reset --hard
if [ $? -ne 0 ]; then
    echo -e "\e[31mERROR: GIT RESET FAILED\e[0m" | tr '[:lower:]' '[:upper:]'
    # Reset permissions on scripts
    sudo chmod +x /home/pi/TideTracker_repo/*.sh
    sudo chmod +x /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/*.sh
    exit 1
fi



# Pull from Git
git pull
if [ $? -ne 0 ]; then
    echo -e "\e[31mERROR: GIT PULL FAILED\e[0m" | tr '[:lower:]' '[:upper:]'
    exit 1
fi

# Set permissions on scripts
sudo chmod +x /home/pi/TideTracker_repo/*.sh
sudo chmod +x /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/*.sh

# Uncomment the following line if you want to run the Python script
#python3 0_boot_sense.py
