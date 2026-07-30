#!/bin/bash
# Exit on any error
set -e

echo "Deploying Pi TV updates..."

# Make sure all scripts are executable
chmod +x *.sh *.py 2>/dev/null || true

# Ensure destination directory exists
sudo mkdir -p /opt/pitv

# Safely copy scripts, configs, and service files (skipping hidden folders like .git)
sudo cp -f *.sh *.py *.service /opt/pitv/ 2>/dev/null || true
sudo cp -f .gitignore /opt/pitv/ 2>/dev/null || true

# Copy the Homebridge TV plugin folder (installed separately via npm — see CLAUDE.md)
if [ -d homebridge-pitv-tv ]; then
    sudo cp -rf homebridge-pitv-tv /opt/pitv/
fi

# Install the `screen` CLI (no extension, so not caught by the globs above)
if [ -f screen ]; then
    sudo cp -f screen /usr/local/bin/screen
    sudo chmod +x /usr/local/bin/screen
fi

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