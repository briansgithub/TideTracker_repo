#!/usr/bin/python
import json
from .paths import get_persistence_path

def load_config():
    """Load persistent data from JSON."""
    path = get_persistence_path()
    if not path.exists():
        return {}
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading config: {e}")
        return {}

def save_config(data):
    """Save persistent data to JSON, merging with existing data."""
    existing = load_config()
    existing.update(data)
    path = get_persistence_path()
    try:
        with open(path, 'w') as f:
            json.dump(existing, f, indent=4)
        return True
    except Exception as e:
        print(f"Error saving config: {e}")
        return False
