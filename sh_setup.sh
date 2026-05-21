#!/usr/bin/env bash 
# TideTracker System Setup Script
set -x

echo -e "\n##### INITIAL SETUP #####\n"
sudo apt-get update
sudo apt-get install -y vim python3 python3-pip python3-pil python3-matplotlib libopenblas-dev
sudo -H pip3 install --upgrade pip

# Ensure all scripts are executable
find . -type f \( -name "*.sh" -o -name "*.py" \) -exec chmod +x {} \;

### BOOT SPEED UP ### 
echo -e "\n##### SETUP: BOOT SPEED UP #####\n" 
CONFIG_FILE="/boot/config.txt"
if [ -f "$CONFIG_FILE" ]; then
    echo "disable_splash=1" | sudo tee -a "$CONFIG_FILE"
    echo "boot_delay=0" | sudo tee -a "$CONFIG_FILE"
    echo "dtoverlay=disable-bt" | sudo tee -a "$CONFIG_FILE"
    echo "dtparam=act_led_trigger=none" | sudo tee -a "$CONFIG_FILE"
fi

### PYTHON DEPENDENCIES ###
echo -e "\n##### SETUP: PYTHON LIBS #####\n"
sudo pip3 install --force-reinstall numpy
sudo pip3 install RPi.GPIO spidev timezonefinder ephem pytz scipy requests psutil

# Enable SPI
sudo raspi-config nonint do_spi 0 

### CRON SETUP ###
echo -e "\n##### SETUP: CRON JOB #####\n"
# Run main.py 75 seconds after boot (as root)
(sudo crontab -l; echo "@reboot sleep 75 && python3 $(pwd)/main.py > /dev/null 2>&1") | sort -u | sudo crontab -
sudo service cron restart

echo -e "\n##### SETUP COMPLETE. REBOOTING... #####\n"
sudo reboot
