#!/bin/bash
# Exit on any error
set -e

echo "Deploying Pi TV updates..."

# Make sure permissions are correct for all scripts
chmod +x *.sh *.py 2>/dev/null || true

# Sync ALL files (.sh, .py, .service, .gitignore, etc.) into /opt/pitv/
sudo mkdir -p /opt/pitv
sudo cp -rf . /opt/pitv/

# Reload systemd services
echo "Reloading systemd service..."
sudo cp pitv-menu.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart pitv-menu.service

echo "----------------------------------------"
echo "Deploy complete! All files synced to /opt/pitv/"
echo "----------------------------------------"

# Prompt for reboot requiring strict capital 'Y'
read -p "Do you want to reboot the Raspberry Pi now? [Y/n]: " answer

if [ "$answer" = "Y" ]; then
    echo "Rebooting Pi..."
    sudo reboot
else
    echo "Reboot skipped."
fi