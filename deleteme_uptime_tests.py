#!/usr/bin/python
import psutil
from datetime import timedelta, datetime
import json

def get_uptime():
    # Get system uptime in seconds
    uptime_seconds = psutil.boot_time()

    # Calculate the time since last reboot
    uptime_timedelta = timedelta(seconds=uptime_seconds)

    return uptime_timedelta

def get_current_time():
    # Get the current time
    current_time = datetime.now()
    
    return current_time

def get_time_difference(uptime_timedelta, current_time):
    # Calculate the time difference between now and uptime
    uptime_date = datetime.utcfromtimestamp(0) + uptime_timedelta
    time_difference = current_time - uptime_date

    return time_difference

def save_to_json(uptime_timedelta, current_time, time_difference, filename="uptime.json"):
    data = {
        "uptime_seconds": uptime_timedelta.total_seconds(),
        "uptime_formatted": str(uptime_timedelta),
        "current_time": str(current_time),
        "time_difference": str(time_difference)
    }
    with open(filename, "w") as json_file:
        json.dump(data, json_file, indent=4)

def main():
    uptime_timedelta = get_uptime()
    current_time = get_current_time()

    # Calculate the time difference
    time_difference = get_time_difference(uptime_timedelta, current_time)

    print(f"Time since last system reboot: {uptime_timedelta}")
    print(f"Current time: {current_time}")
    print(f"Time difference: {time_difference}")
    
    # Save the uptime information to a JSON file
    save_to_json(uptime_timedelta, current_time, time_difference)

if __name__ == "__main__":
    main()
