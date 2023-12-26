#!/usr/bin/python

import os
import RPi.GPIO as GPIO
import subprocess
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

plot_tides_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '1_pull_json_and_plot.py')
no_wifi_errors_script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'no_wifi_paste_over.py')

command = "sudo systemctl start NetworkManager"
subprocess.run(command, shell=True, check=True)

try:
    pin_state = GPIO.input(gpio_pin)
    print(f"\n\nGPIO Pin BCM# {gpio_pin} is {pin_state}\n")
    if pin_state == GPIO.HIGH:
        
        ### command = "sudo systemctl start NetworkManager"
        ### subprocess.run(command, shell=True, check=True)

        # sleep time removed. Cron job set to start 50s after boot

        exit_code = subprocess.run(['sudo', 'bash', auto_run_wifi_script_path], check=True)
    else:

        # sleep time removed. Cron job set to start 50s after boot
        
        if netman.have_active_internet_connection():
            print(f"--------- \nRunning the tides script located at:\n\t{plot_tides_script_path} ---------")
            exit_code = subprocess.run(['sudo', 'python3', plot_tides_script_path], check=True)
        else: 
            print(f"--------- \nRunning the no-wifi script :\n\t{plot_tides_script_path} ---------")
            exit_code = subprocess.run(['sudo', 'python3', no_wifi_errors_script_path], check=True)

except GPIOError as e:
    print(f"GPIO error: {e}")
except subprocess.CalledProcessError as e:
    print(f"Error running subprocess: {e}")
except netman.InternetConnectionError as e:
    print(f"Internet connection error: {e}")
except Exception as e:
    print(f"An unexpected error occurred: {e}")

finally:
    # Cleanup GPIO settings
    GPIO.cleanup()
    # call function to geracefully stop the wifi hotspot? 
    # httpserver.py cleanup() ?

print(f"\nExit code: {exit_code}\n")
