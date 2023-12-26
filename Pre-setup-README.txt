Installation *requires* the current username to be "pi"!

Install git
    sudo apt-get install -y git

git clone https://github.com/briansgithub/TideTracker_repo.git

In order to execute setup scripts, you need to make them exeutable
    cd TideTracker_repo
    chmod +x *.sh

Run "sudo sh_setup.sh"
The Pi will reboot after ~21 minutes
After the pi has rebooted, CONNECT TO WIFI AT LEAST ONCE using
    sudo forked_wifi-connect-headless-rpi\scripts\run.sh

The command to edit the cron job in the setup script may not run from within a .sh script 
and MAY NEED TO BE RUN MANUALLY
(crontab -l; echo "@reboot sleep 50 && /home/pi/TideTracker_repo/script_to_run_on_boot.sh") | sort -u | crontab -

Or you can manually edit the crontab file by 
1. opening the crontab file in an editor with
    crontab -e
2. Inserting and saving the line:
    @reboot sleep 50 && /home/pi/TideTracker_repo/script_to_run_on_boot.sh


(a.sh is a little helper script to help with preparing the repo and running a test)