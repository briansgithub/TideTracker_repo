#!/usr/bin/env bash
# TideTracker Migration and Fix Script

echo "--- 1. Pulling latest code from GitHub ---"
git pull

echo "--- 2. Updating script permissions ---"
sudo chmod +x *.sh
sudo find ./app -name "*.py" -exec chmod +x {} \;

echo "--- 3. Updating Boot Script (script_to_run_on_boot.sh) ---"
cat <<EOF > script_to_run_on_boot.sh
#!/usr/bin/env bash
echo "Running script_to_run_on_boot.sh"
sudo python3 /home/pi/TideTracker_repo/main.py
echo "Completed running script_to_run_on_boot.sh"
EOF
sudo chmod +x script_to_run_on_boot.sh

echo "--- 4. Updating Cron Job ---"
# Remove any old entries and add the new one
(crontab -l | grep -v "script_to_run_on_boot.sh"; echo "@reboot sleep 75 && /home/pi/TideTracker_repo/script_to_run_on_boot.sh > /dev/null 2>&1") | sort -u | crontab -
sudo service cron restart

echo "--- 5. Cleaning up legacy files ---"
# These files have been migrated to the app/ directory
rm -f 0_boot_sense.py 2_pull_json_and_plot_test.py tt_utils.py no_wifi_paste_over.py view_image.py

echo "--- DONE! System is updated to the new professional structure. ---"
echo "You can now run the project manually with: bash a.sh"
