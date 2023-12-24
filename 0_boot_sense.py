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

def is_internet_connected():
    try:
        # Try to send a request to a reliable server (e.g., Google)
        response = requests.get('https://api.tidesandcurrents.noaa.gov/api/prod/', timeout=5)
        print(response)
        return True
    except requests.ConnectionError:
        # Internet connection failed
        return False

try:
    if GPIO.input(gpio_pin) == GPIO.HIGH:
        command = "sudo systemctl start NetworkManager"
        subprocess.run(command, shell=True)
        time.sleep(15)
        # pi_wifi_path = pi_wifi_path.replace('run.sh', 'del-run.sh') # to delete wifi connections
        subprocess.run(['sudo', 'bash', pi_wifi_path])
    else:
        # Wait for internet connection before proceeding
        while not is_internet_connected():
            print("Waiting for internet connection...")
            time.sleep(5)  # Wait for 5 seconds before checking again

        # Continues once connected
        print("Internet connection established. Running the main script.")
        subprocess.run(['python3', NOAA_pull_script_path])
        subprocess.run(['python3', refresh_eInk_path])

finally:
    # Cleanup GPIO settings
    GPIO.cleanup()
