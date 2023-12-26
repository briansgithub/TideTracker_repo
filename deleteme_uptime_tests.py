#!/usr/bin/python
import psutil
from datetime import timedelta
import json

def get_uptime():
    # Get system uptime in seconds
    uptime_seconds = psutil.boot_time()

    # Calculate the time since last reboot
    uptime_timedelta = timedelta(seconds=uptime_seconds)

    return uptime_timedelta

def save_to_json(uptime, filename="uptime.json"):
    data = {"uptime_seconds": uptime.total_seconds(), "uptime_formatted": str(uptime)}
    with open(filename, "w") as json_file:
        json.dump(data, json_file, indent=4)

def main():
    uptime = get_uptime()
    print(f"Time since last system reboot: {uptime}")
    
    # Save the uptime information to a JSON file
    save_to_json(uptime)

if __name__ == "__main__":
    main()
