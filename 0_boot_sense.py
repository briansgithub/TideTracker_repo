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
                # Check if we are already connected to this SSID to avoid redundant reconnections
                current_ssid = netman.get_connected_ssid()
                if current_ssid == ssid:
                    logging.info(f"Graceful exit: Already connected to '{ssid}'. Skipping redundant reconnection.")
                    return

                logging.info(f"Graceful exit: Reconnecting to saved WiFi '{ssid}'...")
                # Note: netman.connect_to_AP will stop the hotspot if it's currently active.
                netman.connect_to_AP(conn_type=conn_type, ssid=ssid, username=username, password=password)
            else:
                logging.info("Graceful exit: No saved WiFi SSID found to reconnect to.")
        except Exception as e:
            logging.error(f"Error reconnecting to WiFi on exit: {e}")

if IS_RPI:
    wifi_libdir = '/home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/src'
    maindir = '/home/pi/TideTracker_repo'
    if os.path.exists(wifi_libdir):
        sys.path.append(wifi_libdir)

else:
    maindir = os.path.dirname(os.path.realpath(__file__))
    wifi_libdir = os.path.join(maindir, 'forked_wifi-connect-headless-rpi','src')
    if os.path.exists(wifi_libdir):
        sys.path.append(wifi_libdir)

if os.path.exists(wifi_libdir):
    sys.path.append(wifi_libdir)

import netman
import http_server


# Define the GPIO pin you want to monitor
run_mode_pin = 16  # Replace with your GPIO pin number
done_pin = 26  # Replace with your BCM pin number


# Set up GPIO mode and pin
GPIO.setmode(GPIO.BCM)
GPIO.setup(run_mode_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
GPIO.setup(done_pin, GPIO.OUT)

# Define script names and their paths
auto_run_wifi_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'forked_wifi-connect-headless-rpi', 'scripts', 'run.sh')

plot_tides_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '2_pull_json_and_plot_test.py')
no_wifi_errors_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'no_wifi_paste_over.py')

command = "sudo systemctl start NetworkManager"
subprocess.run(command, shell=True, check=True)
logging.info('NetworkManager started')

exit_code = None
pin_state = GPIO.LOW

try:
    pin_state = GPIO.input(run_mode_pin)
    logging.info(f'GPIO Pin BCM# {run_mode_pin} is {pin_state} ({"SETUP" if pin_state == GPIO.HIGH else "RUN"} mode)')
    print(f"\n\nGPIO Pin BCM# {run_mode_pin} is {pin_state}\n")
    if pin_state == GPIO.HIGH:
        # User wants cleanup right before the setup script (although http_server.py does it too)
        logging.info('SETUP mode: cleaning up hotspot before launching script')
        http_server.cleanup()
        
        # sleep time removed. Cron job set to start 50s after boot
        logging.info(f'SETUP mode: launching wifi setup script: {auto_run_wifi_script_path}')
        result = subprocess.run(
            ['sudo', 'bash', auto_run_wifi_script_path, '-f'],
            stderr=subprocess.PIPE, text=True
        )
        exit_code = result
        if result.returncode != 0:
            err_msg = result.stderr.strip() if result.stderr else '(no stderr)'
            logging.error(f'SETUP mode: wifi setup script FAILED with code {result.returncode}: {err_msg}')
            print(f"Setup script stderr: {err_msg}")
            raise subprocess.CalledProcessError(result.returncode, result.args)
        logging.info(f'SETUP mode: wifi setup script exited with code {result.returncode}')
    else:
        # User wants cleanup right before the run script to clear any stale hotspot from a crash
        logging.info('RUN mode: cleaning up hotspot before internet check')
        http_server.cleanup()

        # sleep time removed. Cron job set to start 50s after boot
        
        if netman.have_active_internet_connection():
            logging.info(f'RUN mode: internet available, running tides script')
            print(f"--------- \nRunning the tides script located at:\n\t{plot_tides_script_path} ---------")
            exit_code = subprocess.run(['sudo', 'python3', plot_tides_script_path], check=True)
        else: 
            logging.info(f'RUN mode: no internet, running no-wifi script')
            print(f"--------- \nRunning the no-wifi script :\n\t{no_wifi_errors_script_path} ---------")
            exit_code = subprocess.run(['sudo', 'python3', no_wifi_errors_script_path], check=True)


except subprocess.CalledProcessError as e:
    logging.error(f"Error running subprocess: {e}")
    print(f"Error running subprocess: {e}")
    exit_code = e.returncode
except KeyboardInterrupt:
    logging.info("KeyboardInterrupt received, exiting.")
    print("\nStopping by user request (Ctrl+C).")
    exit_code = 130  # Standard exit code for SIGINT
except Exception as e:
    logging.error(f"An unexpected error occurred: {e}")
    print(f"An unexpected error occurred: {e}")
    exit_code = 1

finally:
    # 1. ALWAYS pulse the DONE pin as early as possible to respect the TPL5110's 2-minute window.
    # We do this before potentially slow cleanup tasks.
    try:
        logging.info('Sending DONE pulse to TPL5110')
        GPIO.output(done_pin, GPIO.LOW)
        time.sleep(0.5)  # 500 ms delay
        GPIO.output(done_pin, GPIO.HIGH)
        time.sleep(0.5)  # 500 ms delay
        GPIO.output(done_pin, GPIO.LOW)
    except Exception as e:
        logging.error(f"Failed to pulse DONE pin: {e}")

    # 2. Reconnect to saved WiFi if we exited gracefully
    # This ensures the Pi returns to a client state if it doesn't lose power immediately.
    reconnect_to_saved_wifi()

    # 3. Cleanup GPIO and resources
    GPIO.cleanup()
    logging.info('========== boot_sense.py finished ==========')


print(f"\nExit code: {exit_code}\n")
