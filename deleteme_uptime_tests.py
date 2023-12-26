import psutil
from datetime import datetime
import pytz

def get_boot_time():
    boot_time_timestamp = psutil.boot_time()
    boot_time_utc = datetime.utcfromtimestamp(boot_time_timestamp)
    boot_time_utc = boot_time_utc.replace(tzinfo=pytz.utc)  # Set the timezone to UTC
    boot_time_chicago = boot_time_utc.astimezone(pytz.timezone('America/Chicago'))  # Convert to Chicago time
    return boot_time_chicago

if __name__ == "__main__":
    boot_time = get_boot_time()
    print("Raspberry Pi Boot Time (Chicago Time Zone):", boot_time)
