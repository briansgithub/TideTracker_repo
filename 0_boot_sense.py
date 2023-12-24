import os
import RPi.GPIO as GPIO
import subprocess
import time

# Define the GPIO pin you want to monitor
gpio_pin = 16  # Replace with your GPIO pin number

# Set up GPIO mode and pin
GPIO.setmode(GPIO.BCM)
GPIO.setup(gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

# Define script names and their paths
pi_wifi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'forked_wifi-connect-headless-rpi', 'scripts', 'run.sh')
get_data_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '1_pull_json_and_plot.py')
update_screen_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '2_update_epd_7in5_V2_screen.py')

try:
    if GPIO.input(gpio_pin) == GPIO.HIGH:
        subprocess.run(['python3', get_data_script_path])
        subprocess.run(['python3', update_screen_path])
    else:
        command = "sudo systemctl start NetworkManager"
        subprocess.run(command, shell=True)
        time.sleep(15)
        pi_wifi_path.repla('run.sh','run2.sh')
        subprocess.run(['sudo', 'bash', pi_wifi_path])

finally:
    # Cleanup GPIO settings
    GPIO.cleanup()
