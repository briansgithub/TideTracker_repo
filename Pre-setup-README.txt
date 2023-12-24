Installation *requires* the current username to be "pi"!

Install git
    sudo apt-get install -y git

git clone https://github.com/briansgithub/TideTracker_repo.git

In order to execute setup scripts, you need to make them exeutable
    cd TideTracker_repo
    chmod +x *.sh

Run "sudo sh_setup.sh"
The Pi will reboot after ~21 minutes
After the pi has rebooted, run the script 
    sudo forked_wifi-connect-headless-rpi\scripts\rpi_headless_wifi_install.sh
    sudo forked_wifi-connect-headless-rpi\scripts\run.sh

Enter wifi info

Run sudo 0_boot_sense.py at least once because "Matplotlib is building the font cache; this may take a moment" 


a.sh is a little helper script to help with preparing the repo and running a test