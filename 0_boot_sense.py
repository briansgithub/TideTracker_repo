import os
import RPi.GPIO as GPIO
import subprocess
import time
import requests
import sys

maindir = os.path.dirname(os.path.realpath(__file__))

wifi_libdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'forked_wifi-connect-headless-rpi','src')
if os.path.exists(wifi_libdir):
    sys.path.append(wifi_libdir)
import netman

# Define the GPIO pin you want to monitor
gpio_pin = 16  # Replace with your GPIO pin number

# Set up GPIO mode and pin
GPIO.setmode(GPIO.BCM)
GPIO.setup(gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

# Define script names and their paths
auto_run_wifi_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'forked_wifi-connect-headless-rpi', 'scripts', 'run.sh')
# delete_and_change_wifi_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'forked_wifi-connect-headless-rpi', 'scripts', 'del-run.sh')
# NEVER use -d to delete the wifi!? The wifi portal code seems to never autoconnect to wifi normally again after that

plot_tides_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '1_pull_json_and_plot.py')
no_wifi_errors_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'no_wifi_paste_over.py')

try:
    pin_state = GPIO.input(gpio_pin)
    print(f"\n\nGPIO Pin BCM# {gpio_pin} is {pin_state}\n")
    if pin_state == GPIO.HIGH:
        
        command = "sudo systemctl start NetworkManager"
        subprocess.run(command, shell=True, check=True)

        time.sleep(15) # wait some time for the system to initialize/stabilize
        subprocess.run(['sudo', 'bash', auto_run_wifi_script_path], check=True)
    else:
        time.sleep(15)
        if netman.have_active_internet_connection():
            print(f"--------- \nRunning the tides script located at:\n\t{plot_tides_script_path} ---------")
            subprocess.run(['sudo', 'python3', plot_tides_script_path], check=True)
        else: 
            subprocess.run(['sudo', 'python3', no_wifi_errors_script_path], check=True)

finally:
    # Cleanup GPIO settings
    GPIO.cleanup()