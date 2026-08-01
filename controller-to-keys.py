#!/usr/bin/env python3
"""
/opt/pitv/controller-to-keys.py

Converts up to two generic Bluetooth/USB gamepads into keyboard events
via uinput.  Launched by tv_menu.py before a game starts and terminated
when the game exits.

  Buttons follow the Nintendo layout (A on the right = BTN_EAST is the
  primary/confirm button; B on the bottom = BTN_SOUTH is back/quit).

  Player 1 (first pad)  -> arrow keys, plus:
        A  -> Space   (fire / start — Space Invaders, hard-drop in Tetris)
        X  -> Enter   (menu confirm — e.g. the bastet difficulty screen)
        Y  -> Enter
        +  -> Enter
        B  -> Esc     (back / quit)
        -  -> Esc
  Player 2 (second pad) -> W A S D, plus F / G as action keys, so a second
        controller can drive player 2 in two-player games (e.g. vitetris).

Both the D-pad AND the left analog stick steer; a held direction
auto-repeats so blocks keep sliding while you hold left/right.

REQUIREMENTS
  sudo apt install python3-evdev
  Run setup-input.sh ONCE so the service user can read /dev/input/event*
  and write /dev/uinput (adds a udev rule + the 'input' group).  Without
  that, UInput() below fails with a permission error (logged, then exit),
  and no keys reach the game.

SDL2 games (chromium-bsu, lbreakout2, vitetris) also read the keyboard via
evdev on the console, so this mapper is started for them too.
"""

import sys
import time
import selectors
import evdev
from evdev import ecodes, UInput

# Analog dead-zone.  Sticks report roughly -32768..32767; treat anything
# past this as a firm direction and ignore the slack near centre.
DEADZONE = 20000

# Auto-repeat timing for a held direction (D-pad or stick).
INITIAL_DELAY   = 0.25   # wait before the first repeat
REPEAT_INTERVAL = 0.11   # then repeat this often

# Per-player key maps.  Index 0 = player 1, index 1 = player 2.
PLAYERS = [
    {   # ── Player 1: arrows + primary action keys ──
        "x": {-1: ecodes.KEY_LEFT, 1: ecodes.KEY_RIGHT},
        "y": {-1: ecodes.KEY_UP,   1: ecodes.KEY_DOWN},
        "buttons": {
            ecodes.BTN_EAST:   ecodes.KEY_SPACE,   # A  -> fire / start / drop
            ecodes.BTN_SOUTH:  ecodes.KEY_ESC,     # B  -> back / quit
            ecodes.BTN_NORTH:  ecodes.KEY_ENTER,   # X  -> confirm / menu select
            ecodes.BTN_WEST:   ecodes.KEY_ENTER,   # Y  -> confirm / menu select
            ecodes.BTN_START:  ecodes.KEY_ENTER,   # +  -> menu select
            ecodes.BTN_SELECT: ecodes.KEY_ESC,     # -  -> back
        },
    },
    {   # ── Player 2: WASD + secondary action keys ──
        "x": {-1: ecodes.KEY_A, 1: ecodes.KEY_D},
        "y": {-1: ecodes.KEY_W, 1: ecodes.KEY_S},
        "buttons": {
            ecodes.BTN_EAST:   ecodes.KEY_F,       # A  -> player-2 fire / rotate
            ecodes.BTN_SOUTH:  ecodes.KEY_G,       # B  -> player-2 secondary
            ecodes.BTN_NORTH:  ecodes.KEY_F,
            ecodes.BTN_WEST:   ecodes.KEY_F,
            ecodes.BTN_START:  ecodes.KEY_ENTER,
            ecodes.BTN_SELECT: ecodes.KEY_ESC,
        },
    },
]

# Every key any player can emit — the uinput device must advertise them all.
ALL_KEYS = sorted({
    k
    for p in PLAYERS
    for k in list(p["x"].values()) + list(p["y"].values()) + list(p["buttons"].values())
})


def find_gamepads():
    """Return up to two distinct physical gamepads (skip sensors/audio).

    A single controller can expose several evdev nodes; group by physical
    identity (uniq/phys/name) and keep one node per controller so one pad
    never gets treated as two players.
    """
    EXCLUDE = ("imu", "motion", "sensor", "accel", "gyro",
               "hdmi", "jack", "audio", "sound")
    by_id = {}
    for path in sorted(evdev.list_devices()):
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
        if not has_btn:
            dev.close()
            continue
        ident = (dev.uniq or dev.phys or dev.name) or path
        if ident in by_id:
            dev.close()          # already have a node for this controller
            continue
        by_id[ident] = dev

    devices = list(by_id.values())
    # Assign player order stably by device path, cap at two players.
    devices.sort(key=lambda d: d.path)
    for extra in devices[2:]:
        extra.close()
    return devices[:2]


def main():
    gamepads = find_gamepads()
    if not gamepads:
        print("No gamepad found — controller-to-keys exiting", flush=True)
        sys.exit(0)

    try:
        ui = UInput({ecodes.EV_KEY: ALL_KEYS}, name="pitv-virtual-kb")
    except Exception as e:
        # Almost always /dev/uinput permission: run setup-input.sh once.
        print(f"ERROR: cannot open uinput ({e}).", flush=True)
        print("Run setup-input.sh once, then reboot, so the service user "
              "can write /dev/uinput.", flush=True)
        sys.exit(1)

    print(f"Mapping {len(gamepads)} controller(s):", flush=True)
    for i, dev in enumerate(gamepads):
        print(f"  player {i + 1}: {dev.name} ({dev.path})", flush=True)

    def press(key):
        ui.write(ecodes.EV_KEY, key, 1)
        ui.syn()
        time.sleep(0.03)
        ui.write(ecodes.EV_KEY, key, 0)
        ui.syn()

    # Per-player held-direction state, tracked separately for the D-pad
    # (hat) and the analog stick so releasing one falls back to the other.
    state = {
        i: {
            "hat":    {"x": 0, "y": 0},
            "stick":  {"x": 0, "y": 0},
            "repeat": {"x": [0, 0.0], "y": [0, 0.0]},  # [direction, next_time]
        }
        for i in range(len(gamepads))
    }

    def effective(pi, axis):
        s = state[pi]
        return s["hat"][axis] if s["hat"][axis] != 0 else s["stick"][axis]

    def update_axis(pi, axis, now):
        """Reconcile the repeat timer for one axis of one player."""
        keymap = PLAYERS[pi][axis]
        want   = effective(pi, axis)
        rep    = state[pi]["repeat"][axis]
        if want == 0:
            rep[0] = 0
            return
        if want != rep[0]:
            press(keymap[want])          # new direction: fire immediately
            rep[0] = want
            rep[1] = now + INITIAL_DELAY
        elif now >= rep[1]:
            press(keymap[want])
            rep[1] = now + REPEAT_INTERVAL

    sel = selectors.DefaultSelector()
    dev_player = {}
    for i, dev in enumerate(gamepads):
        try:
            sel.register(dev, selectors.EVENT_READ)
            dev_player[dev.fileno()] = i
        except Exception:
            pass

    try:
        while True:
            for key, _ in sel.select(timeout=REPEAT_INTERVAL / 2):
                pi   = dev_player.get(key.fileobj.fileno(), 0)
                btns = PLAYERS[pi]["buttons"]
                try:
                    for event in key.fileobj.read():
                        if event.type == ecodes.EV_KEY and event.value == 1:
                            k = btns.get(event.code)
                            if k:
                                press(k)
                        elif event.type == ecodes.EV_ABS:
                            code, val = event.code, event.value
                            s = state[pi]
                            if code == ecodes.ABS_HAT0X:
                                s["hat"]["x"] = (val > 0) - (val < 0)
                            elif code == ecodes.ABS_HAT0Y:
                                s["hat"]["y"] = (val > 0) - (val < 0)
                            elif code in (ecodes.ABS_X, ecodes.ABS_RX):
                                s["stick"]["x"] = (1 if val > DEADZONE else
                                                   -1 if val < -DEADZONE else 0)
                            elif code in (ecodes.ABS_Y, ecodes.ABS_RY):
                                s["stick"]["y"] = (1 if val > DEADZONE else
                                                   -1 if val < -DEADZONE else 0)
                except OSError:
                    pass

            now = time.time()
            for pi in state:
                update_axis(pi, "x", now)
                update_axis(pi, "y", now)

    except (KeyboardInterrupt, OSError):
        pass
    finally:
        ui.close()
        for dev in gamepads:
            try: dev.close()
            except Exception: pass


if __name__ == "__main__":
    main()
