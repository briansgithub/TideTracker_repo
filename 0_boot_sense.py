print("\nCONTENTS OF '0_boot_sense.py. Good work\n")

'''import RPi.GPIO as GPIO
import subprocess

# Define the GPIO pin you want to monitor
gpio_pin = 16  # Replace with your GPIO pin number

# Set up GPIO mode and pin
GPIO.setmode(GPIO.BCM)
GPIO.setup(gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

# Define script names and their paths
pi_portal_sh = 'placeholder.sh'
get_data_script = '1_pull_json_and_plot.py'
update_screen = '2_update_epd_7in5_V2_screen.py'

try:
    if GPIO.input(gpio_pin) == GPIO.HIGH:
        subprocess.run(['sudo', 'bash', pi_portal_sh])
    else:
        subprocess.run(['python3', get_data_script])
        subprocess.run(['python3', update_screen])

finally:
    # Cleanup GPIO settings
    GPIO.cleanup()
'''