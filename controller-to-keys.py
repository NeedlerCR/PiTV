#!/usr/bin/env python3
"""
/opt/pitv/controller-to-keys.py

Converts up to two generic Bluetooth/USB gamepads into keyboard events
via uinput.  Launched by tv_menu.py before a game starts and terminated
when the game exits.

  Face buttons are consistent everywhere: the LEFT face button is
  select/confirm/fire, the BOTTOM face button is back/quit. On this
  controller the left button reports as BTN_EAST and the bottom as
  BTN_SOUTH. The left button emits BOTH Space and Enter so it works as
  "select" in menus (Enter) and as "fire/start" in games (Space); the top
  and right face buttons are intentionally left unmapped so nothing else
  acts as confirm.

  Player 1 (first pad)  -> arrow keys, plus:
        Left button   -> Space + Enter  (select / fire / start / confirm)
        Bottom button -> Esc            (back / quit)
        +             -> Enter          (menu select)
        -             -> Esc            (back)
  Player 2 (second pad) -> W A S D, plus F / G as action keys, so a second
        controller can drive player 2 in two-player games (e.g. vitetris).
        Left button F (action), bottom button G (secondary).

  Per-game tweaks (tv_menu passes the game's binary as argv[1]):
    nudoku    -> right button enters a number by pressing it that many times
                 (1 press = 1 … 9 presses = 9; moving the cursor resets);
                 top button = hint (fills one square); bottom (B) = remove.
    freesweep -> right button reveals the square; top button flags a mine.
    vitetris  -> on ONE controller in 2-player, the pad is SPLIT by control
                 surface so the two players can't fight over the same axis:
                   D-pad                   -> player 1 (arrows)
                   either analog stick     -> player 2 (WASD)
                   L shoulder / R shoulder -> P1 rotate / P2 rotate
                 (a pad with no real D-pad keeps the old split: left stick P1,
                 right stick P2 — there's nothing else to give player 1.)
                 Previously the D-pad and the left stick both drove player 1,
                 so whichever moved last won and the two players kept
                 overriding each other. If `screen remote` is on, player 2 is
                 the SSH keyboard instead, so the whole controller stays player
                 1 and no split happens.

Outside that split, both the D-pad AND the left analog stick steer player 1;
a held direction auto-repeats so blocks keep sliding while you hold left/right.

REQUIREMENTS
  sudo apt install python3-evdev
  Run setup-input.sh ONCE so the service user can read /dev/input/event*
  and write /dev/uinput (adds a udev rule + the 'input' group).  Without
  that, UInput() below fails with a permission error (logged, then exit),
  and no keys reach the game.

SDL2 games (chromium-bsu, lbreakout2, vitetris) also read the keyboard via
evdev on the console, so this mapper is started for them too.
"""

import subprocess
import sys
import time
import selectors
import evdev
from evdev import ecodes, UInput


def _remote_running():
    """True if `screen remote on` (remote.py) is active — used to decide, for
    2-player Tetris, whether player 2 is the SSH keyboard (remote on) or the
    controller's analog sticks (remote off)."""
    try:
        return subprocess.run(["pgrep", "-f", "remote.py"],
                              capture_output=True).returncode == 0
    except Exception:
        return False

# Analog dead-zone.  Sticks report roughly -32768..32767; treat anything
# past this as a firm direction and ignore the slack near centre.
DEADZONE = 20000


def _dir(v):
    """Analog value → -1 / 0 / +1 with the dead-zone applied."""
    return 1 if v > DEADZONE else -1 if v < -DEADZONE else 0

# Auto-repeat timing for a held direction (D-pad or stick).
INITIAL_DELAY   = 0.25   # wait before the first repeat
REPEAT_INTERVAL = 0.11   # then repeat this often

# Per-player key maps.  Index 0 = player 1, index 1 = player 2.
PLAYERS = [
    {   # ── Player 1: arrows + primary action keys ──
        "x": {-1: ecodes.KEY_LEFT, 1: ecodes.KEY_RIGHT},
        "y": {-1: ecodes.KEY_UP,   1: ecodes.KEY_DOWN},
        "buttons": {
            # Left face button = the one and only select/confirm/fire button.
            # Emits Space AND Enter so it selects menus and fires in games.
            ecodes.BTN_EAST:   (ecodes.KEY_SPACE, ecodes.KEY_ENTER),  # left  -> select / fire
            ecodes.BTN_SOUTH:  ecodes.KEY_ESC,     # bottom -> back / quit
            # Top (BTN_NORTH) and right (BTN_WEST) deliberately unmapped so
            # nothing but the left button acts as confirm.
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
# A button value may be a single keycode or a tuple of keycodes (e.g. the
# left button emits Space+Enter), so flatten those out.
def _flatten(vals):
    for v in vals:
        if isinstance(v, (list, tuple)):
            yield from v
        else:
            yield v

ALL_KEYS = sorted({
    k
    for p in PLAYERS
    for k in _flatten(
        list(p["x"].values()) + list(p["y"].values()) + list(p["buttons"].values())
    )
})


def _has_dpad(dev):
    """True if the pad reports a real D-pad (ABS_HAT0X). A few pads route the
    D-pad through ABS_X/ABS_Y instead — the Tetris split needs to know, because
    on those there is no D-pad to hand player 1 separately from the stick."""
    try:
        abs_caps = dev.capabilities().get(ecodes.EV_ABS, [])
        return any((c[0] if isinstance(c, tuple) else c) == ecodes.ABS_HAT0X
                   for c in abs_caps)
    except Exception:
        return False


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


# Games that need extra keys the generic map doesn't emit (number entry,
# hint/flag). tv_menu launches us with the game's binary as argv[1].
GAME = sys.argv[1] if len(sys.argv) > 1 else ""

# Digit keys are contiguous: KEY_1..KEY_9 == 2..10, so KEY_1 + (n-1) == KEY_n.
_EXTRA_KEYS = [ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3, ecodes.KEY_4,
               ecodes.KEY_5, ecodes.KEY_6, ecodes.KEY_7, ecodes.KEY_8,
               ecodes.KEY_9, ecodes.KEY_0, ecodes.KEY_H, ecodes.KEY_F,
               ecodes.KEY_X, ecodes.KEY_C, ecodes.KEY_SPACE,
               ecodes.KEY_ENTER, ecodes.KEY_LEFTSHIFT]


def main():
    gamepads = find_gamepads()
    if not gamepads:
        print("No gamepad found — controller-to-keys exiting", flush=True)
        sys.exit(0)

    try:
        ui = UInput({ecodes.EV_KEY: sorted(set(ALL_KEYS) | set(_EXTRA_KEYS))},
                    name="pitv-virtual-kb")
    except Exception as e:
        # Almost always /dev/uinput permission: run setup-input.sh once.
        print(f"ERROR: cannot open uinput ({e}).", flush=True)
        print("Run setup-input.sh once, then reboot, so the service user "
              "can write /dev/uinput.", flush=True)
        sys.exit(1)

    # 2-player Tetris on ONE controller. Split by control surface, not by
    # stick: the D-pad is player 1 and BOTH analog sticks are player 2 (WASD).
    # The old split (left stick = P1, right stick = P2) left the D-pad also
    # driving P1, so the D-pad and the left stick overrode each other — and on
    # pads that report their D-pad as ABS_X/ABS_Y there was no way to tell them
    # apart at all. Giving each player their own surface removes the clash.
    # When `screen remote` is on, player 2 is the SSH keyboard instead, so the
    # whole controller stays player 1 (D-pad and both sticks all steer P1).
    split_tetris = (GAME == "vitetris"
                    and len(gamepads) == 1
                    and not _remote_running())
    # Pads with no separate D-pad (it comes through as ABS_X/ABS_Y) can't do
    # the surface split, so they keep the old one: left stick P1, right stick P2.
    split_hat = split_tetris and _has_dpad(gamepads[0])

    print(f"Mapping {len(gamepads)} controller(s):", flush=True)
    for i, dev in enumerate(gamepads):
        print(f"  player {i + 1}: {dev.name} ({dev.path})", flush=True)
    if split_hat:
        print("  vitetris split: D-pad = P1 (arrows, L shoulder rotates), "
              "sticks = P2 (WASD, R shoulder rotates)", flush=True)
    elif split_tetris:
        print("  vitetris split: no D-pad on this pad — left stick = P1, "
              "right stick = P2 (WASD)", flush=True)

    def press(key):
        ui.write(ecodes.EV_KEY, key, 1)
        ui.syn()
        time.sleep(0.03)
        ui.write(ecodes.EV_KEY, key, 0)
        ui.syn()

    def press_shift(key):
        ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1); ui.syn()
        press(key)
        ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0); ui.syn()

    # Per-player held-direction state, tracked separately for the D-pad
    # (hat) and the analog stick so releasing one falls back to the other.
    # "digit" tracks Sudoku number entry (see game_button).
    state = {
        i: {
            "hat":    {"x": 0, "y": 0},
            "stick":  {"x": 0, "y": 0},
            "repeat": {"x": [0, 0.0], "y": [0, 0.0]},  # [direction, next_time]
            "rstick": {"x": 0, "y": 0},                 # right stick
            "rrepeat":{"x": [0, 0.0], "y": [0, 0.0]},
            "digit":  0,
        }
        for i in range(len(gamepads))
    }

    def game_button(pi, code):
        """Per-game face-button behaviour a plain pad can't otherwise do.
        Returns True if it handled the button. Player 1 only; right face
        button = BTN_WEST, top = BTN_NORTH on this controller."""
        if pi != 0:
            return False
        if GAME == "nudoku":
            # Right button: enter a number by pressing it N times → N. nudoku
            # overwrites the cell with each digit, so sending the running count
            # each press lands on the count. Moving the cursor resets it.
            if code == ecodes.BTN_WEST:
                s = state[0]
                s["digit"] = s["digit"] % 9 + 1
                press(ecodes.KEY_1 + s["digit"] - 1)
                return True
            if code == ecodes.BTN_NORTH:          # top → hint (fill one square)
                press_shift(ecodes.KEY_H)
                return True
            if code == ecodes.BTN_SOUTH:          # bottom (B) → remove the number
                press(ecodes.KEY_X)
                return True
        elif GAME == "freesweep":
            if code == ecodes.BTN_WEST:           # right → reveal the square
                press(ecodes.KEY_SPACE)
                return True
            if code == ecodes.BTN_NORTH:          # top → flag/unflag a mine
                press(ecodes.KEY_F)
                return True
        return False

    def split_button(pi, code):
        """Tetris split only: give each player a rotate button of their own, so
        neither has to flick a direction up to turn a piece. Returns True if it
        handled the button."""
        if not split_tetris or pi != 0:
            return False
        if code in (ecodes.BTN_TL, ecodes.BTN_TL2):     # L shoulder -> P1 rotate
            press(ecodes.KEY_UP)
            return True
        if code in (ecodes.BTN_TR, ecodes.BTN_TR2):     # R shoulder -> P2 rotate
            press(ecodes.KEY_W)
            return True
        return False

    def effective(pi, axis):
        """Player 1's direction on one axis. Normally the D-pad wins and the
        left stick is the fallback; in the Tetris split the D-pad is all of
        player 1, because the sticks belong to player 2."""
        s = state[pi]
        if split_tetris and pi == 0:
            return s["hat"][axis] if split_hat else s["stick"][axis]
        return s["hat"][axis] if s["hat"][axis] != 0 else s["stick"][axis]

    def p2_direction(pi, axis):
        """Player 2's direction in the Tetris split: either stick, whichever is
        pushed (left stick wins a tie), so it doesn't matter which one the
        second player grabs. Without a D-pad it's the right stick alone, since
        the left one is player 1."""
        s = state[pi]
        if not split_hat:
            return s["rstick"][axis]
        return s["stick"][axis] if s["stick"][axis] != 0 else s["rstick"][axis]

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

    def update_raxis(pi, axis, now):
        """Same as update_axis but for the analog sticks driving player 2's
        WASD keys (Tetris split mode). Auto-repeats a held direction too."""
        keymap = PLAYERS[1][axis]            # player-2 key map (WASD)
        want   = p2_direction(pi, axis)
        rep    = state[pi]["rrepeat"][axis]
        if want == 0:
            rep[0] = 0
            return
        if want != rep[0]:
            press(keymap[want])
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
                            if split_button(pi, event.code):
                                continue
                            if game_button(pi, event.code):
                                continue
                            k = btns.get(event.code)
                            if k is not None:
                                if isinstance(k, (list, tuple)):
                                    for kk in k:
                                        press(kk)
                                else:
                                    press(k)
                        elif event.type == ecodes.EV_ABS:
                            code, val = event.code, event.value
                            s = state[pi]
                            if code == ecodes.ABS_HAT0X:
                                s["hat"]["x"] = (val > 0) - (val < 0)
                            elif code == ecodes.ABS_HAT0Y:
                                s["hat"]["y"] = (val > 0) - (val < 0)
                            elif code == ecodes.ABS_X:
                                s["stick"]["x"] = _dir(val)
                            elif code == ecodes.ABS_Y:
                                s["stick"]["y"] = _dir(val)
                            elif code == ecodes.ABS_RX:
                                # Right stick: tracked separately in Tetris
                                # split mode (where both sticks are player 2),
                                # otherwise it mirrors the left stick (P1).
                                if split_tetris:
                                    s["rstick"]["x"] = _dir(val)
                                else:
                                    s["stick"]["x"] = _dir(val)
                            elif code == ecodes.ABS_RY:
                                if split_tetris:
                                    s["rstick"]["y"] = _dir(val)
                                else:
                                    s["stick"]["y"] = _dir(val)
                            # Moving the cursor resets Sudoku number entry, so
                            # the next cell starts counting from 1 again.
                            if effective(pi, "x") or effective(pi, "y"):
                                s["digit"] = 0
                except OSError:
                    pass

            now = time.time()
            for pi in state:
                update_axis(pi, "x", now)
                update_axis(pi, "y", now)
                if split_tetris:
                    update_raxis(pi, "x", now)
                    update_raxis(pi, "y", now)

    except (KeyboardInterrupt, OSError):
        pass
    finally:
        ui.close()
        for dev in gamepads:
            try: dev.close()
            except Exception: pass


if __name__ == "__main__":
    main()
