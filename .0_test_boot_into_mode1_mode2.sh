#!/bin/bash

# Define the GPIO pin you want to monitor
GPIO_PIN=16  # Replace with your GPIO pin number

# Function to check GPIO state and print statements
check_gpio_state() {
    local state=$(raspi-gpio get $GPIO_PIN | grep -oP '(?<=level=)[a-z]+')
    if [ "$state" == "high" ]; then
        echo "GPIO pin is HIGH"
    else
        echo "GPIO pin is LOW"
    fi
}

# Check GPIO state
check_gpio_state
