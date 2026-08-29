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
sudo cp -f .gitignore VERSION /opt/pitv/ 2>/dev/null || true

# Copy the Homebridge TV plugin folder AND reinstall it into Homebridge.
# npm copies the plugin at install time, so refreshing /opt/pitv alone doesn't
# update the running plugin — Homebridge must reinstall it from that path.
if [ -d homebridge-pitv-tv ]; then
    sudo cp -rf homebridge-pitv-tv /opt/pitv/
    if command -v hb-service >/dev/null 2>&1; then
        echo "Reinstalling Homebridge PiTV TV plugin..."
        sudo hb-service add /opt/pitv/homebridge-pitv-tv 2>/dev/null || true
        sudo hb-service restart 2>/dev/null || true
    fi
fi

# Install the `screen` CLI (no extension, so not caught by the globs above)
if [ -f screen ]; then
    sudo cp -f screen /usr/local/bin/screen
    sudo chmod +x /usr/local/bin/screen
fi

# Reload systemd services
echo "Reloading systemd service..."
sudo cp pitv-menu.service /etc/systemd/system/
sudo cp pitv-guest.service /etc/systemd/system/
sudo cp pitv-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart pitv-menu.service
sudo systemctl enable pitv-guest.service pitv-admin.service 2>/dev/null || true
sudo systemctl restart pitv-guest.service
sudo systemctl restart pitv-admin.service

echo "----------------------------------------"
echo "Deploy complete! All files synced to /opt/pitv/"
echo "----------------------------------------"

# The emergency (master) code used to be hardcoded in files that are in git, so
# say so plainly until this Pi has one of its own.
if ! python3 /opt/pitv/pin-admin.py emergency status >/dev/null 2>&1; then
    echo
    echo "!! The emergency code on this Pi is still the one published in git."
    echo "!! Set your own now:  screen emergency set <6 digits>"
    echo
fi

# Prompt for reboot requiring strict capital 'Y'
read -p "Do you want to reboot the Raspberry Pi now? [Y/n]: " answer

if [ "$answer" = "Y" ]; then
    echo "Rebooting Pi..."
    sudo reboot
else
    echo "Reboot skipped."
fi