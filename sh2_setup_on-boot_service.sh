#!/usr/bin/env bash 
set -x

# Set the username automatically
USERNAME=$(whoami)

# Define paths and filenames
SERVICE_FILE_CONTENT=$(cat <<EOL
# /etc/systemd/system/0_boot_sense.service

[Unit]
Description=Boot Sense
After=basic.target

[Service]
ExecStart=/usr/bin/python3 /home/$USERNAME/TideTracker_repo/0_boot_sense.py
Restart=no

[Install]
WantedBy=multi-user.target

EOL
)

SERVICE_FILENAME="0_boot_sense.service"
SYSTEMD_PATH="/etc/systemd/system/"

# Write the service file content to a temporary file
echo "$SERVICE_FILE_CONTENT" > "$SERVICE_FILENAME"

# Move the service file to /etc/systemd/system/
sudo mv "$SERVICE_FILENAME" "$SYSTEMD_PATH"

# Reload systemd to apply changes
sudo systemctl daemon-reload

# Enable and start the service
sudo systemctl enable "$SERVICE_FILENAME"
sudo systemctl start "$SERVICE_FILENAME"

echo "Boot service setup complete."
