#!/usr/bin/env bash
# /opt/pitv/setup-input.sh
#
# One-time setup so PiTV can inject controller keystrokes into console games.
#
# Console games receive controller input through a virtual keyboard that
# controller-to-keys.py creates on /dev/uinput. The pitv-menu service runs as
# an ordinary user, which by default CANNOT read /dev/input/event* or write
# /dev/uinput — so key injection silently fails and the games ignore the pad
# (Tetris menu won't move, Pac-Man won't start, etc). This is the usual reason
# "the controller doesn't work in games" even though it works in the menu.
#
# This script grants that access: loads the uinput kernel module (now and at
# every boot), adds a udev rule making /dev/uinput group-writable by 'input',
# and puts the service user in the 'input' group.
#
# Run it ONCE, then reboot:
#   ./setup-input.sh
#   sudo reboot
set -euo pipefail

USER_NAME="${1:-charlieneedler}"

echo "Granting controller key-injection access for user: $USER_NAME"

# 1) Load the uinput kernel module now and on every boot.
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null

# 2) udev rule: let the 'input' group read/write /dev/uinput and event devices.
sudo tee /etc/udev/rules.d/99-pitv-input.rules >/dev/null <<'RULES'
KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
SUBSYSTEM=="input", GROUP="input", MODE="0660"
RULES

# 3) Add the service user to the 'input' group.
sudo usermod -aG input "$USER_NAME"

# 4) Reload and re-trigger udev so the rules apply immediately where possible.
sudo udevadm control --reload-rules
sudo udevadm trigger

echo
echo "Done. A REBOOT is required for the group change to take effect:"
echo "  sudo reboot"
echo
echo "After rebooting, open a game and check:  cat /tmp/pitv-mapper.log"
echo "It should list your controller(s). If it reports a uinput permission"
echo "error, re-run this script and make sure you rebooted."
