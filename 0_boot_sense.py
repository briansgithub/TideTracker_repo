import os
import RPi.GPIO as GPIO
import subprocess
import time
import requests

# Define the GPIO pin you want to monitor
gpio_pin = 16  # Replace with your GPIO pin number

# Set up GPIO mode and pin
GPIO.setmode(GPIO.BCM)
GPIO.setup(gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

# Define script names and their paths
pi_wifi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'forked_wifi-connect-headless-rpi', 'scripts', 'run.sh')
NOAA_pull_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '1_pull_json_and_plot.py')
refresh_eInk_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '2_update_epd_7in5_V2_screen.py')

try:
    if GPIO.input(gpio_pin) == GPIO.HIGH:
        command = "sudo systemctl start NetworkManager"
        subprocess.run(command, shell=True, check=True)
        time.sleep(15)
        # pi_wifi_path = pi_wifi_path.replace('run.sh', 'del-run.sh') # to delete wifi connections
        subprocess.run(['sudo', 'bash', pi_wifi_path], check=True)
    else:
        time.sleep(15)
        subprocess.run(['python3', NOAA_pull_script_path], check=True)
        subprocess.run(['python3', refresh_eInk_path], check=True)

finally:
    # Cleanup GPIO settings
    GPIO.cleanup()
