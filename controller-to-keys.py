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

Both the D-pad AND the analog sticks steer the game: the left stick's X/Y
axes (and the right stick as a fallback) are converted to arrow keys with a
dead-zone, and a held direction — D-pad or stick — auto-repeats so blocks in
Tetris keep sliding while you hold left/right.
"""

import sys
import time
import selectors
import evdev
from evdev import ecodes, UInput

# Map gamepad buttons -> keyboard keys
BTN_TO_KEY = {
    ecodes.BTN_SOUTH:      ecodes.KEY_ENTER,   # A button
    ecodes.BTN_EAST:       ecodes.KEY_ESC,     # B button
    ecodes.BTN_NORTH:      ecodes.KEY_SPACE,   # Y button
    ecodes.BTN_WEST:       ecodes.KEY_SPACE,   # X button
    ecodes.BTN_START:      ecodes.KEY_ENTER,
    ecodes.BTN_SELECT:     ecodes.KEY_ESC,
}

# Direction -> key, for both axes.  1/-1 are the two ends of each axis.
KEY_X = {-1: ecodes.KEY_LEFT, 1: ecodes.KEY_RIGHT}
KEY_Y = {-1: ecodes.KEY_UP,   1: ecodes.KEY_DOWN}

ALL_KEYS = list(
    set(BTN_TO_KEY.values()) | set(KEY_X.values()) | set(KEY_Y.values())
)

# Analog dead-zone.  Sticks report roughly -32768..32767; treat anything
# past ±60% as a firm direction and ignore the slack near centre.
DEADZONE = 20000

# Auto-repeat timing for a held direction (D-pad or stick).
INITIAL_DELAY  = 0.25   # wait before the first repeat
REPEAT_INTERVAL = 0.11  # then repeat this often


def find_gamepads():
    """Return input devices that look like gamepads (skip sensors/audio)."""
    EXCLUDE = ("imu", "motion", "sensor", "accel", "gyro",
               "hdmi", "jack", "audio", "sound")
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except Exception:
            continue
        name = dev.name.lower()
        if any(x in name for x in EXCLUDE):
            dev.close()
            continue
        caps = dev.capabilities()
        keys = caps.get(ecodes.EV_KEY, [])
        has_btn = any(isinstance(c, int) and c >= 0x120 for c in keys)  # BTN_*
        if has_btn and (ecodes.EV_KEY in caps):
            devices.append(dev)
        else:
            dev.close()
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
        time.sleep(0.03)
        ui.write(ecodes.EV_KEY, key, 0)
        ui.syn()

    # Held-direction state, tracked separately for the D-pad (hat) and the
    # analog stick so releasing one falls back to the other.
    hat   = {"x": 0, "y": 0}
    stick = {"x": 0, "y": 0}
    # For each axis: the currently-repeating direction and its next-fire time.
    repeat = {"x": [0, 0.0], "y": [0, 0.0]}  # [direction, next_time]

    def effective(axis):
        return hat[axis] if hat[axis] != 0 else stick[axis]

    def update_axis(axis, now):
        """Reconcile the repeat timer for one axis with its current input."""
        keymap = KEY_X if axis == "x" else KEY_Y
        want   = effective(axis)
        state  = repeat[axis]
        if want == 0:
            state[0] = 0
            return
        if want != state[0]:
            # New direction: fire immediately, then arm the repeat delay.
            press(keymap[want])
            state[0] = want
            state[1] = now + INITIAL_DELAY
        elif now >= state[1]:
            press(keymap[want])
            state[1] = now + REPEAT_INTERVAL

    sel = selectors.DefaultSelector()
    for dev in gamepads:
        try:
            sel.register(dev, selectors.EVENT_READ)
        except Exception:
            pass

    try:
        while True:
            for key, _ in sel.select(timeout=REPEAT_INTERVAL / 2):
                try:
                    for event in key.fileobj.read():
                        if event.type == ecodes.EV_KEY and event.value == 1:
                            k = BTN_TO_KEY.get(event.code)
                            if k:
                                press(k)
                        elif event.type == ecodes.EV_ABS:
                            code, val = event.code, event.value
                            if code == ecodes.ABS_HAT0X:
                                hat["x"] = (val > 0) - (val < 0)
                            elif code == ecodes.ABS_HAT0Y:
                                hat["y"] = (val > 0) - (val < 0)
                            elif code in (ecodes.ABS_X, ecodes.ABS_RX):
                                stick["x"] = (1 if val > DEADZONE else
                                              -1 if val < -DEADZONE else 0)
                            elif code in (ecodes.ABS_Y, ecodes.ABS_RY):
                                stick["y"] = (1 if val > DEADZONE else
                                              -1 if val < -DEADZONE else 0)
                except OSError:
                    pass

            now = time.time()
            update_axis("x", now)
            update_axis("y", now)

    except (KeyboardInterrupt, OSError):
        pass
    finally:
        ui.close()
        for dev in gamepads:
            try: dev.close()
            except Exception: pass


if __name__ == "__main__":
    main()
