#!/bin/bash

# Find all .py and .sh files and make them executable

find /home/pi/TideTracker_repo -type f \( -name "*.py" -o -name "*.sh" \) -exec chmod +x {} \;
