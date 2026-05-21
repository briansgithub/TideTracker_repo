#!/usr/bin/python
import csv
import re
from .paths import STATIONS_CSV_PATH

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
