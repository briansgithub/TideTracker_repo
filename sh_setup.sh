#!/usr/bin/env bash 

# TODO:  Add echo statements before each batch of commands

# Shebang taken from the wifi portal setup
# Make this script executable with the following command:
# chmod +x run_command.sh

# Run the script with:
# ./run_command.sh


### SETUP: UPDATE & UPGRADE ###
echo -e "\n##### SETUP: UPDATE & UPGRADE #####\n" 
sudo apt-get update
sudo apt-get upgrade
echo -e "\n##### END SETUP: UPDATE & UPGRADE #####\n" 
### END SETUP: UPDATE & UPGRADE ###



### BOOT SPEED UP ### 
echo -e "\n##### SETUP: BOOT SPEED UP #####\n" 

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

echo -e "\n##### END SETUP: BOOT SPEED UP #####\n" 

### END BOOT SPEED UP ###



###  SETUP: INSTALL PYTHON###
echo -e "\n##### SETUP: INSTALL PYTHON #####\n" 

sudo apt-get install -y python3
sudo apt install python3-pip
sudo -H pip3 install --upgrade pip

echo -e "\n##### END SETUP: PYTHON INSTALL #####\n" 
###  END SETUP: INSTALL PYTHON###



### SETUP: E-INK SETUP ###
echo -e "\n##### SETUP: E-INK #####\n" 

sudo apt-get install python3-pil # Python Imaging Library, pillow library 	
sudo apt-get install python3-numpy
sudo pip3 install RPi.GPIO
sudo pip3 install spidev
# Enable SPI, code according to ChatGPT
echo -e "\n##### SETUP: ENABLE SPI (4-wire) #####\n" 
sudo raspi-config nonint do_spi 0 
echo -e "\n##### END SETUP: ENABLE SPI (4-wire) #####\n" 

echo -e "\n##### END SETUP: E-INK SETUP #####\n" 
### END E-INK SETUP ###



# TODO: Run the wifi portal init commands
    # (?) sudo /TideTracker_repo/submodules/forked_pi_portal/scripts/rpi_headless_wifi_install.sh 
# TODO: edit the cron file (?)
# sudo reboot