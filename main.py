#!/usr/bin/python
import os
import sys
import logging

# Ensure the project root is in path for imports
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

# --- Automatic Root Escalation ---
def escalate_privileges():
    if os.name != 'nt' and os.geteuid() != 0:
        logging.info("Script not running as root. Attempting to escalate via sudo...")
        try:
            os.execvp("sudo", ["sudo", "python3"] + sys.argv)
        except Exception as e:
            print(f"CRITICAL: Failed to escalate privileges: {e}")
            sys.exit(1)

if __name__ == "__main__":
    escalate_privileges()
    
    # Lazy load core to keep escalation fast
    from app.core import main
    main()
