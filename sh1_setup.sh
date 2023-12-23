#!/usr/bin/env bash 
set -x

# TODO:  Add echo statements before each batch of commands

# Shebang taken from the wifi portal setup
# Make this script executable with the following command:
# chmod +x run_command.sh

# Run the script with:
# ./run_command.sh


### SETUP: UPDATE & UPGRADE ###
echo -e "\n##### SETUP: UPDATE & UPGRADE #####\n" 
sudo apt-get update
sudo apt-get upgrade -y
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

# echo 0 | sudo tee /sys/class/leds/led0/brightness #? Disable LED

echo -e "\n##### END SETUP: BOOT SPEED UP #####\n" 

### END BOOT SPEED UP ###



###  SETUP: INSTALL PYTHON###
echo -e "\n##### SETUP: INSTALL PYTHON #####\n" 

echo "\nsudo apt-get install -y python3\n"
sudo apt-get install -y python3
sudo apt install -y python3-pip
sudo -H pip3 install --upgrade pip

echo -e "\n##### END SETUP: PYTHON INSTALL #####\n" 
###  END SETUP: INSTALL PYTHON###



### SETUP: E-INK SETUP ###
echo -e "\n##### SETUP: E-INK #####\n" 

sudo apt-get install -y python3-pil # Python Imaging Library, pillow library 	

# sudo apt-get install -y python3-numpy
sudo apt-get install -y libopenblas-dev # needed to fix numpy 
pip3 install --force-reinstall numpy

sudo pip3 install RPi.GPIO
sudo pip3 install spidev

# Enable SPI, code according to ChatGPT
echo -e "\n##### SETUP: ENABLE SPI (4-wire) #####\n" 
sudo raspi-config nonint do_spi 0 
echo -e "\n##### END SETUP: ENABLE SPI (4-wire) #####\n" 

echo -e "\n##### END SETUP: E-INK SETUP #####\n" 
### END E-INK SETUP ###



### SETUP: NOAA PULL AND PLOT LIBS ###
echo -e "\n##### SETUP: NOAA PULL AND PLOT LIBS #####\n" 

# Installing packages on Raspberry Pi Zero can be time-consuming due to its limited resources. You can try installing precompiled packages to save time:
sudo apt-get install -y python3-matplotlib
pip3 install timezonefinder 
pip3 install ephem
pip3 intall pytz

### END SETUP: NOAA PULL AND PLOT LIBS ###
echo -e "\n##### END SETUP: NOAA PULL AND PLOT LIBS #####\n" 


### Fix NUMPY install
echo -e "\n##### FIX NUMPY INSTALL #####\n" 
sudo apt-get update
sudo apt-get install -y libopenblas-dev
sudo pip3 install --force-reinstall numpy
### not executed, may not be neessary ### export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib/openblas-base



# TODO: Run the wifi portal init commands
# Must execute ./rpi_headless_wifi_install.sh as sudo
# Must execute ./run as sudo
    # (?) sudo /TideTracker_repo/submodules/forked_pi_portal/scripts/rpi_headless_wifi_install.sh 

# TODO: edit the cron file (?)


# sudo reboot