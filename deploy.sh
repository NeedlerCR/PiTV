#!/bin/bash
# Exit on any error
set -e

echo "Deploying Pi TV updates..."

# Make sure permissions are correct
chmod +x *.sh *.py

# Sync scripts into /opt/pitv/ (creates directory if missing)
sudo mkdir -p /opt/pitv
sudo cp -f *.sh *.py /opt/pitv/ 2>/dev/null || true

# Reload systemd services
echo "Reloading systemd service..."
sudo cp pitv-menu.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart pitv-menu.service

echo "Deploy complete!"