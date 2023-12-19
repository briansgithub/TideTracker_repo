#!/usr/bin/env bash 

# TODO:  Add echo statements before each batch of commands

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
echo -e "\n##### BOOT SPEED UP #####\n" 

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

echo -e "\n##### END BOOT SPEED UP #####\n" 
### END BOOT SPEED UP ###

###  Setup ###
echo -e "\n##### SETUP #####\n" 

y | sudo apt-get install python3
sudo apt install python3-pip
sudo -H pip3 install --upgrade pip

echo -e "\n##### END SETUP #####\n" 

### E-INK SETUP ###
echo -e "\n##### E-INK SETUP #####\n" 

sudo apt-get install python3-pil # Python Imaging Library, pillow library 	
sudo apt-get install python3-numpy
sudo pip3 install RPi.GPIO
sudo pip3 install spidev
# Enable SPI, code according to ChatGPT
echo -e "\n##### ENABLE SPI #####\n" 
sudo raspi-config nonint do_spi 0 
echo -e "\n##### END ENABLE SPI #####\n" 

echo -e "\n##### END E-INK SETUP #####\n" 
### END E-INK SETUP ###

# TODO: [add the tidetracker submodule  init && update commands]
# TODO: [Reboot]