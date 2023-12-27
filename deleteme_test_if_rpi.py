from pathlib import Path
import re

def is_raspberry_pi():
    # https://raspberrypi.stackexchange.com/a/139704/540
    CPUINFO_PATH = Path("/proc/cpuinfo")

    if not CPUINFO_PATH.exists():
        return False
    with open(CPUINFO_PATH) as f:
        cpuinfo = f.read()
    return re.search(r"^Model\s*:\s*Raspberry Pi", cpuinfo, flags=re.M) is not None

is_rpi = is_raspberry_pi()
print(f"Is RPi?: {is_rpi}")