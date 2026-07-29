#!/usr/bin/env python3
"""
/opt/pitv/controller-to-keys.py

Converts generic Bluetooth gamepad events to keyboard arrow key events
via uinput.  Run automatically by tv_menu.py before launching ncurses
games, terminated when the game exits.

Requirements:
  sudo apt install python3-evdev
  sudo usermod -aG input charlieneedler   # then reboot

SDL2 games (chromium-bsu, lbreakout2, kobodeluxe) handle controllers
natively — this mapper is only needed for ncurses games.
"""

import sys
import time
import evdev
from evdev import ecodes, UInput

# Map gamepad buttons -> keyboard keys
BTN_TO_KEY = {
    ecodes.BTN_DPAD_UP:    ecodes.KEY_UP,
    ecodes.BTN_DPAD_DOWN:  ecodes.KEY_DOWN,
    ecodes.BTN_DPAD_LEFT:  ecodes.KEY_LEFT,
    ecodes.BTN_DPAD_RIGHT: ecodes.KEY_RIGHT,
    ecodes.BTN_SOUTH:      ecodes.KEY_ENTER,   # A button
    ecodes.BTN_EAST:       ecodes.KEY_ESC,     # B button
    ecodes.BTN_NORTH:      ecodes.KEY_SPACE,   # Y button
    ecodes.BTN_WEST:       ecodes.KEY_SPACE,   # X button
    ecodes.BTN_START:      ecodes.KEY_ENTER,
    ecodes.BTN_SELECT:     ecodes.KEY_ESC,
}

HAT_Y = {-1: ecodes.KEY_UP, 1: ecodes.KEY_DOWN}
HAT_X = {-1: ecodes.KEY_LEFT, 1: ecodes.KEY_RIGHT}

ALL_KEYS = list(set(BTN_TO_KEY.values()) | set(HAT_Y.values()) | set(HAT_X.values()))


def find_gamepads():
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities()
            if ecodes.EV_KEY in caps or ecodes.EV_ABS in caps:
                devices.append(dev)
        except Exception:
            pass
    return devices


def main():
    gamepads = find_gamepads()
    if not gamepads:
        print("No gamepad found — controller-to-keys exiting", flush=True)
        sys.exit(0)

    ui = UInput({ecodes.EV_KEY: ALL_KEYS}, name="pitv-virtual-kb")

    def press(key):
        ui.write(ecodes.EV_KEY, key, 1)
        ui.syn()
        time.sleep(0.04)
        ui.write(ecodes.EV_KEY, key, 0)
        ui.syn()

    try:
        # Only read from first gamepad found
        gamepad = gamepads[0]
        for event in gamepad.read_loop():
            if event.type == ecodes.EV_KEY and event.value == 1:
                key = BTN_TO_KEY.get(event.code)
                if key:
                    press(key)

            elif event.type == ecodes.EV_ABS:
                if event.code == ecodes.ABS_HAT0Y:
                    key = HAT_Y.get(event.value)
                    if key:
                        press(key)
                elif event.code == ecodes.ABS_HAT0X:
                    key = HAT_X.get(event.value)
                    if key:
                        press(key)

    except (KeyboardInterrupt, OSError):
        pass
    finally:
        ui.close()


if __name__ == "__main__":
    main()
