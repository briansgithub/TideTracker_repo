In order to execute setup scripts, you need to make them exeutable
    chmod +x *.sh
    with sudo, install forked_pi_portal\scripts\rpi_headless_wifi_install.sh
    
install 
Run the setup script: sh1_setup.sh
Run the sh2_setup_on-boot-service.sh script (Do NOT run with sudo)
   Accidentally running as sudo will direct the bootup service to set the
   on-boot script to 
   .../root/home/... .py
   instead of 
   .../pi/home/... .py

   which will not make the neessary script run on boot

This is what the bootservice script does: 
To make the script run at startup: 
    1. Create a systemd service file. 
    2. Enable the systemd Service


