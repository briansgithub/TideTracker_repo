#!/usr/bin/python

import os
import sys
import time
import logging
import subprocess

# Standard libraries for RPi
try:
    import RPi.GPIO as GPIO
except ImportError:
    # Fallback for non-RPi testing
    class GPIO:
        BCM = IN = OUT = LOW = HIGH = PUD_DOWN = 0
        @staticmethod
        def setmode(a): pass
        @staticmethod
        def setup(a, b, pull_up_down=0): pass
        @staticmethod
        def input(a): return 0
        @staticmethod
        def output(a, b): pass
        @staticmethod
        def cleanup(): pass

# Project-specific shared utils
import tt_utils

# Project modules (now that tt_utils added paths)
import netman
import http_server
import no_wifi_paste_over
import importlib

# Lazy import for the plotter to avoid loading all its heavy libs (matplotlib, etc.) 
# unless we actually need to run it in RUN mode.
def run_tide_plotter():
    plotter = importlib.import_module("2_pull_json_and_plot_test")
    plotter.run_plot()

# Set up logging to terminal
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    stream=sys.stdout
)
logging.info('========== TideTracker Boot Sequence Starting ==========')

# Define the GPIO pins
run_mode_pin = 16 
done_pin = 26

def pulse_done_pin():
    """Pulse the DONE pin for the TPL5110 timer."""
    try:
        logging.info('Sending DONE pulse to TPL5110')
        GPIO.output(done_pin, GPIO.LOW)
        time.sleep(0.5)
        GPIO.output(done_pin, GPIO.HIGH)
        time.sleep(0.5)
        GPIO.output(done_pin, GPIO.LOW)
    except Exception as e:
        logging.error(f"Failed to pulse DONE pin: {e}")

def main():
    # Set up GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(run_mode_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(done_pin, GPIO.OUT)

    # Ensure NetworkManager is running
    try:
        subprocess.run(["sudo", "systemctl", "start", "NetworkManager"], check=True)
        logging.info('NetworkManager verified')
    except Exception as e:
        logging.warning(f'Could not start NetworkManager: {e}')

    try:
        pin_state = GPIO.input(run_mode_pin)
        mode_str = "SETUP" if pin_state == GPIO.HIGH else "RUN"
        logging.info(f'Boot Mode: {mode_str} (Pin {run_mode_pin} is {pin_state})')

        if pin_state == GPIO.HIGH:
            # --- SETUP MODE ---
            logging.info('Entering SETUP mode: Launching WiFi configuration portal')
            http_server.cleanup() # Clean slate
            
            # Call http_server.main directly (Phase 1 improvement: no subprocess/run.sh)
            # We use force_setup=True (-f) to ensure the hotspot starts
            http_server.main(
                address='0.0.0.0', 
                port=80, 
                ui_path=str(tt_utils.WIFI_DIR / "ui"), 
                rcode='', 
                delete_connections=False, 
                force_setup=True
            )
        else:
            # --- RUN MODE ---
            logging.info('Entering RUN mode: Checking internet connection')
            http_server.cleanup() # Ensure no stale hotspot
            
            if netman.have_active_internet_connection():
                logging.info('Internet available, updating tide display')
                run_tide_plotter()
            else: 
                logging.info('No internet, displaying WiFi error screen')
                no_wifi_paste_over.run_error_display()

    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt received.")
    except Exception as e:
        logging.error(f"Error in boot sequence: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Always pulse the DONE pin to keep the TPL5110 happy
        pulse_done_pin()
        
        # Reconnect to saved WiFi if we are exiting SETUP mode gracefully
        netman.reconnect_to_last_wifi()
        
        GPIO.cleanup()
        logging.info('========== TideTracker Boot Sequence Finished ==========')

if __name__ == "__main__":
    main()
