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
    
    # If git pull fails, set permissions on scripts using find
    find /home/pi/TideTracker_repo -type f \( -name "*.sh" -o -name "*.py" \) -exec chmod +x {} \;
    
    exit 1
fi

# Set permissions on scripts
find /home/pi/TideTracker_repo -type f \( -name "*.sh" -o -name "*.py" \) -exec chmod +x {} \;

# Uncomment the following line if you want to run the Python script
python3 0_boot_sense.py

# sudo /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/del-run.sh
