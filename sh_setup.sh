#!/usr/bin/env bash 

# Shebang taken from the wifi portal setup
# Make this script executable with the following command:
# chmod +x run_command.sh

# Run the script with:
# ./run_command.sh

# Make the script executable with the following command:
# chmod +x thisScriptName.sh

sudo apt-get update
sudo apt-get upgrade

### BOOT SPEED UP# ### 
CONFIG_FILE="/boot/config.txt"

# Check if the config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: $CONFIG_FILE not found. Make sure you are running this script on a Raspberry Pi."
    exit 1
fi

# Add settings to config file
echo "disable_splash=1" | sudo tee -a "$CONFIG_FILE"
echo "boot_delay=0" | sudo tee -a "$CONFIG_FILE"
echo "dtoverlay=disable-bt" | sudo tee -a "$CONFIG_FILE"
#UPDATE THIS: echo "hdmi_blanking=1" | sudo tee -a "$CONFIG_FILE"
echo "Settings added to $CONFIG_FILE. Reboot for changes to take effect."

### END BOOT SPEED UP ###

###  Setup ###
sudo apt-get install python3
sudo apt install python3-pip
sudo -H pip3 install --upgrade pip

### E-INK SETUP ###
sudo apt-get install python3-pip
sudo apt-get install python3-pil # Python Imaging Library, pillow library 	
sudo apt-get install python3-numpy
sudo pip3 install RPi.GPIO
sudo pip3 install spidev
# Enable SPI, code according to ChatGPT
sudo raspi-config nonint do_spi 0 

### END E-INK SETUP ###