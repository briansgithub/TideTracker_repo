#!/usr/bin/python
import logging
import sys
import time
import importlib

# Project utilities
from app.utils import paths, config, station
import app.network.manager as netman

# Lazy loaders for heavy modules
def get_portal():
    return importlib.import_module("app.network.portal")

def get_plotter():
    return importlib.import_module("app.display.plotter")

def get_error_handler():
    return importlib.import_module("app.display.error_handler")

# Hardware dependencies (optional/mocked)
try:
    import RPi.GPIO as GPIO
except ImportError:
    from app.utils.legacy import GPIO

# Configuration
RUN_MODE_PIN = 16 
DONE_PIN = 26

def pulse_done_pin():
    """Pulse the DONE pin for the TPL5110 timer to signal shutdown."""
    try:
        logging.info('Sending DONE pulse to TPL5110')
        GPIO.output(DONE_PIN, GPIO.LOW)
        time.sleep(0.5)
        GPIO.output(DONE_PIN, GPIO.HIGH)
        time.sleep(0.5)
        GPIO.output(DONE_PIN, GPIO.LOW)
    except Exception as e:
        logging.error(f"Failed to pulse DONE pin: {e}")

def main():
    # Logging setup
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
        stream=sys.stdout
    )
    logging.info('========== TideTracker Start ==========')

    # Hardware setup
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RUN_MODE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(DONE_PIN, GPIO.OUT)

    try:
        # Check operating mode via physical pin
        pin_state = GPIO.input(RUN_MODE_PIN)
        is_setup_mode = (pin_state == GPIO.HIGH)
        logging.info(f'Mode: {"SETUP" if is_setup_mode else "RUN"} (Pin {RUN_MODE_PIN}={pin_state})')

        if is_setup_mode:
            # --- SETUP MODE ---
            portal = get_portal()
            portal.cleanup() 
            portal.main(
                address='0.0.0.0', 
                port=80, 
                ui_path=str(paths.UI_DIR), 
                rcode='', 
                delete_connections=False, 
                force_setup=True
            )
        else:
            # --- RUN MODE ---
            # Ensure no stale hotspot is running
            import app.network.services as services
            services.stop() 
            
            if netman.have_active_internet_connection():
                logging.info('Internet available, updating display')
                plotter = get_plotter()
                plotter.run_plot()
            else: 
                logging.info('No internet, showing error screen')
                err = get_error_handler()
                err.run_error_display()

    except KeyboardInterrupt:
        logging.info("Terminated by user.")
    except Exception as e:
        logging.error(f"Error in core loop: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # CRITICAL: Always pulse the DONE pin to keep the timer cycle correct
        pulse_done_pin()
        
        # Restore client WiFi if we were in setup
        netman.reconnect_to_last_wifi()
        
        GPIO.cleanup()
        logging.info('========== TideTracker Finished ==========')

if __name__ == "__main__":
    main()
