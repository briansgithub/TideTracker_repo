#!/usr/bin/python
import os
import json
import csv
import re
import sys
from pathlib import Path

# --- Path Configuration ---
ROOT_DIR = Path(__file__).parent.absolute()
WIFI_DIR = ROOT_DIR / "forked_wifi-connect-headless-rpi"
WIFI_SRC_DIR = WIFI_DIR / "src"
PERSISTENT_DATA_PATH = ROOT_DIR / "tidetracker_persistent_data.json"
STATIONS_CSV_PATH = ROOT_DIR / "stations.csv"

# Add wifi source to path so it can be imported anywhere
if str(WIFI_SRC_DIR) not in sys.path:
    sys.path.append(str(WIFI_SRC_DIR))

# --- System Utilities ---
def is_raspberry_pi():
    """Detect if running on a Raspberry Pi."""
    cpuinfo_path = Path("/proc/cpuinfo")
    if not cpuinfo_path.exists():
        return False
    with open(cpuinfo_path) as f:
        cpuinfo = f.read()
    return re.search(r"^Model\s*:\s*Raspberry Pi", cpuinfo, flags=re.M) is not None

IS_RPI = is_raspberry_pi()

# --- Config/JSON Utilities ---
def load_config():
    """Load persistent data from JSON."""
    if not PERSISTENT_DATA_PATH.exists():
        return {}
    try:
        with open(PERSISTENT_DATA_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading config: {e}")
        return {}

def save_config(data):
    """Save persistent data to JSON, merging with existing data."""
    existing = load_config()
    existing.update(data)
    try:
        with open(PERSISTENT_DATA_PATH, 'w') as f:
            json.dump(existing, f, indent=4)
        return True
    except Exception as e:
        print(f"Error saving config: {e}")
        return False

# --- Station Utilities ---
_stations_cache = None

def get_stations():
    """Load and cache station data from CSV."""
    global _stations_cache
    if _stations_cache is not None:
        return _stations_cache
    
    _stations_cache = {}
    if not STATIONS_CSV_PATH.exists():
        return _stations_cache
        
    try:
        with open(STATIONS_CSV_PATH, newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                sid = row.get("Station ID")
                if sid:
                    _stations_cache[sid] = row
    except Exception as e:
        print(f"Error loading stations CSV: {e}")
    return _stations_cache

def get_station_info(station_id):
    """Get info for a specific station ID."""
    stations = get_stations()
    return stations.get(str(station_id))

def extract_number_from_string(input_string):
    """Extract digits from a string (e.g., NOAA station format). Defaults to 8725520."""
    if not input_string:
        return "8725520"
    match = re.search(r'\d+', str(input_string))
    return match.group(0) if match else "8725520"
