import curses
import json
import math
import os
import random
import re
import selectors
import shutil
import subprocess
import sys
import threading
import time

# ─────────────────────────────────────────────────────────────────────
# OPTIONAL DEPENDENCIES
# ─────────────────────────────────────────────────────────────────────

try:
    import evdev
    from evdev import ecodes, UInput
    EVDEV_OK = True
except ImportError:
    EVDEV_OK = False

# ChronosVer: vYYYY.MAJOR.MINOR.BUG
VERSION = "v2026.2.2.0"

# ─────────────────────────────────────────────────────────────────────
# LOG SYSTEM
# ─────────────────────────────────────────────────────────────────────

LOG_PATH          = "/tmp/pitv.log"
LOG_BUFFER: list  = []
LOG_MAX           = 200
log_visible       = False          # toggled by pressing Shift+F then 6
LOG_PANEL_LINES   = 3


def log(msg: str) -> None:
    entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG_BUFFER.append(entry)
    if len(LOG_BUFFER) > LOG_MAX:
        LOG_BUFFER.pop(0)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(entry + "\n")
    except OSError:
        pass


def log_clear() -> None:
    LOG_BUFFER.clear()
    try:
        open(LOG_PATH, "w").close()
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────
# PIN / LOCKOUT SYSTEM
# ─────────────────────────────────────────────────────────────────────
#
# Locked games are opened with a personal PIN. PINs are managed from the CLI
# (`screen pin assign|list|remove|rename|revoke`) and stored per-person in
# PIN_FILE, so the log records WHO opened each game. The emergency code is a
# master that always works.

EMERGENCY_CODE  = "159753"
FREE_GAMES      = set()          # every game requires a PIN
OTP_MAX_FAILS   = 3
PIN_FILE        = os.path.expanduser("~/.pitv/pins.json")


def load_pins() -> dict:
    """{name: 6-digit PIN}, read fresh each time so `screen pin` edits apply
    without restarting the menu."""
    try:
        with open(PIN_FILE) as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def verify_pin(code: str):
    """Return the player's name for a valid PIN, 'Emergency' for the master
    code, or None. (Free games never reach here.)"""
    if code == EMERGENCY_CODE:
        return "Emergency"
    for name, pin in load_pins().items():
        if code and code == pin:
            return name
    return None


def set_highscore_name(binary: str, who: str) -> None:
    """Make a game's high-score name the PIN owner's name instead of a stale
    default. Safe if the config is missing (does nothing). Handles lbreakouthd
    (Breakout), whose config is key=value with the active player on `player0=`."""
    name = "".join(c for c in who if c.isalnum())[:14] or "PLAYER"
    if binary in ("lbreakouthd", "lbreakout2"):
        cfg = os.path.expanduser("~/.lbreakouthd/lbreakouthd.conf")
        try:
            if not os.path.isfile(cfg):
                return
            out, changed = [], False
            for ln in open(cfg).read().splitlines():
                if ln.startswith("player0="):
                    out.append(f"player0={name}")
                    changed = True
                else:
                    out.append(ln)
            if changed:
                with open(cfg, "w") as f:
                    f.write("\n".join(out) + "\n")
                log(f"Breakout high-score name set to {name}")
        except Exception as e:
            log(f"set_highscore_name error: {e}")


# ─────────────────────────────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────────────────────────────

current_view        = "MENU"
selected_art_idx    = 0
selected_game_idx   = 0
selected_dur_idx    = 0
active_art_type     = None
art_stop_time       = None
input_queue: list   = []
FIFO_PATH           = "/tmp/tv_menu.fifo"
should_clear_screen = False

# Keypad (OTP entry)
keypad_row     = 0
keypad_col     = 0
keypad_entered = ""
keypad_error   = ""

# Pending game launch (waiting for OTP)
pending_game_key = ""
pending_game_cmd: list = []

# Tic-Tac-Toe mode selection
ttt_sel   = 0          # 0 = 1 player, 1 = 2 player
ttt_mode  = "2P"       # "1P" (vs computer) or "2P"
ttt_stage = 1          # which player's PIN we're collecting (2P)
ttt_p1    = ""         # player 1's name (2P)

# Lockout
otp_fail_count  = 0
system_locked   = False
lock_entered    = ""
lock_error      = ""
lock_fail_count = 0        # wrong tries on the lockout screen
hard_locked     = False    # escalated: emergency code failed 3x → full lockdown

# Controller scan trigger
_controller_scan_event = threading.Event()
_controller_scan_event.set()   # scan immediately on startup

# Controller joystick debounce
_last_joy_time: float = 0.0
_JOY_COOLDOWN         = 0.25

# How the controller listener should interpret input right now:
#   "MENU"          → D-pad/stick drive menu navigation (LEFT=back, RIGHT=select)
#   "GAME_EXTERNAL" → a subprocess game is running; movement is delivered by
#                     controller-to-keys.py (uinput), so suppress everything
#                     here except HOME so we don't inject BACK and kill the game
#   "GAME_INTERNAL" → the built-in snake is running; feed raw directions
#                     (UP/DOWN/LEFT/RIGHT) straight into the input queue
#   "KEYPAD"        → an OTP / lock keypad is on screen; D-pad and sticks
#                     navigate the grid (raw L/R/U/D), A=press, B=delete/back
controller_mode = "MENU"

KEYPAD_LAYOUT = [
    ["1", "2", "3"],
    ["4", "5", "6"],
    ["7", "8", "9"],
    ["DEL", "0", "OK"],
]

DURATION_OPTIONS = [
    ("Indefinite", None),
    ("30 Seconds", 30),
    ("1 Minute",   60),
    ("5 Minutes",  300),
]

# Only fire remains as an external command; matrix is internal curses.
# CACA_DELAY=150000 µs → ~7 fps, much calmer on a TV than flat-out speed.
EXTERNAL_CMDS = {
    "FIRE": (["cacafire"], {"CACA_GEOMETRY": "80x40", "CACA_DELAY": "150000"}),
}

GAME_KEYS = {
    "SNAKE":       ["snake"],
    "TETRIS":      ["vitetris"],         # normal Tetris; its menu also has 2-player
    "INVADERS":    ["ninvaders"],
    "BREAKOUT":    ["lbreakouthd"],
    "SHOOTER":     ["chromium-bsu"],
    # ── added games (all PIN-locked like the rest) ──
    "MOONBUGGY":   ["moon-buggy"],       # retro jump-the-craters, high score
    "TYRIAN":      ["opentyrian"],       # classic vertical shmup, high score
    "SUPERTUX":    ["supertux2"],        # modern platformer (Mario-like)
    "FROZENBUBBLE":["frozen-bubble"],    # bubble-pop puzzle, 1-2 player, high score
    "BOULDERDASH": ["phear"],            # Boulder Dash clone (pkg cavezofphear)
    "2048":        ["2048"],             # modern slide-and-add number puzzle
    "SUDOKU":      ["nudoku"],           # ncurses Sudoku
    "MINESWEEPER": ["freesweep"],        # ncurses Minesweeper
    "CURSEOFWAR":  ["curseofwar"],       # fast ncurses real-time strategy vs AI
    "NETHACK":     ["nethack"],          # legendary roguelike (pkg nethack-console)
    "CRAWL":       ["crawl"],            # Dungeon Crawl Stone Soup roguelike
    "DOPEWARS":    ["dopewars"],         # buy-low/sell-high trading game, high score
}

# (label, cmd_list, game_key, is_2player)
GAME_OPTIONS = [
    # ── arcade / action ──
    ("SNAKE",             ["snake"],          "SNAKE",        False),  # built-in
    ("TETRIS",            ["vitetris"],       "TETRIS",       True),
    ("SPACE INVADERS",    ["ninvaders"],      "INVADERS",     False),
    ("BREAKOUT",          ["lbreakouthd"],    "BREAKOUT",     False),
    ("SPACE SHOOTER",     ["chromium-bsu"],   "SHOOTER",      False),
    ("TYRIAN",            ["opentyrian"],     "TYRIAN",       False),
    ("MOON BUGGY",        ["moon-buggy"],     "MOONBUGGY",    False),
    ("SUPER TUX",         ["supertux2"],      "SUPERTUX",     False),
    ("FROZEN BUBBLE",     ["frozen-bubble"],  "FROZENBUBBLE", True),
    ("BOULDER DASH",      ["phear"],          "BOULDERDASH",  False),
    # ── puzzle / strategy ──
    ("2048",              ["2048"],           "2048",         False),
    ("SUDOKU",            ["nudoku"],         "SUDOKU",       False),
    ("MINESWEEPER",       ["freesweep"],      "MINESWEEPER",  False),
    ("CURSE OF WAR",      ["curseofwar"],     "CURSEOFWAR",   False),
    ("NOUGHTS & CROSSES", ["noughts"],        "TICTACTOE",    True),   # built-in
    # ── adventure / RPG (keyboard recommended) ──
    ("NETHACK",           ["nethack"],        "NETHACK",      False),
    ("DUNGEON CRAWL",     ["crawl"],          "CRAWL",        False),
    ("DOPE WARS",         ["dopewars"],       "DOPEWARS",     False),
]

# Debian's bsdgames/bastet/etc packages install into /usr/games, but the
# systemd service's default PATH (/usr/local/bin:/usr/bin:/bin) doesn't
# include it — shutil.which() alone will report every game as missing.
# Search these directories explicitly and cache the resolved absolute path.
_GAME_SEARCH_DIRS = [
    "/usr/games", "/usr/local/games",
    "/usr/bin", "/usr/local/bin", "/bin",
]
_binary_path_cache: dict = {}


def resolve_binary(name: str) -> str | None:
    """Return the absolute path to `name`, checking /usr/games etc, or None."""
    if name in _binary_path_cache:
        return _binary_path_cache[name]
    # PATH-based lookup first (fast path if PATH happens to be set right)
    found = shutil.which(name)
    if not found:
        for d in _GAME_SEARCH_DIRS:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    _binary_path_cache[name] = found
    return found

ART_OPTIONS = [
    ("FIRE",          "FIRE",   "FIRE"),
    ("CLOCK + STARS", None,     "CLOCK"),
    ("MATRIX RAIN",   None,     "MATRIX"),
    ("SCREEN MIRROR", "MIRROR", "MIRROR"),
    ("GAMES",         "GAMES",  "GAMES"),
]


# ─────────────────────────────────────────────────────────────────────
# CONTROLLER  (generic Bluetooth / USB gamepad via evdev)
# ─────────────────────────────────────────────────────────────────────

def _handle_controller_event(event) -> None:
    global _last_joy_time
    if not EVDEV_OK:
        return

    mode = controller_mode

    if event.type == ecodes.EV_KEY and event.value == 1:   # key-down only
        c = event.code

        # HOME button always returns to the main menu / exits a running game.
        if c == ecodes.BTN_MODE:
            input_queue.append("HOME")
            return

        if mode == "GAME_EXTERNAL":
            # Movement is injected by controller-to-keys.py as real key events.
            # If we also queued BACK/SELECT here the game would exit or the
            # inputs would double-fire, so ignore everything but HOME (above).
            return

        if mode in ("GAME_INTERNAL", "KEYPAD"):
            # Both want raw D-pad directions; only the face buttons differ.
            if c == ecodes.BTN_DPAD_UP:      input_queue.append("UP")
            elif c == ecodes.BTN_DPAD_DOWN:  input_queue.append("DOWN")
            elif c == ecodes.BTN_DPAD_LEFT:  input_queue.append("LEFT")
            elif c == ecodes.BTN_DPAD_RIGHT: input_queue.append("RIGHT")
            elif c == ecodes.BTN_EAST:       # Nintendo A → select/confirm
                # Keypad: press the highlighted key. Game (e.g. N+C): place mark.
                input_queue.append("SELECT")
            elif c == ecodes.BTN_SOUTH:      # Nintendo B → back (keypad: delete/exit)
                input_queue.append("BACK")
            elif c in (ecodes.BTN_THUMBL, ecodes.BTN_THUMBR):  # stick click → select
                input_queue.append("SELECT")
            elif c in (ecodes.BTN_TR, ecodes.BTN_TR2) and mode == "GAME_INTERNAL":
                input_queue.append("SPEED")   # R button: snake speeds up
            return

        # ── MENU navigation ──
        if c == ecodes.BTN_DPAD_UP:
            input_queue.append("UP")
        elif c == ecodes.BTN_DPAD_DOWN:
            input_queue.append("DOWN")
        elif c == ecodes.BTN_DPAD_LEFT:
            input_queue.append("BACK")       # D-pad LEFT  → back
        elif c == ecodes.BTN_DPAD_RIGHT:
            input_queue.append("SELECT")     # D-pad RIGHT → select

        elif c == ecodes.BTN_EAST:           # Nintendo A  → select
            input_queue.append("SELECT")
        elif c == ecodes.BTN_SOUTH:          # Nintendo B  → back
            input_queue.append("BACK")

        elif c in (ecodes.BTN_THUMBL, ecodes.BTN_THUMBR):  # stick click → select
            input_queue.append("SELECT")

        elif c == ecodes.BTN_START:          # + button → select
            input_queue.append("SELECT")

    elif event.type == ecodes.EV_ABS:
        # In GAME_EXTERNAL the mapper owns the sticks/D-pad entirely.
        if mode == "GAME_EXTERNAL":
            return

        game = mode in ("GAME_INTERNAL", "KEYPAD")   # raw L/R directions

        if event.code == ecodes.ABS_HAT0Y:
            if event.value == -1:
                input_queue.append("UP")
            elif event.value == 1:
                input_queue.append("DOWN")
        elif event.code == ecodes.ABS_HAT0X:
            if event.value == -1:
                input_queue.append("LEFT" if game else "BACK")   # hat LEFT
            elif event.value == 1:
                input_queue.append("RIGHT" if game else "SELECT") # hat RIGHT
        elif event.code in (ecodes.ABS_Y, ecodes.ABS_X):
            # Left joystick, debounced so a held stick doesn't flood the queue.
            now = time.time()
            if now - _last_joy_time < _JOY_COOLDOWN:
                return
            if event.code == ecodes.ABS_Y:
                if event.value < -8000:
                    input_queue.append("UP");   _last_joy_time = now
                elif event.value > 8000:
                    input_queue.append("DOWN"); _last_joy_time = now
            else:  # ABS_X — horizontal only steers a game (menus have no L/R nav)
                if not game:
                    return
                if event.value < -8000:
                    input_queue.append("LEFT");  _last_joy_time = now
                elif event.value > 8000:
                    input_queue.append("RIGHT"); _last_joy_time = now


def listen_controllers() -> None:
    """
    Scans for gamepads on startup, or whenever triggered by
    'screen controller' / SCAN_CONTROLLER (crontab handles periodic
    retriggering — this listener itself does not auto-retry).
    Rescans only when a monitored device actually disappears.
    IMU/audio/HDMI devices are excluded.
    """
    if not EVDEV_OK:
        log("evdev not installed — no controller support")
        return

    EXCLUDE = ("(imu)", "motion", "sensor", "accel", "gyro",
               "hdmi", "jack", "audio", "sound")
    INCLUDE = ("controller", "gamepad", "joystick", "joypad",
               "xbox", "playstation", "dualshock", "8bitdo")

    def _is_gamepad(dev) -> bool:
        name = dev.name.lower()
        if any(x in name for x in EXCLUDE):
            return False
        if any(x in name for x in INCLUDE):
            return True
        caps = dev.capabilities()
        if ecodes.EV_ABS in caps and ecodes.EV_KEY in caps:
            btns = {c for c in caps.get(ecodes.EV_KEY, [])
                    if isinstance(c, int) and c >= 304}
            if len(btns) >= 4:
                return True
        return False

    def _scan():
        found = {}
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                if _is_gamepad(dev):
                    found[path] = dev
                    log(f"Gamepad: {dev.name} at {path}")
                else:
                    dev.close()
            except Exception:
                pass
        return found

    log("Controller listener ready")

    while True:
        _controller_scan_event.wait()
        _controller_scan_event.clear()
        log("Scanning for controllers (60 s)…")

        deadline = time.time() + 60
        gamepads = {}
        while time.time() < deadline and not gamepads:
            gamepads = _scan()
            if not gamepads:
                time.sleep(3)

        if not gamepads:
            log("No controller found — waiting for next 'screen controller' trigger")
            continue

        log(f"Monitoring {len(gamepads)} controller(s)")
        try:
            sel       = selectors.DefaultSelector()
            monitored = set(gamepads.keys())
            for dev in gamepads.values():
                sel.register(dev, selectors.EVENT_READ)

            while True:
                ready = sel.select(timeout=1)

                # Only rescan if one of OUR devices disappeared
                current = set(evdev.list_devices())
                if not monitored.issubset(current):
                    log("Controller disconnected — rescanning")
                    sel.close()
                    for d in gamepads.values():
                        try: d.close()
                        except Exception: pass
                    _controller_scan_event.set()
                    break

                for key, _ in ready:
                    try:
                        for event in key.fileobj.read():
                            _handle_controller_event(event)
                    except Exception:
                        pass

        except Exception as e:
            log(f"Controller error: {e}")
            _controller_scan_event.set()
            time.sleep(1)


# ─────────────────────────────────────────────────────────────────────
# INPUT LISTENERS  (FIFO + CEC remote)
# ─────────────────────────────────────────────────────────────────────

def listen_fifo():
    if os.path.exists(FIFO_PATH):
        try: os.remove(FIFO_PATH)
        except Exception: pass
    try:
        os.mkfifo(FIFO_PATH)
        os.chmod(FIFO_PATH, 0o666)
    except Exception:
        pass

    while True:
        try:
            with open(FIFO_PATH, "r") as fifo:
                for line in fifo:
                    raw = line.strip()
                    cmd = raw.upper()

                    if cmd == "LOG_CLEAR":
                        log_clear()
                        continue
                    if cmd == "SCAN_CONTROLLER":
                        log("Controller scan triggered")
                        _controller_scan_event.set()
                        continue

                    # HomeKit (homebridge-pitv-tv) power control. Routed through
                    # here — not called by Homebridge directly — because this
                    # process owns the CEC bus, so cec-cmd.sh can free and reuse
                    # it regardless of which user Homebridge runs as.
                    if cmd in ("TV_ON", "TV_OFF"):
                        log(f"HomeKit CEC: {cmd}")
                        _run_cec_cmd("on 0" if cmd == "TV_ON" else "standby 0")
                        continue

                    # `screen unlock authorise <code>` clears a hard lockdown.
                    if cmd.startswith("UNLOCK "):
                        code = raw.split(" ", 1)[1].strip() if " " in raw else ""
                        if code == EMERGENCY_CODE and hard_locked:
                            _clear_hardlock()
                        else:
                            log("UNLOCK rejected")
                        continue

                    log(f"FIFO: {raw[:60]}")

                    if cmd in ("UP","DOWN","LEFT","RIGHT","SELECT","BACK","CLEAR","HOME"):
                        input_queue.append(cmd)
                    elif cmd.startswith("RUN "):
                        parts   = raw.split(" ", 3)
                        art_key = parts[1].upper() if len(parts) > 1 else ""
                        dur_str = parts[2].upper() if len(parts) > 2 else "INDEFINITE"
                        payload = parts[3]         if len(parts) > 3 else ""
                        try:
                            dur = None if dur_str == "INDEFINITE" else int(dur_str)
                        except ValueError:
                            dur = None
                        input_queue.append(("DIRECT_RUN", art_key, dur, payload))
        except Exception:
            time.sleep(0.2)


TV_STATE_FILE  = "/tmp/pitv-tv-state"
TV_INPUT_FILE  = "/tmp/pitv-tv-input"
tv_power_state = "unknown"
tv_input_phys  = None


def _set_tv_power(state):
    """Record the TV's power state and publish it to a file the Homebridge
    plugin reads, so powering the TV on/off with the PHYSICAL remote shows up
    in the Home app."""
    global tv_power_state
    if state != tv_power_state:
        tv_power_state = state
        log(f"TV power -> {state}")
    try:
        with open(TV_STATE_FILE, "w") as f:
            f.write(state)
    except OSError:
        pass


def _set_tv_input(phys):
    """Record the TV's active input (physical address like '10:00') from CEC
    Active-Source traffic, so the Home app's input selection tracks reality —
    otherwise re-picking the input HomeKit already thinks is active does
    nothing, and you have to toggle back and forth."""
    global tv_input_phys
    if phys != tv_input_phys:
        tv_input_phys = phys
        log(f"TV input -> {phys}")
    try:
        with open(TV_INPUT_FILE, "w") as f:
            f.write(phys)
    except OSError:
        pass


def listen_cec_remote():
    while True:
        try:
            proc = subprocess.Popen(
                ["cec-client", "-d", "8"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )

            def _poll_power(p):
                # Ask the TV its power status every few seconds so a change made
                # with the physical remote gets noticed.
                while p.poll() is None:
                    try:
                        p.stdin.write("pow 0\n"); p.stdin.flush()
                    except Exception:
                        return
                    time.sleep(5)
            threading.Thread(target=_poll_power, args=(proc,), daemon=True).start()

            for line in iter(proc.stdout.readline, ""):
                low = line.lower()
                if "key pressed:" in line:
                    key = line.split("key pressed:")[1].strip().split(" ")[0].lower()
                    log(f"CEC: {key}")
                    if   key == "up":                        input_queue.append("UP")
                    elif key == "down":                      input_queue.append("DOWN")
                    elif key == "left":                      input_queue.append("LEFT")
                    elif key == "right":                     input_queue.append("RIGHT")
                    elif key in ("select","enter"):          input_queue.append("SELECT")
                    elif key in ("exit","back","clear","return"): input_queue.append("BACK")
                # ── TV power state (from the pow poll or the TV's own reports) ──
                elif "power status:" in low:
                    if "standby" in low:   _set_tv_power("off")
                    elif "on" in low:      _set_tv_power("on")
                elif ":90:00" in low:      _set_tv_power("on")    # report power: on
                elif ":90:01" in low:      _set_tv_power("off")   # report power: standby
                else:
                    # Active Source (opcode 0x82) tells us the current input.
                    m = re.search(r":82:([0-9a-f]{2}:[0-9a-f]{2})", low)
                    if m:
                        _set_tv_input(m.group(1))
            proc.wait()
        except Exception:
            pass
        time.sleep(1)


CEC_UDP_PORT = 8129
# Whitelisted raw CEC frame, e.g. "tx 4f:82:10:00" (used for input switching).
_CEC_TX_RE = re.compile(r"^tx([ ][0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2})*)+$")


def _run_cec_cmd(cmd_str):
    """Run cec-cmd.sh with its (chatty) output suppressed.

    cec-client prints 'opening a connection to the CEC adapter…' to stdout;
    if that inherited the menu tty it scribbled over the UI. Discarding it
    keeps the screen clean.
    """
    def _run():
        try:
            subprocess.run(["/opt/pitv/cec-cmd.sh", cmd_str],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        except Exception as e:
            log(f"cec-cmd.sh {cmd_str!r} error: {e}")
    threading.Thread(target=_run, daemon=True).start()


_kill_stop   = threading.Event()
_kill_thread = None


def _set_kill_switch(on):
    """HomeKit 'kill switch'. While on, repeatedly send CEC standby so the TV
    can't stay powered on; turning it off stops that. Re-sends every 15 s so a
    TV that gets switched on is forced back off within a few seconds."""
    global _kill_thread
    if on:
        if _kill_thread and _kill_thread.is_alive():
            return
        _kill_stop.clear()

        def _loop():
            while not _kill_stop.is_set():
                _run_cec_cmd("standby 0")
                _kill_stop.wait(7)

        _kill_thread = threading.Thread(target=_loop, daemon=True)
        _kill_thread.start()
        log("Kill switch ON — forcing TV to stay off")
    else:
        _kill_stop.set()
        log("Kill switch OFF")


_hardlock_stop = threading.Event()


def _start_hardlock():
    """Escalated lockdown (emergency code failed 3x): force the TV off every
    7 s and block the screen. Only `screen unlock authorise <code>` clears it."""
    global hard_locked
    if hard_locked:
        return
    hard_locked = True
    _hardlock_stop.clear()

    def _loop():
        # 15 s grace before enforcement kicks in (if authorised in time,
        # the TV is never touched).
        if _hardlock_stop.wait(15):
            return
        while not _hardlock_stop.is_set():
            _run_cec_cmd("standby 0")          # kill switch: force TV off
            _hardlock_stop.wait(7)
    threading.Thread(target=_loop, daemon=True).start()
    log("HARD LOCK engaged — screen blocked; TV forced off in 15s (then every 7s)")


def _clear_hardlock():
    """`screen unlock authorise <code>`: end the lockdown — kill switch off,
    TV on, input -> PiTV, back to the menu."""
    global hard_locked, system_locked, otp_fail_count, lock_fail_count
    global lock_entered, lock_error, current_view
    _hardlock_stop.set()
    _set_kill_switch(False)
    hard_locked    = False
    system_locked  = False
    otp_fail_count = lock_fail_count = 0
    lock_entered   = lock_error = ""
    _run_cec_cmd("on 0")
    _run_cec_cmd("tx 1f:82:20:00")             # switch to PiTV (HDMI 2)
    current_view = "MENU"
    log("Authorised — hard lock cleared, TV on, input PiTV")


_remote_ui       = None
_remote_ui_tried = False
_current_game    = None      # binary of the running external game (per-game keys)


def _remote_keymap():
    # Each token maps to one or more keycodes. SELECT and PLAY both send
    # Enter + Space so either the tap or play/pause confirms a menu AND fires /
    # starts (e.g. Space Invaders' "press SPACE to play").
    return {
        "UP":     [ecodes.KEY_UP],    "DOWN":  [ecodes.KEY_DOWN],
        "LEFT":   [ecodes.KEY_LEFT],  "RIGHT": [ecodes.KEY_RIGHT],
        "SELECT": [ecodes.KEY_ENTER, ecodes.KEY_SPACE],
        "PLAY":   [ecodes.KEY_ENTER, ecodes.KEY_SPACE],
        "BACK":   [ecodes.KEY_ESC],   "SPEED": [ecodes.KEY_R],
    }


def _remote_inject(token):
    """Inject keystrokes so the Apple remote can drive EXTERNAL games (which
    read the console keyboard, not input_queue). Lazily opens a uinput device;
    if that isn't permitted it silently no-ops (menu/built-in games still
    work)."""
    global _remote_ui, _remote_ui_tried
    if not EVDEV_OK:
        return
    keys = _remote_keymap().get(token)
    if not keys:
        return
    if _remote_ui is None and not _remote_ui_tried:
        _remote_ui_tried = True
        try:
            allk = sorted({k for v in _remote_keymap().values() for k in v}
                          | {ecodes.KEY_SPACE})
            _remote_ui = UInput({ecodes.EV_KEY: allk}, name="pitv-remote-kb")
        except Exception as e:
            log(f"Remote uinput unavailable: {e}")
    if _remote_ui:
        for kc in keys:
            try:
                _remote_ui.write(ecodes.EV_KEY, kc, 1); _remote_ui.syn()
                time.sleep(0.02)
                _remote_ui.write(ecodes.EV_KEY, kc, 0); _remote_ui.syn()
            except Exception as e:
                log(f"Remote inject error: {e}")


def listen_cec_udp():
    """Control channel from homebridge-pitv-tv over localhost UDP.

    Homebridge is sandboxed and can't write our /tmp FIFO, but it can always
    send a localhost datagram. Accepted messages:
      TV_ON / TV_OFF     -> CEC power on / standby (this process owns the bus)
      KILL_ON / KILL_OFF -> kill switch (keep the TV forced off)
      CEC tx <frame>     -> raw CEC frame, e.g. input switching (whitelisted)
      KEY <TOKEN>        -> Apple Home / Control Centre remote. In the menu and
                            built-in games it feeds input_queue; during an
                            external game it's injected as a real keystroke via
                            uinput. Tokens: UP/DOWN/LEFT/RIGHT/SELECT/PLAY/BACK/
                            HOME/SPEED.
    """
    import socket
    MENU_NAV = {"UP", "DOWN", "LEFT", "RIGHT", "SELECT", "BACK", "SPEED"}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", CEC_UDP_PORT))
    except Exception as e:
        log(f"Control UDP listener bind failed: {e}")
        return
    log(f"Control UDP listener ready on 127.0.0.1:{CEC_UDP_PORT}")
    while True:
        try:
            data, _ = sock.recvfrom(128)
            msg = data.decode("utf-8", "ignore").strip()
            up  = msg.upper()
            if up in ("TV_ON", "TV_OFF"):
                log(f"HomeKit CEC: {up}")
                _set_tv_power("on" if up == "TV_ON" else "off")
                _run_cec_cmd("on 0" if up == "TV_ON" else "standby 0")
            elif up in ("KILL_ON", "KILL_OFF"):
                _set_kill_switch(up == "KILL_ON")
            elif up in ("GUEST_ON", "GUEST_OFF"):
                # Gate the guest web portal (it reads this file).
                state = "on" if up == "GUEST_ON" else "off"
                log(f"Guest mode -> {state}")
                try:
                    with open("/tmp/pitv-guest-mode", "w") as f:
                        f.write(state)
                except OSError:
                    pass
            elif up.startswith("KEY "):
                tok = up[4:].strip()
                if tok == "HOME":
                    input_queue.append("HOME")            # quit game / to menu
                elif controller_mode == "GAME_EXTERNAL":
                    _remote_inject(tok)                   # real keys into the game
                else:
                    if tok == "PLAY":
                        q = "SPEED"          # play/pause speeds up Snake
                    elif tok == "ENTER":
                        q = "SELECT"
                    else:
                        q = tok
                    if q in MENU_NAV:
                        input_queue.append(q)             # menu + built-in games
            elif msg.startswith("CEC "):
                frame = msg[4:].strip()
                if _CEC_TX_RE.match(frame):
                    log(f"HomeKit CEC frame: {frame}")
                    _run_cec_cmd(frame)
                else:
                    log(f"CEC UDP: rejected {frame!r}")
        except Exception as e:
            log(f"CEC UDP error: {e}")
            time.sleep(0.5)


threading.Thread(target=listen_fifo,        daemon=True).start()
threading.Thread(target=listen_cec_remote,  daemon=True).start()
threading.Thread(target=listen_controllers, daemon=True).start()
threading.Thread(target=listen_cec_udp,     daemon=True).start()

log("PiTV started")


# ─────────────────────────────────────────────────────────────────────
# RENDERER STATE RESET
# ─────────────────────────────────────────────────────────────────────

def reset_renderer_state():
    for fn in (render_starfield, render_matrix):
        for attr in ("stars","grid","cell_char","drops","grid_size"):
            if hasattr(fn, attr):
                delattr(fn, attr)


# ─────────────────────────────────────────────────────────────────────
# SUBPROCESS RUNNERS
# ─────────────────────────────────────────────────────────────────────

def _reset_terminal():
    try:
        subprocess.run(["stty", "sane"], check=False, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def show_message(stdscr, lines: list, color_pair: int = 4, duration: float = 3.0):
    """Briefly show a centred message block on screen."""
    max_y, max_x = stdscr.getmaxyx()
    stdscr.erase()
    start_y = max(0, (max_y - len(lines)) // 2)
    for i, line in enumerate(lines):
        x = max(0, (max_x - len(line)) // 2)
        try:
            stdscr.addstr(start_y + i, x, line, curses.color_pair(color_pair) | curses.A_BOLD)
        except curses.error:
            pass
    stdscr.refresh()
    time.sleep(duration)


def run_external_art(stdscr, art_entry, duration):
    """
    Launch an external art program.  A daemon monitor thread kills it the
    moment BACK/SELECT/CLEAR/HOME arrives so the exit is always responsive
    even when the subprocess is pegging the CPU.
    """
    cmd_list, env_extras = art_entry
    env = {**os.environ, **env_extras}
    log(f"Art start: {cmd_list[0]}")
    curses.def_prog_mode()
    curses.endwin()
    _reset_terminal()

    try:
        proc = subprocess.Popen(cmd_list, env=env)
    except FileNotFoundError:
        log(f"ERROR: {cmd_list[0]} not found")
        curses.reset_prog_mode(); curses.curs_set(0)
        return

    start      = time.time()
    stop_event = threading.Event()

    def _monitor():
        while not stop_event.is_set():
            if duration and (time.time() - start >= duration):
                proc.terminate(); return
            if input_queue:
                peek = input_queue[0]
                if peek in ("BACK","SELECT","CLEAR","HOME") or (
                        isinstance(peek, tuple) and peek[0] == "DIRECT_RUN"):
                    proc.terminate(); return
            if proc.poll() is not None:
                return
            time.sleep(0.05)

    monitor = threading.Thread(target=_monitor, daemon=True)
    monitor.start()
    proc.wait()
    stop_event.set()
    monitor.join(timeout=1)
    if proc.poll() is None:
        proc.kill()

    log(f"Art stop: {cmd_list[0]}")
    curses.reset_prog_mode(); curses.curs_set(0)


def run_game(stdscr, cmd_list: list):
    """
    Launch a game binary.

    - Checks the binary exists first and shows an error on-screen if not.
    - Only injects SDL env for SDL2 games.
    - Starts the controller-to-keys mapper for ncurses games.
    - Exits on BACK or HOME from the FIFO/controller.
    """
    global controller_mode, _current_game
    binary = cmd_list[0]

    # ── Pre-flight: is the binary installed? (checks /usr/games too) ──
    resolved = resolve_binary(binary)
    if not resolved:
        pkg_hint = {
            "snake":          "bsdgames",
            "ninvaders":      "ninvaders",
            "lbreakouthd":    "lbreakouthd",
            "chromium-bsu":   "chromium-bsu",
            "vitetris":       "vitetris",
            # added games whose apt package name differs from the binary
            "phear":          "cavezofphear",
            "nethack":        "nethack-console",
            "supertux2":      "supertux",
            # added games where package == binary (listed for a clear message)
            "moon-buggy":     "moon-buggy",
            "opentyrian":     "opentyrian",
            "frozen-bubble":  "frozen-bubble",
            "2048":           "2048",
            "nudoku":         "nudoku",
            "freesweep":      "freesweep",
            "curseofwar":     "curseofwar",
            "crawl":          "crawl",
            "dopewars":       "dopewars",
        }.get(binary, binary)
        msg = [
            "GAME NOT INSTALLED",
            "",
            f"'{binary}' was not found (checked /usr/games, /usr/bin, etc).",
            "",
            f"Install:  sudo apt install -y {pkg_hint}",
            "",
            "Returning to menu in 4 seconds…",
        ]
        log(f"Game not found: {binary} — install with: sudo apt install -y {pkg_hint}")
        show_message(stdscr, msg, color_pair=1, duration=4.0)
        return

    # Replace the bare name with its resolved absolute path for Popen
    cmd_list = [resolved] + cmd_list[1:]

    log(f"Game start: {binary} -> {resolved}")
    game_t0 = time.time()
    curses.def_prog_mode()
    curses.endwin()
    _reset_terminal()
    os.system("clear")

    # SDL games need the kmsdrm video driver on the bare framebuffer console.
    # (Harmless for the SDL 1.2 ones — they ignore it — but correct for SDL2.)
    SDL_GAMES = {"chromium-bsu", "lbreakouthd",
                 "opentyrian", "supertux2", "frozen-bubble"}
    is_sdl    = binary in SDL_GAMES

    # Controller-to-keys mapper. SDL games on the console also read the
    # keyboard via evdev and their native controller support is unreliable,
    # so we run the mapper for EVERY game. Its output goes to a log (not
    # DEVNULL) so a /dev/uinput permission error — the usual reason controls
    # don't work — is visible instead of silently swallowed.
    mapper_proc = None
    mapper_log  = None
    if EVDEV_OK:
        mapper = "/opt/pitv/controller-to-keys.py"
        if os.path.exists(mapper):
            try:
                mapper_log  = open("/tmp/pitv-mapper.log", "w")
                mapper_proc = subprocess.Popen(
                    ["python3", mapper, binary],   # binary → per-game key tweaks
                    stdout=mapper_log, stderr=subprocess.STDOUT,
                )
            except Exception as e:
                log(f"Mapper start failed: {e}")

    env = os.environ.copy()
    if is_sdl:
        env["SDL_VIDEODRIVER"] = "kmsdrm"
        env["SDL_AUDIODRIVER"]  = "alsa"

    # While the game runs the controller listener must stop injecting menu
    # commands — the mapper delivers movement as real key events instead.
    controller_mode = "GAME_EXTERNAL"
    _current_game   = binary
    game_log = None
    try:
        try:
            # Capture the game's stderr so a crash-on-launch (e.g. a game that
            # bails on this terminal) leaves a diagnosable trace in the log.
            game_log = open("/tmp/pitv-game.log", "w")
            proc = subprocess.Popen(cmd_list, env=env, stderr=game_log)
        except Exception as e:
            log(f"ERROR launching {binary}: {e}")
            _reset_terminal()
            if mapper_proc: mapper_proc.terminate()
            curses.reset_prog_mode(); curses.curs_set(0)
            show_message(stdscr, ["LAUNCH ERROR", "", str(e)], color_pair=1, duration=3.0)
            return

        while True:
            while input_queue:
                c = input_queue.pop(0)
                if c in ("BACK", "HOME"):
                    proc.terminate()
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.1)

        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired: proc.kill()

        if mapper_proc and mapper_proc.poll() is None:
            mapper_proc.terminate()
    finally:
        controller_mode = "MENU"
        _current_game   = None
        for f in (mapper_log, game_log):
            if f:
                try: f.close()
                except Exception: pass

    log(f"Game stop: {binary} rc={proc.returncode} after {time.time()-game_t0:.1f}s")
    curses.reset_prog_mode(); curses.curs_set(0)


def run_snake(stdscr):
    """
    Built-in snake game.

    Replaces bsdgames' `snake`, which has no speed control and runs far too
    fast to actually play on a TV.  Because it's pure curses and reads the
    shared input_queue directly, D-pad LEFT/RIGHT, the analog sticks, the
    keyboard arrows and the CEC remote all steer the snake the same way —
    no controller-to-keys mapper needed.
    """
    global controller_mode
    log("Snake: start (built-in)")
    controller_mode = "GAME_INTERNAL"
    input_queue.clear()

    curses.curs_set(0)
    stdscr.nodelay(True)

    DIRS   = {"UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)}
    quit_game = False
    apples    = 0            # food eaten
    speed_lvl = 0            # raised by the R button; never lowered
    final     = 0            # apples × speed bonus, computed at game over

    # Each logical cell is drawn 2 columns wide so the snake looks big and
    # chunky on a TV; blocks are solid (a reverse-video space in colour).
    CELL_W = 2

    # Speed model.  Seconds per step: starts gentle, and both the R button
    # (speed_lvl) and natural growth (apples) make it faster.  There is no
    # control that slows it back down.
    BASE_TICK   = 0.20
    MIN_TICK    = 0.06
    SPEED_STEP  = 0.020     # per R-button level
    GROW_STEP   = 0.003     # per apple
    MAX_SPEED   = 6         # R-button level cap

    def tick_for():
        return max(MIN_TICK, BASE_TICK - speed_lvl * SPEED_STEP - apples * GROW_STEP)

    def multiplier():
        # Small end-of-game bonus that grows with how fast you dared to go.
        return 1.0 + 0.15 * speed_lvl

    try:
        max_y, max_x = stdscr.getmaxyx()
        top, left    = 1, 1
        bottom       = max_y - 2          # leave a row for the status bar
        right        = max_x - 2
        gw           = (right - left + 1) // CELL_W   # logical columns
        gh           = bottom - top + 1               # logical rows
        if gw < 8 or gh < 6:
            show_message(stdscr, ["SCREEN TOO SMALL FOR SNAKE"],
                         color_pair=1, duration=2.0)
            return

        cx, cy      = gw // 2, gh // 2
        snake       = [(cx - 2, cy), (cx - 1, cy), (cx, cy)]   # tail … head
        direction   = (1, 0)                                   # committed heading
        pending_dir = direction                                # next turn to apply

        def place_food():
            while True:
                fx = random.randint(0, gw - 1)
                fy = random.randint(0, gh - 1)
                if (fx, fy) not in snake:
                    return (fx, fy)

        food      = place_food()
        last_step = time.time()

        BODY = curses.color_pair(2) | curses.A_REVERSE
        HEAD = curses.color_pair(2) | curses.A_REVERSE | curses.A_BOLD
        FOOD = curses.color_pair(1) | curses.A_REVERSE

        def draw_cell(lx, ly, attr):
            stdscr.addstr(top + ly, left + lx * CELL_W, " " * CELL_W, attr)

        while True:
            # ── Keyboard (fed into the same queue as controller/CEC) ──
            try:
                ch = stdscr.getch()
                if ch != -1:
                    if   ch == curses.KEY_UP:    input_queue.append("UP")
                    elif ch == curses.KEY_DOWN:  input_queue.append("DOWN")
                    elif ch == curses.KEY_LEFT:  input_queue.append("LEFT")
                    elif ch == curses.KEY_RIGHT: input_queue.append("RIGHT")
                    elif ch in (ord("r"), ord("R"), ord("+")):
                        input_queue.append("SPEED")
                    elif ch in (27, ord("q")):   input_queue.append("BACK")
            except curses.error:
                pass

            # ── Drain queued input. Validate every turn against the committed
            # heading (not against each other) so chaining several inputs
            # inside one tick can never fold the snake back on itself. ──
            while input_queue:
                cmd = input_queue.pop(0)
                if cmd in ("BACK", "HOME"):
                    quit_game = True
                    break
                if cmd == "SPEED":
                    speed_lvl = min(MAX_SPEED, speed_lvl + 1)   # only speeds up
                elif cmd in DIRS:
                    d = DIRS[cmd]
                    if d[0] != -direction[0] or d[1] != -direction[1]:  # no U-turn
                        pending_dir = d
            if quit_game:
                break

            # ── Advance on the tick (commit exactly one turn per step) ──
            now = time.time()
            if now - last_step >= tick_for():
                last_step = now
                direction = pending_dir
                hx, hy = snake[-1]
                nx, ny = hx + direction[0], hy + direction[1]
                if (nx < 0 or nx >= gw or ny < 0 or ny >= gh
                        or (nx, ny) in snake):
                    break                                   # crash → game over
                snake.append((nx, ny))
                if (nx, ny) == food:
                    apples += 1
                    food    = place_food()
                else:
                    snake.pop(0)

            # ── Render ──
            stdscr.erase()
            try:
                stdscr.attron(curses.color_pair(2))
                stdscr.border()
                stdscr.attroff(curses.color_pair(2))
                draw_cell(food[0], food[1], FOOD)
                for i, (sx, sy) in enumerate(snake):
                    draw_cell(sx, sy, HEAD if i == len(snake) - 1 else BODY)
            except curses.error:
                pass
            live = int(round(apples * multiplier()))
            status = (f" SNAKE   Score: {live}   Speed: {speed_lvl}/{MAX_SPEED}"
                      f" (x{multiplier():.2f})   R: faster   B/BACK: quit ")
            try:
                stdscr.addstr(max_y - 1, 0, status.center(max_x - 1),
                              curses.A_REVERSE | curses.A_DIM)
            except curses.error:
                pass
            stdscr.refresh()
            time.sleep(0.01)

        final = int(round(apples * multiplier()))
        if not quit_game:
            show_message(stdscr,
                         ["GAME OVER", "",
                          f"Apples: {apples}   Speed bonus: x{multiplier():.2f}",
                          f"Score: {final}", "",
                          "Returning to menu…"],
                         color_pair=1, duration=3.0)
    finally:
        controller_mode = "MENU"
        input_queue.clear()
        stdscr.clear()
        log(f"Snake: stop (apples {apples}, speed {speed_lvl}, score {final})")


TTT_LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]


def _ttt_wins(b, m):
    return any(b[a] == b[x] == b[y] == m for a, x, y in TTT_LINES)


def _ttt_ai(board, me, opp):
    """Pick a move: win if able, else block, else centre/corner/edge."""
    empties = [i for i in range(9) if not board[i]]
    for i in empties:
        b = board[:]; b[i] = me
        if _ttt_wins(b, me):
            return i
    for i in empties:
        b = board[:]; b[i] = opp
        if _ttt_wins(b, opp):
            return i
    for i in (4, 0, 2, 6, 8, 1, 3, 5, 7):
        if i in empties:
            return i
    return None


def run_noughts(stdscr, vs_computer=False):
    """Built-in Noughts & Crosses. Single-player (X = you, O = the computer)
    or 2-player hotseat (X then O).

    Like Snake it reads the shared input_queue, so the controller, the CEC
    remote AND the Apple Home / Control-Centre remote all drive it — no
    external binary, so nothing to install or crash.
    """
    global controller_mode
    log(f"Noughts & Crosses: start ({'1P vs CPU' if vs_computer else '2P'})")
    controller_mode = "GAME_INTERNAL"
    input_queue.clear()
    curses.curs_set(0)
    stdscr.nodelay(True)

    board  = [""] * 9
    cur    = 4
    turn   = "X"
    winner = None

    def check():
        for a, b, c in TTT_LINES:
            if board[a] and board[a] == board[b] == board[c]:
                return board[a]
        return "draw" if all(board) else None

    try:
        while True:
            # Computer's move (single-player: you are X, the computer is O).
            if vs_computer and turn == "O" and not winner:
                time.sleep(0.4)
                mv = _ttt_ai(board, "O", "X")
                if mv is not None:
                    board[mv] = "O"
                    winner = check()
                    if not winner:
                        turn = "X"

            try:
                ch = stdscr.getch()
                if ch != -1:
                    if   ch == curses.KEY_UP:     input_queue.append("UP")
                    elif ch == curses.KEY_DOWN:   input_queue.append("DOWN")
                    elif ch == curses.KEY_LEFT:   input_queue.append("LEFT")
                    elif ch == curses.KEY_RIGHT:  input_queue.append("RIGHT")
                    elif ch in (10, 13, ord(" ")): input_queue.append("SELECT")
                    elif ch in (27, ord("q")):    input_queue.append("BACK")
            except curses.error:
                pass

            quit_game = False
            while input_queue:
                cmd = input_queue.pop(0)
                if cmd in ("BACK", "HOME"):
                    quit_game = True
                    break
                if winner:
                    if cmd == "SELECT":          # play again
                        board = [""] * 9; cur = 4; turn = "X"; winner = None
                    continue
                # In single-player, only place on your (X) turn.
                if vs_computer and turn != "X":
                    continue
                r, c = cur // 3, cur % 3
                if   cmd == "UP"    and r > 0: cur -= 3
                elif cmd == "DOWN"  and r < 2: cur += 3
                elif cmd == "LEFT"  and c > 0: cur -= 1
                elif cmd == "RIGHT" and c < 2: cur += 1
                elif cmd == "SELECT" and not board[cur]:
                    board[cur] = turn
                    winner = check()
                    if not winner:
                        turn = "O" if turn == "X" else "X"
            if quit_game:
                break

            # ── Render ──
            stdscr.erase()
            max_y, max_x = stdscr.getmaxyx()
            oy, ox = max_y // 2 - 3, max_x // 2 - 5

            def put(y, x, s, a=0):
                try: stdscr.addstr(y, x, s, a)
                except curses.error: pass

            title = ("NOUGHTS & CROSSES  (vs Computer)" if vs_computer
                     else "NOUGHTS & CROSSES  (2 Player)")
            put(oy - 2, max(0, (max_x - len(title)) // 2), title,
                curses.color_pair(2) | curses.A_BOLD)
            for r in range(3):
                for c in range(3):
                    i = r * 3 + c
                    y, x = oy + r * 2, ox + c * 4
                    a = curses.A_BOLD
                    if i == cur and not winner:
                        a |= curses.A_REVERSE
                    if   board[i] == "X": a |= curses.color_pair(5)
                    elif board[i] == "O": a |= curses.color_pair(1)
                    put(y, x, f" {board[i] or ' '} ", a)
                    if c < 2: put(y, x + 3, "|")
                if r < 2:
                    put(oy + r * 2 + 1, ox, "-----------")

            if winner == "draw":
                msg = " Draw!   A/SELECT: play again    B/BACK: quit "
            elif winner:
                if vs_computer:
                    who = "You win!" if winner == "X" else "Computer wins!"
                else:
                    who = f"{winner} wins!"
                msg = f" {who}   A/SELECT: play again    B/BACK: quit "
            else:
                if vs_computer:
                    turn_txt = "Your turn (X)" if turn == "X" else "Computer…"
                else:
                    turn_txt = f"Turn: {turn}"
                msg = (f" {turn_txt}    D-pad/Stick: move    A/SELECT: place"
                       f"    B/BACK: quit ")
            put(max_y - 1, 0, msg.center(max_x - 1),
                curses.A_REVERSE | curses.A_DIM)
            stdscr.refresh()
            time.sleep(0.02)
    finally:
        controller_mode = "MENU"
        input_queue.clear()
        stdscr.clear()
        log("Noughts & Crosses: stop")


def run_mirror(stdscr):
    """
    AirPlay screen mirroring via uxplay.

    Modelled on run_game(): we must release the console (endwin) before
    starting, because only one program can drive the display device at a
    time. Previously the mirror screen kept a curses UI refreshing the
    framebuffer (fbcon) while uxplay's kmssink tried to take the same
    display — audio streamed but video never showed. Ending curses frees the
    framebuffer, and 'kmssink force-modesetting=true' lets kmssink set the
    display mode even though fbcon still owns the console.
    """
    UXPLAY_LOG = "/tmp/uxplay.log"

    # A phone mirrors in portrait; filling a 16:9 TV stretches it. Render the
    # stream into a centred, phone-shaped rectangle (black bars at the sides)
    # by giving kmssink a render-rectangle sized from the real display.
    def _screen_size():
        try:
            with open("/sys/class/graphics/fb0/virtual_size") as f:
                w, h = (int(v) for v in f.read().strip().split(","))
                if w > 0 and h > 0:
                    return w, h
        except Exception:
            pass
        return 1920, 1080

    sw, sh = _screen_size()
    pw = max(120, int(sh * 9 / 19.5))          # iPhone-ish portrait width
    px = max(0, (sw - pw) // 2)                 # centre it horizontally
    portrait_sink   = (f'kmssink force-modesetting=true '
                       f'render-rectangle=<{px},0,{pw},{sh}>')
    fullscreen_sink = "kmssink force-modesetting=true"
    log(f"Mirror: display {sw}x{sh}; portrait rect <{px},0,{pw},{sh}>")

    curses.def_prog_mode()
    curses.endwin()
    _reset_terminal()
    os.system("clear")

    if not resolve_binary("uxplay"):
        print("\nuxplay is not installed. Install it with:\n"
              "  sudo apt install uxplay gstreamer1.0-plugins-bad \\\n"
              "      gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly\n"
              "If it starts but the phone can't find it, enable mDNS:\n"
              "  sudo systemctl enable --now avahi-daemon\n", flush=True)
        time.sleep(6)
        curses.reset_prog_mode(); curses.curs_set(0)
        return

    print("AirPlay receiver 'PiTV' is ready.\n"
          "  iPhone/iPad/Mac: Control Centre -> Screen Mirroring -> PiTV\n"
          "  (phone and Pi must share the same Wi-Fi network)\n"
          "The phone appears centred at its own shape; the sides stay black.\n"
          "Press BACK / B / HOME to stop.\n", flush=True)

    try:
        logf = open(UXPLAY_LOG, "w")
    except Exception:
        logf = subprocess.DEVNULL

    def _launch(sink):
        return subprocess.Popen(
            ["uxplay", "-n", "PiTV", "-vs", sink, "-avdec"],
            stdout=logf, stderr=subprocess.STDOUT,
        )

    # Try the portrait sink first; if it dies quickly (bad render-rectangle on
    # this GStreamer build), fall back to the known-good fullscreen sink so the
    # mirror still works rather than leaving a black screen.
    stopped_by_user = False
    crashed         = False
    proc            = None
    for idx, sink in enumerate([portrait_sink, fullscreen_sink]):
        started = time.time()
        proc    = _launch(sink)
        while True:
            while input_queue:
                c = input_queue.pop(0)
                if c in ("BACK", "HOME"):
                    stopped_by_user = True
                    proc.terminate()
                    break
            if stopped_by_user or proc.poll() is not None:
                break
            time.sleep(0.1)

        if stopped_by_user:
            break
        # uxplay exited on its own.
        quick = (time.time() - started) < 6
        if idx == 0 and quick:
            log("Mirror: portrait sink failed fast — retrying fullscreen")
            continue
        crashed = (proc.returncode not in (0, -15))   # -15 = our SIGTERM
        break

    if proc is not None and proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=2)
        except subprocess.TimeoutExpired: proc.kill()

    if logf not in (None, subprocess.DEVNULL):
        try: logf.close()
        except Exception: pass

    if crashed:
        log("uxplay exited unexpectedly — see /tmp/uxplay.log")
        try:
            with open(UXPLAY_LOG) as f:
                tail = [ln.rstrip() for ln in f if ln.strip()][-8:]
        except Exception:
            tail = []
        print("\nScreen mirroring stopped unexpectedly. Last output:", flush=True)
        for ln in tail:
            print("  " + ln, flush=True)
        print("\nIf video never appeared: try '-vs fbdevsink', or set "
              "gpu_mem=128 in /boot/firmware/config.txt.\n"
              "Full log: cat /tmp/uxplay.log\n", flush=True)
        time.sleep(6)

    log("Mirror: stopped")
    curses.reset_prog_mode(); curses.curs_set(0)


# ─────────────────────────────────────────────────────────────────────
# BUILT-IN RENDERERS
# ─────────────────────────────────────────────────────────────────────

def render_starfield(stdscr, frame):
    max_y, max_x = stdscr.getmaxyx()
    if not hasattr(render_starfield, "stars"):
        render_starfield.stars = [
            [random.uniform(-1,1), random.uniform(-1,1), random.uniform(0.1,1.0)]
            for _ in range(80)
        ]
    cx, cy = max_x // 2, max_y // 2
    for star in render_starfield.stars:
        star[2] -= 0.03
        if star[2] <= 0:
            star[0] = random.uniform(-1,1)
            star[1] = random.uniform(-1,1)
            star[2] = 1.0
        sx = int(cx + (star[0]/star[2])*(cx/1.5))
        sy = int(cy + (star[1]/star[2])*(cy/1.5))
        ch = "." if star[2]>0.6 else ("*" if star[2]>0.3 else "#")
        if 0<=sx<max_x and 0<=sy<max_y:
            try: stdscr.addch(sy, sx, ch, curses.color_pair(3))
            except curses.error: pass


def render_clock(stdscr, frame):
    max_y, max_x = stdscr.getmaxyx()
    time_str = time.strftime("%H:%M:%S")
    B = "\u2588"
    font = {
        "0":["  BBBBBBB  "," BBB   BBB ","BBB     BBB","BBB     BBB","BBB     BBB"," BBB   BBB ","  BBBBBBB  "],
        "1":["      BBB","    BBBBB","      BBB","      BBB","      BBB","      BBB","   BBBBBBB"],
        "2":["  BBBBBBB  "," BBB   BBB ","       BBB ","  BBBBBBB  "," BBB       "," BBB   BBB ","  BBBBBBB  "],
        "3":["  BBBBBBB  "," BBB   BBB ","       BBB ","   BBBBBB  ","       BBB "," BBB   BBB ","  BBBBBBB  "],
        "4":[" BBB   BBB "," BBB   BBB "," BBB   BBB ","  BBBBBBB  ","       BBB ","       BBB ","      BBB  "],
        "5":["  BBBBBBB  "," BBB       "," BBBBBBB   ","       BBB ","       BBB "," BBB   BBB ","  BBBBBBB  "],
        "6":["  BBBBBBB  "," BBB   BBB "," BBB       "," BBBBBBBBB "," BBB   BBB "," BBB   BBB ","  BBBBBBB  "],
        "7":[" BBBBBBBBBBB"," BBB   BBB ","       BBB ","      BBB  ","     BBB   ","    BBB    ","   BBB     "],
        "8":["  BBBBBBB  "," BBB   BBB "," BBB   BBB ","  BBBBBBB  "," BBB   BBB "," BBB   BBB ","  BBBBBBB  "],
        "9":["  BBBBBBB  "," BBB   BBB "," BBB   BBB ","  BBBBBBBBB","       BBB "," BBB   BBB ","  BBBBBBB  "],
        ":":["   "," B "," B ","   "," B "," B ","   "],
    }
    font = {k:[r.replace("B",B) for r in v] for k,v in font.items()}
    cw = 11
    # Compute actual rendered width from first row (colon is 3 wide, not 11)
    total_w = sum(len(font.get(ch,[" "*cw]*7)[0]) for ch in time_str) + (len(time_str)-1)
    sx = max(0, (max_x - total_w) // 2)
    sy = max(0, (max_y - 7)       // 2)
    for row in range(7):
        line = "".join(
            (" " if i>0 else "") + font.get(ch,[" "*cw]*7)[row]
            for i,ch in enumerate(time_str)
        )
        try:
            stdscr.addstr(sy+row, sx, line, curses.color_pair(4)|curses.A_BOLD)
        except curses.error:
            pass


def render_clock_stars(stdscr, frame):
    render_starfield(stdscr, frame)
    render_clock(stdscr, frame)


def render_matrix(stdscr, frame):
    """
    Matrix digital rain with 8-level intensity fade (~7-8 char trail).
    Updates every 2nd frame (~6 fps); cells at 0 are explicitly blanked.
    """
    if frame % 2 != 0:
        return

    max_y, max_x = stdscr.getmaxyx()
    CHARSET = "0123456789ABCDEF@#$%&*"
    # 8 intensity levels: 1=dimmest tail, 8=bright white head
    IATTR = [
        curses.color_pair(2)|curses.A_DIM,   # 1
        curses.color_pair(2)|curses.A_DIM,   # 2
        curses.color_pair(2),                 # 3
        curses.color_pair(2),                 # 4
        curses.color_pair(2)|curses.A_BOLD,  # 5
        curses.color_pair(2)|curses.A_BOLD,  # 6
        curses.color_pair(4),                 # 7
        curses.color_pair(4)|curses.A_BOLD,  # 8 — white head
    ]
    cols = max_x // 2

    if (not hasattr(render_matrix,"grid")
            or render_matrix.grid_size != (max_y, cols)):
        render_matrix.grid      = [[0]*cols for _ in range(max_y)]
        render_matrix.cell_char = [[" "]*cols for _ in range(max_y)]
        render_matrix.drops     = [random.randint(-max_y,0) for _ in range(cols)]
        render_matrix.grid_size = (max_y, cols)

    grid  = render_matrix.grid
    cchar = render_matrix.cell_char
    drops = render_matrix.drops

    # Fade all cells
    for r in range(max_y):
        for c in range(cols):
            if grid[r][c] > 0:
                grid[r][c] -= 1

    # Advance drops; stamp head at max intensity
    for c in range(cols):
        y = drops[c]
        if 0 <= y < max_y:
            grid[y][c]  = 8
            cchar[y][c] = random.choice(CHARSET)
        drops[c] += 1
        if drops[c] >= max_y or random.random() < 0.04:
            drops[c] = random.randint(-max_y//2, 0)

    # Render
    for r in range(max_y):
        for c in range(cols):
            x   = c * 2
            lvl = grid[r][c]
            try:
                if lvl > 0:
                    stdscr.addch(r, x, cchar[r][c], IATTR[lvl-1])
                else:
                    stdscr.addch(r, x, " ")
            except curses.error:
                pass


# Patch callables into ART_OPTIONS after function definitions
ART_OPTIONS[1] = ("CLOCK + STARS", render_clock_stars, "CLOCK")
ART_OPTIONS[2] = ("MATRIX RAIN",   render_matrix,      "MATRIX")


# ─────────────────────────────────────────────────────────────────────
# LOG PANEL
# ─────────────────────────────────────────────────────────────────────

def draw_log_panel(stdscr):
    if not log_visible:
        return
    max_y, max_x = stdscr.getmaxyx()
    sep_y = max_y - LOG_PANEL_LINES - 1
    if sep_y < 2:
        return
    try:
        stdscr.addstr(sep_y, 0, "-"*(max_x-1), curses.color_pair(3)|curses.A_DIM)
    except curses.error:
        pass
    for i, entry in enumerate(LOG_BUFFER[-(LOG_PANEL_LINES):]):
        y = sep_y + 1 + i
        if y < max_y:
            try:
                stdscr.addstr(y, 1, entry[:max_x-2], curses.color_pair(3)|curses.A_DIM)
            except curses.error:
                pass


# ─────────────────────────────────────────────────────────────────────
# MENU / VIEW RENDERERS
# ─────────────────────────────────────────────────────────────────────

_LOG_GUARD = LOG_PANEL_LINES + 2


def _draw_card_list(stdscr, items, selected_idx, title, subtitle, color):
    max_y, max_x = stdscr.getmaxyx()
    rule = "="*min(len(title)+6, max_x-4)
    try:
        stdscr.addstr(1, max(0,(max_x-len(rule))//2),  rule,  color|curses.A_BOLD)
        stdscr.addstr(2, max(0,(max_x-len(title))//2), title, curses.color_pair(4)|curses.A_BOLD)
        stdscr.addstr(3, max(0,(max_x-len(rule))//2),  rule,  color|curses.A_BOLD)
        if subtitle:
            stdscr.addstr(4, 3, subtitle, curses.color_pair(3)|curses.A_DIM)
    except curses.error:
        pass

    bx = 3
    bw = min(60, max_x-6)
    top_y = 6
    guard = max_y - _LOG_GUARD if log_visible else max_y - 2
    available = max(4, guard - top_y)

    # The list can be longer than the screen (lots of games), so window it
    # around the selection: the highlighted card is 4 rows tall, others 2.
    def item_h(i):
        return 4 if i == selected_idx else 2

    start = end = selected_idx
    used  = item_h(selected_idx)
    i = selected_idx - 1                       # grow upward
    while i >= 0 and used + item_h(i) <= available:
        used += item_h(i); start = i; i -= 1
    i = selected_idx + 1                        # grow downward
    while i < len(items) and used + item_h(i) <= available:
        used += item_h(i); end = i; i += 1
    i = start - 1                               # fill any remaining space up top
    while i >= 0 and used + item_h(i) <= available:
        used += item_h(i); start = i; i -= 1

    # "more above / below" hints so it's clear the list scrolls.
    if start > 0:
        try:
            stdscr.addstr(5, bx + 7, "^ more ^", curses.color_pair(3) | curses.A_DIM)
        except curses.error:
            pass

    y = top_y
    for idx in range(start, end + 1):
        label = items[idx]
        if idx == selected_idx:
            top = "+"  + "-"*(bw-2) + "+"
            mid = ("|  >>  " + label).ljust(bw-1) + "|"
            bot = "+"  + "-"*(bw-2) + "+"
            try:
                stdscr.addstr(y,   bx, top, color|curses.A_BOLD)
                stdscr.addstr(y+1, bx, mid, curses.color_pair(4)|curses.A_BOLD|curses.A_REVERSE)
                stdscr.addstr(y+2, bx, bot, color|curses.A_BOLD)
            except curses.error:
                pass
            y += 4
        else:
            try:
                stdscr.addstr(y, bx+7, label, curses.color_pair(3)|curses.A_BOLD)
            except curses.error:
                pass
            y += 2

    if end < len(items) - 1 and y < guard:
        try:
            stdscr.addstr(y, bx + 7, "v more v", curses.color_pair(3) | curses.A_DIM)
        except curses.error:
            pass


def draw_main_menu(stdscr, sel):
    _draw_card_list(
        stdscr, [label for label,_,_ in ART_OPTIONS], sel,
        title    = "PI DISPLAY CONTROLLER",
        subtitle = "  Remote/Controller: navigate    SSH: screen remote on    Log: Shift+F, 6",
        color    = curses.color_pair(2),
    )


def draw_games_menu(stdscr, sel):
    def badge(key, is2p):
        tags = (["FREE"] if key in FREE_GAMES else ["LOCK"])
        if is2p: tags.append("2P")
        # SNAKE is built-in (no external binary), so it's never "N/A".
        if key not in ("SNAKE", "TICTACTOE") and not resolve_binary(GAME_KEYS.get(key, [key])[0]):
            tags.append("N/A")
        return "  [" + "/".join(tags) + "]"

    labels = [label + badge(key, is2p) for label,_,key,is2p in GAME_OPTIONS]
    _draw_card_list(
        stdscr, labels, sel,
        title    = "GAMES LIBRARY",
        subtitle = "  A/Right=select   B/Left=back   [N/A]=install it (see below)",
        color    = curses.color_pair(5),
    )


def draw_duration_menu(stdscr, art_label, sel):
    _draw_card_list(
        stdscr, [label for label,_ in DURATION_OPTIONS], sel,
        title    = f"DURATION: {art_label}",
        subtitle = None,
        color    = curses.color_pair(1),
    )


def draw_hardlock(stdscr):
    """Full-screen lockdown block. Cleared only by
    `screen unlock authorise <emergency code>` over SSH."""
    max_y, max_x = stdscr.getmaxyx()
    stdscr.erase()
    lines = [
        "SYSTEM LOCKED DOWN",
        "",
        "Too many failed unlock attempts.",
        "The TV is being held off.",
        "",
        "An administrator must run this over SSH:",
        "screen unlock authorise <emergency code>",
    ]
    start = max(0, (max_y - len(lines)) // 2)
    for i, ln in enumerate(lines):
        attr = curses.color_pair(1) | (curses.A_BOLD if i == 0 else 0)
        try:
            stdscr.addstr(start + i, max(0, (max_x - len(ln)) // 2), ln, attr)
        except curses.error:
            pass


def draw_keypad(stdscr, game_name, entered, error, is_lock=False):
    """On-screen numpad. D-pad/joystick move, A enters, B deletes/back."""
    global keypad_row, keypad_col, lock_entered
    max_y, max_x = stdscr.getmaxyx()
    stdscr.erase()

    title = "SYSTEM LOCKED — ENTER EMERGENCY CODE" if is_lock else f"ENTER PIN: {game_name}"
    color = curses.color_pair(1) if is_lock else curses.color_pair(5)
    try:
        stdscr.addstr(1, max(0,(max_x-len(title))//2), title, color|curses.A_BOLD)
    except curses.error:
        pass

    disp = " ".join(d if is_lock else "_" for d in entered.ljust(6,"_"))
    try:
        stdscr.addstr(3, max(0,(max_x-len(disp))//2), disp, curses.color_pair(4)|curses.A_BOLD)
    except curses.error:
        pass

    if error:
        try:
            stdscr.addstr(4, max(0,(max_x-len(error))//2), error, curses.color_pair(1)|curses.A_BOLD)
        except curses.error:
            pass

    cw   = 8
    gx   = max(0, (max_x - 3*cw) // 2)
    gy   = 6
    for r, row in enumerate(KEYPAD_LAYOUT):
        for c, key in enumerate(row):
            sel   = (r == keypad_row and c == keypad_col)
            cell  = f"[{key:^4}]"
            attr  = (curses.color_pair(2)|curses.A_BOLD|curses.A_REVERSE
                     if sel else curses.color_pair(3))
            try:
                stdscr.addstr(gy + r*2, gx + c*cw, cell, attr)
            except curses.error:
                pass

    hint = "Open your authenticator app for the code"
    if is_lock:
        hint = "Enter emergency code to unlock system"
    controls = "D-pad/Stick: move   A: enter   B: delete (or back when empty)"
    try:
        stdscr.addstr(gy+9,  max(0,(max_x-len(hint))//2),     hint,     curses.color_pair(3)|curses.A_DIM)
        stdscr.addstr(gy+10, max(0,(max_x-len(controls))//2), controls, curses.color_pair(3)|curses.A_DIM)
    except curses.error:
        pass


# ─────────────────────────────────────────────────────────────────────
# KEYPAD HELPERS
# ─────────────────────────────────────────────────────────────────────

def keypad_press(key: str, target: str) -> tuple[str, bool]:
    """Process one key press on the numpad. Returns (new_entered, should_verify)."""
    global keypad_entered, lock_entered
    buf = lock_entered if target == "LOCK" else keypad_entered

    if key == "DEL":
        buf = buf[:-1]
    elif key == "OK":
        if target == "LOCK": lock_entered = buf
        else: keypad_entered = buf
        return buf, True
    elif key.isdigit():
        if len(buf) < 6:
            buf += key
        if len(buf) == 6:
            if target == "LOCK": lock_entered = buf
            else: keypad_entered = buf
            return buf, True

    if target == "LOCK": lock_entered = buf
    else: keypad_entered = buf
    return buf, False


# ─────────────────────────────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────────────────────────────

def main(stdscr):
    global current_view, selected_art_idx, selected_game_idx, selected_dur_idx
    global active_art_type, art_stop_time, should_clear_screen
    global keypad_row, keypad_col, keypad_entered, keypad_error
    global lock_entered, lock_error
    global pending_game_key, pending_game_cmd
    global ttt_sel, ttt_mode, ttt_stage, ttt_p1
    global otp_fail_count, system_locked, log_visible
    global lock_fail_count, hard_locked
    global controller_mode

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.idlok(False); stdscr.idcok(False)

    curses.start_color(); curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED,     curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_GREEN,   curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_CYAN,    curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_WHITE,   curses.COLOR_BLACK)
    curses.init_pair(5, curses.COLOR_MAGENTA, curses.COLOR_BLACK)

    frame       = 0
    last_view   = None

    # W-then-3 log toggle state
    last_key_char = ""
    last_key_time = 0.0

    while True:
        # ── View transition ──────────────────────────────────────────
        if current_view != last_view:
            stdscr.clear()
            reset_renderer_state()
            should_clear_screen = True

            # A code keypad needs raw D-pad/stick navigation; everything else
            # in the main loop uses normal menu navigation. (The blocking game
            # runners set their own mode while they're on screen.)
            controller_mode = "KEYPAD" if current_view in ("KEYPAD", "LOCKED") else "MENU"

            if current_view in ("KEYPAD",):
                keypad_row = keypad_col = 0
                keypad_entered = keypad_error = ""

            if current_view == "LOCKED":
                keypad_row = keypad_col = 0
                lock_entered = lock_error = ""

            log(f"View: {current_view}")
            last_view = current_view

        max_y, max_x = stdscr.getmaxyx()

        # ── Keyboard input ───────────────────────────────────────────
        try:
            ch = stdscr.getch()
            now = time.time()
            if ch > 0:
                # Log toggle: press Shift+F (capital F) then 6, within 1 second
                if ch == ord("F"):
                    last_key_char = "F"; last_key_time = now
                elif ch == ord("6") and last_key_char == "F" and (now-last_key_time) < 1.0:
                    log_visible = not log_visible
                    log(f"Log {'shown' if log_visible else 'hidden'}")
                    last_key_char = ""
                else:
                    last_key_char = ""

                if ch == curses.KEY_UP:              input_queue.append("UP")
                elif ch == curses.KEY_DOWN:          input_queue.append("DOWN")
                elif ch == curses.KEY_LEFT:          input_queue.append("LEFT")
                elif ch == curses.KEY_RIGHT:         input_queue.append("RIGHT")
                elif ch in (10, 13, curses.KEY_ENTER): input_queue.append("SELECT")
                elif ch in (27, ord("q")):           input_queue.append("BACK")
        except curses.error:
            pass

        # ── Process input queue ──────────────────────────────────────
        while input_queue:
            cmd = input_queue.pop(0)

            # ── Hard lockdown: ignore all input; only `screen unlock
            # authorise <code>` (over the FIFO) can clear it. ──
            if hard_locked:
                current_view = "HARDLOCK"
                continue

            # ── System locked: only unlock keypad works ──────────────
            if system_locked:
                if current_view != "LOCKED":
                    current_view = "LOCKED"
                if cmd == "UP":
                    keypad_row = (keypad_row-1) % len(KEYPAD_LAYOUT)
                elif cmd == "DOWN":
                    keypad_row = (keypad_row+1) % len(KEYPAD_LAYOUT)
                elif cmd == "LEFT":
                    keypad_col = (keypad_col-1) % 3
                elif cmd == "RIGHT":
                    keypad_col = (keypad_col+1) % 3
                elif cmd == "BACK":
                    # B deletes the last digit; there's no "leaving" a lockout.
                    if lock_entered:
                        lock_entered, _ = keypad_press("DEL", "LOCK")
                        lock_error = ""
                elif cmd == "SELECT":
                    key = KEYPAD_LAYOUT[keypad_row][keypad_col]
                    entered, verify = keypad_press(key, "LOCK")
                    if verify:
                        # A lockout is cleared ONLY by the emergency code —
                        # not by an ordinary player PIN.
                        if entered == EMERGENCY_CODE:
                            system_locked = False
                            otp_fail_count = lock_fail_count = 0
                            log("System unlocked via emergency code")
                            current_view = "MENU"
                        else:
                            lock_error   = "WRONG CODE"
                            lock_entered = ""
                            lock_fail_count += 1
                            # 3 failed unlock attempts → full lockdown.
                            if lock_fail_count >= OTP_MAX_FAILS:
                                _start_hardlock()
                                current_view = "HARDLOCK"
                # All other commands (HOME, CLEAR, etc.) are ignored while locked
                continue

            # ── HOME: return to main menu from anywhere ──────────────
            if cmd == "HOME":
                current_view = "MENU"
                break

            if cmd == "CLEAR":
                current_view = "CLEAR"
                break

            if isinstance(cmd, tuple) and cmd[0] == "DIRECT_RUN":
                _, art_key, dur_sec, payload = cmd
                if art_key == "GAME":
                    game_key = payload.strip().upper()
                    if game_key == "SNAKE":
                        run_snake(stdscr)
                    elif game_key == "TICTACTOE":
                        run_noughts(stdscr)
                    else:
                        gc = GAME_KEYS.get(game_key)
                        if gc: run_game(stdscr, gc)
                    current_view = "MENU"
                elif art_key == "MIRROR":
                    run_mirror(stdscr)
                    current_view = "MENU"
                elif art_key == "GAMES":
                    current_view = "GAMES_MENU"
                elif art_key in ("CLOCK","MATRIX"):
                    fn = render_clock_stars if art_key=="CLOCK" else render_matrix
                    active_art_type = fn
                    art_stop_time   = time.time()+dur_sec if dur_sec else None
                    current_view    = "ART_RUNNING"
                elif art_key in EXTERNAL_CMDS:
                    run_external_art(stdscr, EXTERNAL_CMDS[art_key], dur_sec)
                    current_view = "MENU"
                else:
                    current_view = "MENU"
                break

            # ── View-specific handling ───────────────────────────────
            if current_view == "MENU":
                if cmd == "UP":
                    selected_art_idx = (selected_art_idx-1) % len(ART_OPTIONS)
                elif cmd == "DOWN":
                    selected_art_idx = (selected_art_idx+1) % len(ART_OPTIONS)
                elif cmd in ("SELECT","RIGHT"):
                    _, art_type, art_key = ART_OPTIONS[selected_art_idx]
                    if art_key == "MIRROR":
                        run_mirror(stdscr)
                        current_view = "MENU"
                    elif art_key == "GAMES": current_view = "GAMES_MENU"
                    else: current_view = "DURATION_SELECT"
                elif cmd in ("BACK","LEFT"):
                    pass  # already at root

            elif current_view == "DURATION_SELECT":
                if cmd == "UP":
                    selected_dur_idx = (selected_dur_idx-1) % len(DURATION_OPTIONS)
                elif cmd == "DOWN":
                    selected_dur_idx = (selected_dur_idx+1) % len(DURATION_OPTIONS)
                elif cmd in ("BACK","LEFT"):
                    current_view = "MENU"
                elif cmd in ("SELECT","RIGHT"):
                    _, art_type, art_key = ART_OPTIONS[selected_art_idx]
                    dur_sec = DURATION_OPTIONS[selected_dur_idx][1]
                    if art_key in EXTERNAL_CMDS:
                        run_external_art(stdscr, EXTERNAL_CMDS[art_key], dur_sec)
                        current_view = "MENU"
                    else:
                        active_art_type = art_type
                        art_stop_time   = time.time()+dur_sec if dur_sec else None
                        current_view    = "ART_RUNNING"

            elif current_view == "GAMES_MENU":
                if cmd == "UP":
                    selected_game_idx = (selected_game_idx-1) % len(GAME_OPTIONS)
                elif cmd == "DOWN":
                    selected_game_idx = (selected_game_idx+1) % len(GAME_OPTIONS)
                elif cmd in ("BACK","LEFT"):
                    current_view = "MENU"
                elif cmd in ("SELECT","RIGHT"):
                    label, game_cmd, game_key, _ = GAME_OPTIONS[selected_game_idx]
                    pending_game_key = game_key
                    pending_game_cmd = game_cmd
                    if game_key == "TICTACTOE":
                        # Choose 1-player (vs computer) or 2-player first.
                        ttt_sel = 0
                        current_view = "TTT_MODE"
                    else:
                        # Every game needs a PIN, so go via the keypad; the game
                        # (built-in or external) launches after it verifies.
                        current_view = "KEYPAD"

            elif current_view == "TTT_MODE":
                if cmd in ("UP", "DOWN"):
                    ttt_sel ^= 1
                elif cmd in ("BACK", "LEFT"):
                    current_view = "GAMES_MENU"
                elif cmd in ("SELECT", "RIGHT"):
                    ttt_mode  = "1P" if ttt_sel == 0 else "2P"
                    ttt_stage = 1
                    ttt_p1    = ""
                    keypad_entered = keypad_error = ""
                    current_view = "KEYPAD"

            elif current_view == "KEYPAD":
                if cmd == "UP":
                    keypad_row = (keypad_row-1) % len(KEYPAD_LAYOUT)
                elif cmd == "DOWN":
                    keypad_row = (keypad_row+1) % len(KEYPAD_LAYOUT)
                elif cmd == "LEFT":
                    keypad_col = (keypad_col-1) % 3
                elif cmd == "RIGHT":
                    keypad_col = (keypad_col+1) % 3
                elif cmd == "BACK":
                    # B deletes the last digit; on an empty code it goes back.
                    if keypad_entered:
                        keypad_entered, _ = keypad_press("DEL", "GAME")
                        keypad_error = ""
                    else:
                        current_view = "GAMES_MENU"
                elif cmd == "SELECT":
                    key = KEYPAD_LAYOUT[keypad_row][keypad_col]
                    entered, verify = keypad_press(key, "GAME")
                    if verify:
                        who = verify_pin(entered)
                        if who:
                            otp_fail_count = 0
                            log(f"{pending_game_key} unlocked by {who}")
                            set_highscore_name(pending_game_cmd[0], who)
                            show_message(stdscr, ["", f"Welcome {who}!"],
                                         color_pair=2, duration=1.5)
                            if (pending_game_key == "TICTACTOE" and ttt_mode == "2P"
                                    and ttt_stage == 1):
                                # Player 1 verified; keep the keypad up for P2.
                                ttt_p1 = who
                                ttt_stage = 2
                                keypad_entered = keypad_error = ""
                            else:
                                if pending_game_key == "SNAKE":
                                    run_snake(stdscr)
                                elif pending_game_key == "TICTACTOE":
                                    if ttt_mode == "1P":
                                        run_noughts(stdscr, vs_computer=True)
                                    else:
                                        log(f"N&C 2P: {ttt_p1} vs {who}")
                                        run_noughts(stdscr, vs_computer=False)
                                else:
                                    run_game(stdscr, pending_game_cmd)
                                current_view = "GAMES_MENU"
                        else:
                            otp_fail_count += 1
                            remaining = OTP_MAX_FAILS - otp_fail_count
                            if otp_fail_count >= OTP_MAX_FAILS:
                                log("Max failures — system locked")
                                system_locked = True
                                current_view  = "LOCKED"
                            else:
                                keypad_error   = f"WRONG PIN — {remaining} attempt(s) left"
                                keypad_entered = ""

            elif current_view in ("ART_RUNNING","CLEAR"):
                if cmd in ("BACK","SELECT","LEFT"):
                    current_view = "MENU"

        # Auto-return from timed art
        if current_view == "ART_RUNNING" and art_stop_time:
            if time.time() >= art_stop_time:
                current_view = "MENU"

        # ── Render ──────────────────────────────────────────────────
        if current_view in ("MENU","DURATION_SELECT","GAMES_MENU","CLEAR",
                             "LOCKED","TTT_MODE"):
            stdscr.erase()

        if hard_locked:
            draw_hardlock(stdscr)

        elif system_locked or current_view == "LOCKED":
            draw_keypad(stdscr, "", lock_entered, lock_error, is_lock=True)

        elif current_view == "MENU":
            draw_main_menu(stdscr, selected_art_idx)
            draw_log_panel(stdscr)

        elif current_view == "DURATION_SELECT":
            draw_duration_menu(stdscr, ART_OPTIONS[selected_art_idx][0], selected_dur_idx)
            draw_log_panel(stdscr)

        elif current_view == "GAMES_MENU":
            draw_games_menu(stdscr, selected_game_idx)
            draw_log_panel(stdscr)

        elif current_view == "TTT_MODE":
            _draw_card_list(stdscr, ["1 PLAYER  (vs Computer)", "2 PLAYER"],
                            ttt_sel, title="NOUGHTS & CROSSES",
                            subtitle="  A/Right: choose    B/Left: back",
                            color=curses.color_pair(5))

        elif current_view == "KEYPAD":
            name = GAME_OPTIONS[selected_game_idx][0]
            if pending_game_key == "TICTACTOE" and ttt_mode == "2P":
                name += f" — PLAYER {ttt_stage}"
            draw_keypad(stdscr, name, keypad_entered, keypad_error)

        elif current_view == "ART_RUNNING":
            if should_clear_screen:
                stdscr.clear(); should_clear_screen = False
            active_art_type(stdscr, frame)
            status = (f" {int(art_stop_time-time.time())}s remaining | BACK to exit "
                      if art_stop_time else " Running indefinitely | BACK to exit ")
            try:
                stdscr.addstr(max_y-1, 0, status.center(max_x-1),
                              curses.A_REVERSE|curses.A_DIM)
            except curses.error:
                pass

        elif current_view == "CLEAR":
            pass

        stdscr.refresh()
        frame += 1
        time.sleep(0.08)


if __name__ == "__main__":
    curses.wrapper(main)
