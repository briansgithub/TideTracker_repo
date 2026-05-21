#!/usr/bin/python

import os
import RPi.GPIO as GPIO
import subprocess
import sys
import time
import re
import logging
import json
from pathlib import Path

# Set up logging to terminal
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s: %(message)s',
    stream=sys.stdout
)
logging.info('========== boot_sense.py starting ==========')

# --- SINGLE INSTANCE LOCK ---
LOCK_FILE = "/tmp/tidetracker.lock"

def check_single_instance():
    """Prevents multiple copies of the script from fighting over CPU/GPIO."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                pid = int(f.read().strip())
            # Check if process with this PID is still running
            os.kill(pid, 0)
            logging.error(f"Another instance is already running (PID {pid}). Exiting to prevent conflict.")
            sys.exit(0)
        except (OSError, ValueError):
            # PID not running or file corrupt, safe to overwrite
            pass
            
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))

check_single_instance()

def is_raspberry_pi():
    CPUINFO_PATH = Path("/proc/cpuinfo")
    if not CPUINFO_PATH.exists():
        return False
    with open(CPUINFO_PATH) as f:
        cpuinfo = f.read()
    return re.search(r"^Model\s*:\s*Raspberry Pi", cpuinfo, flags=re.M) is not None

IS_RPI = is_raspberry_pi()

def reconnect_to_saved_wifi():
    """Attempts to reconnect to saved WiFi credentials on graceful exit."""
    persistent_data_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'tidetracker_persistent_data.json')
    if os.path.exists(persistent_data_path):
        try:
            with open(persistent_data_path, 'r') as f:
                data = json.load(f)
            ssid = data.get('wifi_ssid')
            password = data.get('wifi_password')
            username = data.get('wifi_username')
            conn_type = data.get('wifi_conn_type', 'NONE')
            
            if ssid:
                import netman
                # Check if we are already connected to this SSID to avoid redundant reconnections
                current_ssid = netman.get_connected_ssid()
                if current_ssid == ssid:
                    logging.info(f"Graceful exit: Already connected to '{ssid}'. Skipping redundant reconnection.")
                    return

                logging.info(f"Graceful exit: Reconnecting to saved WiFi '{ssid}'...")
                netman.connect_to_AP(conn_type=conn_type, ssid=ssid, username=username, password=password)
            else:
                logging.info("Graceful exit: No saved WiFi SSID found to reconnect to.")
        except Exception as e:
            logging.error(f"Error reconnecting to WiFi on exit: {e}")

# Path setups
if IS_RPI:
    wifi_libdir = '/home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/src'
else:
    maindir = os.path.dirname(os.path.realpath(__file__))
    wifi_libdir = os.path.join(maindir, 'forked_wifi-connect-headless-rpi','src')

if os.path.exists(wifi_libdir):
    sys.path.append(wifi_libdir)

# GPIO configuration
run_mode_pin = 16 
done_pin = 26

GPIO.setmode(GPIO.BCM)
GPIO.setup(run_mode_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
GPIO.setup(done_pin, GPIO.OUT)

# Script paths
auto_run_wifi_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'forked_wifi-connect-headless-rpi', 'scripts', 'run.sh')
plot_tides_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '2_pull_json_and_plot_test.py')
no_wifi_errors_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'no_wifi_paste_over.py')

# Ensure NetworkManager is running
try:
    subprocess.run("sudo systemctl start NetworkManager", shell=True, check=True)
    logging.info('NetworkManager started')
except subprocess.CalledProcessError as e:
    logging.warning(f"Could not start NetworkManager: {e}")

exit_code = None

try:
    pin_state = GPIO.input(run_mode_pin)
    logging.info(f'GPIO Pin BCM# {run_mode_pin} is {pin_state} ({"SETUP" if pin_state == GPIO.HIGH else "RUN"} mode)')

    if pin_state == GPIO.HIGH:
        # SETUP MODE
        import http_server # Lazy load
        logging.info(f'SETUP mode: launching wifi setup script: {auto_run_wifi_script_path}')
        result = subprocess.run(
            ['sudo', 'bash', auto_run_wifi_script_path, '-f'],
            stderr=subprocess.PIPE, text=True
        )
        exit_code = result.returncode
        if result.returncode != 0:
            err_msg = result.stderr.strip() if result.stderr else '(no stderr)'
            logging.error(f'SETUP mode: wifi setup script FAILED with code {result.returncode}: {err_msg}')
            raise subprocess.CalledProcessError(result.returncode, result.args)
        logging.info(f'SETUP mode: wifi setup script exited with code {result.returncode}')
    else:
        # RUN MODE
        import http_server # Lazy load for cleanup
        import netman
        
        logging.info('RUN mode: cleaning up hotspot before internet check')
        http_server.cleanup()

        logging.info('Waiting 10s for WiFi to settle...')
        time.sleep(10)
        
        if netman.have_active_internet_connection(timeout=5, retries=3):
            logging.info(f'RUN mode: internet available, running tides script')
            # Run the tides script. Note: We use check=True so it raises exception on failure
            result = subprocess.run(['sudo', 'python3', plot_tides_script_path], check=True)
            exit_code = result.returncode
        else: 
            logging.info(f'RUN mode: no internet, running no-wifi script')
            result = subprocess.run(['sudo', 'python3', no_wifi_errors_script_path], check=True)
            exit_code = result.returncode

except subprocess.CalledProcessError as e:
    logging.error(f"Subprocess failed with exit code {e.returncode}")
    exit_code = e.returncode
except KeyboardInterrupt:
    logging.info("KeyboardInterrupt received.")
    exit_code = 130
except Exception as e:
    logging.error(f"An unexpected error occurred: {e}")
    exit_code = 1

finally:
    # 1. Cleanup Instance Lock
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
        except:
            pass

    # 2. Reconnect to saved WiFi if we exited gracefully
    reconnect_to_saved_wifi()

    # 3. Cleanup GPIO and resources
    GPIO.cleanup()

    # 4. ALWAYS pulse the DONE pin as the VERY LAST step.
    # On Pi Zero, especially with heavy matplotlib, we want to ensure 
    # the OS has settled before cutting power.
    try:
        logging.info('Sending DONE pulse to TPL5110 (Powering Off)')
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(done_pin, GPIO.OUT)
        
        GPIO.output(done_pin, GPIO.LOW)
        time.sleep(0.5)
        GPIO.output(done_pin, GPIO.HIGH)
        time.sleep(0.5)
        GPIO.output(done_pin, GPIO.LOW)
    except Exception as e:
        logging.error(f"Failed to pulse DONE pin: {e}")

    logging.info(f'========== boot_sense.py finished (Exit Code: {exit_code}) ==========')

sys.exit(exit_code if exit_code is not None else 0)
