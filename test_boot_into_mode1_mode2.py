import RPi.GPIO as GPIO
import time

# Define the GPIO pin you want to monitor
gpio_pin = 17  # Replace with your GPIO pin number

# Set up GPIO mode and pin
GPIO.setmode(GPIO.BCM)
GPIO.setup(gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

# Function to print statements based on GPIO state
def print_state():
    if GPIO.input(gpio_pin) == GPIO.HIGH:
        print("GPIO pin is HIGH")
    else:
        print("GPIO pin is LOW")

# Main loop
try:
    while True:
        print_state()
        time.sleep(1)  # Adjust the sleep duration as needed

except KeyboardInterrupt:
    pass

finally:
    GPIO.cleanup()
