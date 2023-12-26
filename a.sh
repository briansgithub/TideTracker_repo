#!/usr/bin/env bash
# A quick shell script for running the commands I need to run so often
# set -x

# Function to print colored text
print_error() {
    tput setaf 1;  # Set text color to red
    echo "ERROR: $1";
    tput sgr0;  # Reset text color
}

# Reset Git repository
git reset --hard
if [ $? -ne 0 ]; then
    print_error "GIT RESET FAILED!"
    # Reset permissions on scripts
    exit 1
fi

# Pull from Git
git pull
if [ $? -ne 0 ]; then
    print_error "GIT PULL FAILED!"
    exit 1
fi

# Set permissions on scripts
sudo chmod +x /home/pi/TideTracker_repo/*.sh
sudo chmod +x /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/*.sh

sudo chmod +x /home/pi/TideTracker_repo/*.py
sudo chmod +x /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/*.py

# Uncomment the following line if you want to run the Python script
#python3 0_boot_sense.py

### sudo /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/del-run.sh