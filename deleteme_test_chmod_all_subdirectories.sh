#!/bin/bash

# Find all .py and .sh files and make them executable

find /your/directory/path -type f \( -name "*.py" -o -name "*.sh" \) -exec chmod +x {} \;
