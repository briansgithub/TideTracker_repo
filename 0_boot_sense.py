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
auto_run_wifi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'forked_wifi-connect-headless-rpi', 'scripts', 'run.sh')
delete_and_change_wifi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'forked_wifi-connect-headless-rpi', 'scripts', 'del-run.sh')
plot_tides_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '1_pull_json_and_plot.py')

try:
    if GPIO.input(gpio_pin) == GPIO.HIGH:
        command = "sudo systemctl start NetworkManager"
        subprocess.run(command, shell=True, check=True)
        time.sleep(15)
        subprocess.run(['sudo', 'bash', auto_run_wifi_path], check=True)
    else:
        print(f"\nRunning the wifi script located at:\n\t{auto_run_wifi_path}\n\t")
        subprocess.run(['sudo', 'bash', auto_run_wifi_path], check=True)

        print(f"\nRunning the tides script located at:\n\t{plot_tides_path}\n\t")
        subprocess.run(['sudo', 'python3', plot_tides_path], check=True)

finally:
    # Cleanup GPIO settings
    GPIO.cleanup()
