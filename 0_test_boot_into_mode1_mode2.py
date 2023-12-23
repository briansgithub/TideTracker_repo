import RPi.GPIO as GPIO
import time
import subprocess

# Define the GPIO pin you want to monitor
gpio_pin = 16  # Replace with your GPIO pin number

# Set up GPIO mode and pin
GPIO.setmode(GPIO.BCM)
GPIO.setup(gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

# Function to execute commands based on GPIO state
def execute_commands():
    if GPIO.input(gpio_pin) == GPIO.HIGH:
        # Command to execute when GPIO is HIGH
        # subprocess.run(["your_high_command_here"])
        print("RESULT: GPIO is HIGH")
    else:
        # Command to execute when GPIO is LOW
        # subprocess.run(["your_low_command_here"])
        print("RESULT: GPIO is LOW")

# Main loop
try:
    while True:
        execute_commands()
        time.sleep(1)  # Adjust the sleep duration as needed

except KeyboardInterrupt:
    pass

finally:
    GPIO.cleanup()
