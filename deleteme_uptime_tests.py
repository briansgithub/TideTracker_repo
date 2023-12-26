import psutil
from datetime import datetime

def get_boot_time():
    boot_time_timestamp = psutil.boot_time()
    boot_time_datetime = datetime.utcfromtimestamp(boot_time_timestamp)
    return boot_time_datetime

if __name__ == "__main__":
    boot_time = get_boot_time()
    print("Raspberry Pi Boot Time:", boot_time)
