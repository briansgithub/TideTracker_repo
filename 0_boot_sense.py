#!/usr/bin/python

import os
import RPi.GPIO as GPIO
import subprocess
import sys
import time
import re
from pathlib import Path


def is_raspberry_pi():
    CPUINFO_PATH = Path("/proc/cpuinfo")

    if not CPUINFO_PATH.exists():
        return False
    with open(CPUINFO_PATH) as f:
        cpuinfo = f.read()
    return re.search(r"^Model\s*:\s*Raspberry Pi", cpuinfo, flags=re.M) is not None

IS_RPI = is_raspberry_pi()

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

try:
    pin_state = GPIO.input(run_mode_pin)
    print(f"\n\nGPIO Pin BCM# {run_mode_pin} is {pin_state}\n")
    if pin_state == GPIO.HIGH:
        
        ### command = "sudo systemctl start NetworkManager"
        ### subprocess.run(command, shell=True, check=True)
        # sleep time removed. Cron job set to start 50s after boot

        # Define the terminal command
        command = "ps aux | grep -i 'forked_wifi-connect-headless-rpi' | grep -v grep | awk '{print $2}' | xargs sudo kill"
        
        # Execute the terminal command using subprocess
        subprocess.run(command, shell=True, check=True)
        time.sleep(1)  # 1 second delay

        exit_code = subprocess.run(['sudo', 'bash', auto_run_wifi_script_path], check=True)
    else:

        # sleep time removed. Cron job set to start 50s after boot
        
        if netman.have_active_internet_connection():
            print(f"--------- \nRunning the tides script located at:\n\t{plot_tides_script_path} ---------")
            exit_code = subprocess.run(['sudo', 'python3', plot_tides_script_path], check=True)
        else: 
            print(f"--------- \nRunning the no-wifi script :\n\t{no_wifi_errors_script_path} ---------")
            exit_code = subprocess.run(['sudo', 'python3', no_wifi_errors_script_path], check=True)


except subprocess.CalledProcessError as e:
    print(f"Error running subprocess: {e}")
except netman.InternetConnectionError as e:
    print(f"Internet connection error: {e}")
except Exception as e:
    print(f"An unexpected error occurred: {e}")

finally:
    # Cleanup GPIO settings

    http_server.cleanup()
    
    GPIO.output(done_pin, GPIO.LOW)
    time.sleep(0.5)  # 500 ms delay
    GPIO.output(done_pin, GPIO.HIGH)
    time.sleep(0.5)  # 500 ms delay
    GPIO.output(done_pin, GPIO.LOW)

    GPIO.cleanup() # Never going to get called?
    
    # call function to geracefully stop the wifi hotspot? 
    # httpserver.py cleanup() ?


print(f"\nExit code: {exit_code}\n")
