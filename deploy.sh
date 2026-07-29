#!/bin/bash

# Exit immediately if any command fails
set -e

echo "🚀 Deploying updates from /home/charlieneedler to system locations..."

# 1. Ensure target directory /opt/pitv exists
sudo mkdir -p /opt/pitv

# 2. Copy scripts to /opt/pitv/ and set executable permissions
echo "📦 Deploying python and shell scripts to /opt/pitv/..."
sudo cp cec-cmd.sh controller-to-keys.py otp-setup.sh remote.py tv_menu.py tv-state.sh /opt/pitv/ 2>/dev/null || true
sudo chmod +x /opt/pitv/*.sh /opt/pitv/*.py 2>/dev/null || true

# 3. Deploy screen script to /usr/local/bin/ (if present)
if [ -f "screen" ]; then
    echo "📦 Deploying screen script to /usr/local/bin/..."
    sudo cp screen /usr/local/bin/screen
    sudo chmod +x /usr/local/bin/screen
fi

# 4. Deploy systemd service file (if present)
if [ -f "pitv-menu.service" ]; then
    echo "⚙️ Updating pitv-menu.service in /etc/systemd/system/..."
    sudo cp pitv-menu.service /etc/systemd/system/pitv-menu.service
fi

# 5. Reload systemd daemon and restart the service
echo "🔄 Reloading systemd daemon and restarting pitv-menu..."
sudo systemctl daemon-reload
sudo systemctl restart pitv-menu

echo "✅ Deployment complete! System updated."
