#!/usr/bin/env bash 
# Shebang taken from the wifi portal setup
# Make this script executable with the following command:
# chmod +x run_command.sh
# Run the script with:
# ./run_command.sh

set -x
sudo apt-get install -y vim
# Backup existing .vimrc file
cp ~/.vimrc ~/.vimrc_backup
# Add line numbers configuration to .vimrc
echo "set number" >> ~/.vimrc


### Pre-Setup ### 
echo -e "\n##### PRE-SETUP #####\n" 
sudo chmod +x /home/pi/TideTracker_repo/*.sh
sudo chmod +x /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/*.sh
    # Note that the cron job did not execute when the script_to_run_on_boot.sh was not made executable 

sudo chmod +x /home/pi/TideTracker_repo/*.py
sudo chmod +x /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/*.py

echo -e "\n##### END: PRE-SETUP #####\n" 
### END: Pre-Setup ###



### SETUP: UPDATE & UPGRADE ###
echo -e "\n##### SETUP: UPDATE & UPGRADE #####\n" 
sudo apt-get update
sudo apt-get upgrade -y
echo -e "\n##### END SETUP: UPDATE & UPGRADE #####\n" 
### END SETUP: UPDATE & UPGRADE ###


echo -e "\n##### PRE-SETUP #####\n" 
sudo chmod +x /home/pi/TideTracker_repo/*.sh
sudo chmod +x /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/*.sh

echo -e "\n##### RUN RPI HEADLESS WIFI SCRIPT #####\n" 
sudo /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/rpi_headless_wifi_install.sh
echo -e "\n##### END: RUN RPI HEADLESS WIFI SCRIPT #####\n" 

echo -e "\n##### END: PRE-SETUP #####\n" 
### END: Pre-Setup ###


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
# Check if running as root
echo "dtparam=act_led_trigger=none" | sudo tee -a "$CONFIG_FILE" 
# echo "arm_freq=800" | sudo tee -a "$CONFIG_FILE" 
# echo "force_turbo=1" | sudo tee -a "$CONFIG_FILE" 
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


### Fix NUMPY install
echo -e "\n##### SETUP: FIX NUMPY INSTALL #####\n" 
sudo apt-get install -y libopenblas-dev
sudo pip3 install --force-reinstall numpy
### not executed, may not be neessary ### export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib/openblas-base
echo -e "\n##### END SETUP: FIX NUMPY INSTALL #####\n" 



### SETUP: E-INK SETUP ###
echo -e "\n##### SETUP: E-INK #####\n" 

sudo apt-get install -y python3-pil # Python Imaging Library, pillow library 	

# sudo apt-get install -y libopenblas-dev # needed to fix numpy 
# sudo pip3 install --force-reinstall numpy

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
sudo pip3 install timezonefinder # for some reason this one also needs to be sudo
sudo pip3 install ephem
sudo pip3 install pytz 
sudo pip3 install scipy
sudo pip3 install requests

### END SETUP: NOAA PULL AND PLOT LIBS ###
echo -e "\n##### END SETUP: NOAA PULL AND PLOT LIBS #####\n" 



# Creating a boot service wasn't working, so changing to cron 

echo -e "\n##### SETUP: RUN SCRIPT ON BOOT - WRITE TO THE CRON TAB FILE #####\n" 
# Make cron file and edit it (?)
# crontab -e

# Backup current crontab
# view crontab contents with cmd "crontab -l"
# open the crontab in a text editor with "crontab -e"

# Add cron job to the file
##### This command may not work from this script and may need to be run manually!
(crontab -l; echo "@reboot sleep 60 && /home/pi/TideTracker_repo/script_to_run_on_boot.sh") | sort -u | crontab -
sudo service cron restart

##### 
echo -e "\n##### END SETUP: RUN SCRIPT ON BOOT - WRITE TO THE CRON TAB FILE #####\n" 



echo -e "\n##### SETUP RUN RPI HEADLESS WIFI SCRIPT #####\n" 
sudo /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/rpi_headless_wifi_install.sh
### The wifi portal run.sh code must be performed after a reboot!
### sudo /home/pi/TideTracker_repo/forked_wifi-connect-headless-rpi/scripts/run.sh
echo -e "\n##### END SETUP: RUN RPI HEADLESS WIFI SCRIPT #####\n" 

sudo pip3 install psutil
# for getting time-since-bootup and for writing python scripts to debug the cron job
sudo pip3 install json

sudo reboot