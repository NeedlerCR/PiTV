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

# ── Shared control key ────────────────────────────────────────────────
# tv_menu.py only accepts SIGNED commands on its UDP channel. Homebridge runs
# as a different user, so the key lives in /etc/pitv/control.key, readable by
# the 'pitv' group, with both users in it.
PITV_USER="${SUDO_USER:-$(id -un)}"
HB_USER="$(sed -n 's/^User=//p' /etc/systemd/system/homebridge.service \
                                /lib/systemd/system/homebridge.service 2>/dev/null | head -1)"
[ -z "$HB_USER" ] && id homebridge >/dev/null 2>&1 && HB_USER="homebridge"

sudo mkdir -p /etc/pitv
getent group pitv >/dev/null || sudo groupadd pitv
if [ ! -s /etc/pitv/control.key ]; then
    echo "Creating the shared control key (/etc/pitv/control.key)..."
    python3 -c "import secrets; print(secrets.token_hex(32))" \
        | sudo tee /etc/pitv/control.key >/dev/null
fi
sudo chgrp pitv /etc/pitv/control.key
sudo chmod 640 /etc/pitv/control.key
sudo usermod -aG pitv "$PITV_USER" 2>/dev/null || true
if [ -n "$HB_USER" ]; then
    sudo usermod -aG pitv "$HB_USER" 2>/dev/null || true
    echo "Control key shared with Homebridge user '$HB_USER'."
else
    echo "Could not find the Homebridge user — if the Home app's TV tile stops"
    echo "responding, paste the key from 'screen control key show' into the"
    echo "PiTV plugin's config (controlKey), or add its user to the 'pitv' group."
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