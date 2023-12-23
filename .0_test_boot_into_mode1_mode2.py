import RPi.GPIO as GPIO
import time
import subprocess

# Define the GPIO pin you want to monitor
gpio_pin = 16  # Replace with your GPIO pin number

# Set up GPIO mode and pin
GPIO.setmode(GPIO.BCM)
GPIO.setup(gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

if GPIO.input(gpio_pin) == GPIO.HIGH:
    subprocess.run(["your_high_command_here"])
else:
    subprocess.run(["your_low_command_here"])

GPIO.cleanup()
