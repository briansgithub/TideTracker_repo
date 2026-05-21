#!/usr/bin/python
import os
import re
from pathlib import Path

# --- Root Directory ---
ROOT_DIR = Path(__file__).parent.parent.parent.absolute()

# --- Application Packages ---
APP_DIR = ROOT_DIR / "app"
UTILS_DIR = APP_DIR / "utils"
NETWORK_DIR = APP_DIR / "network"
DISPLAY_DIR = APP_DIR / "display"

# --- Data and Resources ---
DATA_DIR = ROOT_DIR / "data"
UI_DIR = ROOT_DIR / "ui"
RESOURCES_DIR = ROOT_DIR / "resources"
FONTS_DIR = RESOURCES_DIR / "fonts"

# --- Specific Files ---
PERSISTENT_DATA_PATH = DATA_DIR / "persistence.json"
STATIONS_CSV_PATH = DATA_DIR / "stations.csv"

# Legacy support for old filename if needed
OLD_PERSISTENT_DATA_PATH = DATA_DIR / "tidetracker_persistent_data.json"

# --- System Utilities ---
def is_raspberry_pi():
    """Detect if running on a Raspberry Pi."""
    cpuinfo_path = Path("/proc/cpuinfo")
    if not cpuinfo_path.exists():
        return False
    try:
        with open(cpuinfo_path) as f:
            cpuinfo = f.read()
        return re.search(r"^Model\s*:\s*Raspberry Pi", cpuinfo, flags=re.M) is not None
    except:
        return False

IS_RPI = is_raspberry_pi()

def get_persistence_path():
    """Return current persistence path, migration logic included."""
    if not PERSISTENT_DATA_PATH.exists() and OLD_PERSISTENT_DATA_PATH.exists():
        return OLD_PERSISTENT_DATA_PATH
    return PERSISTENT_DATA_PATH
