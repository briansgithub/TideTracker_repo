#!/usr/bin/python
# -*- coding:utf-8 -*-

import RPi.GPIO as GPIO
import time

# Set up GPIO mode and pin
GPIO.setmode(GPIO.BOARD)
GPIO.setup(11, GPIO.OUT)

try:
    # Toggle the pin state
    while True:
        GPIO.output(11, GPIO.HIGH)
        time.sleep(1)
        GPIO.output(11, GPIO.LOW)
        time.sleep(1)

except KeyboardInterrupt:
    # Cleanup GPIO settings on keyboard interrupt
    GPIO.cleanup()
