#!/bin/bash

echo -e "\nGPIO TEST SCRIPT: $(basename $0)\n"

# GPIO pin number
GPIO_PIN=27  # Replace with your GPIO pin number

# Set up GPIO pin as input
gpio -g mode $GPIO_PIN in

# Function to check GPIO state
check_gpio_state() {
    local state=$(gpio -g read $GPIO_PIN)
    if [ $state -eq 1 ]; then
        echo "GPIO pin is HIGH"
    else
        echo "GPIO pin is LOW"
    fi
}

# Check GPIO state on boot
check_gpio_state

echo -e "\nEND GPIO TEST SCRIPT: $(basename $0)\n"