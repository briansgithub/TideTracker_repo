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



(a.sh is a little helper script to help with preparing the repo and running a test)