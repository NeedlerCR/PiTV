import curses
import math
import os
import random
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
    from evdev import ecodes
    EVDEV_OK = True
except ImportError:
    EVDEV_OK = False

try:
    import pyotp
    PYOTP_OK = True
except ImportError:
    PYOTP_OK = False

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
# OTP / LOCKOUT SYSTEM
# ─────────────────────────────────────────────────────────────────────

EMERGENCY_CODE  = "159753"
FREE_GAMES      = {"SNAKE", "TETRIS"}
GAME_SECRET     = "JBSWY3DPEHPK3PXP"   # one entry in authenticator covers all locked games
OTP_MAX_FAILS   = 3


def verify_otp(game_key: str, code: str) -> bool:
    if code == EMERGENCY_CODE:
        log(f"OTP: emergency code used for {game_key}")
        return True
    if game_key in FREE_GAMES:
        return True
    if not PYOTP_OK:
        log("OTP: pyotp not installed — granting access")
        return True
    result = pyotp.TOTP(GAME_SECRET).verify(code, valid_window=1)
    log(f"OTP: {'PASS' if result else 'FAIL'} for {game_key}")
    return result


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

# Lockout
otp_fail_count  = 0
system_locked   = False
lock_entered    = ""
lock_error      = ""

# Controller scan trigger
_controller_scan_event = threading.Event()
_controller_scan_event.set()   # scan immediately on startup

# Controller joystick debounce
_last_joy_time: float = 0.0
_JOY_COOLDOWN         = 0.25

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
    "SNAKE":      ["snake"],
    "TETRIS":     ["bastet"],
    "INVADERS":   ["ninvaders"],
    "PACMAN":     ["pacman4console"],
    "BUGGY":      ["moon-buggy"],
    "BOMBERMAN":  ["bombardier"],
    "BREAKOUT":   ["lbreakout2"],
    "SHOOTER":    ["chromium-bsu"],
    "ASTEROIDS":  ["kobodeluxe"],
    "BATTLESHIP": ["bs"],
}

# (label, cmd_list, game_key, is_2player)
GAME_OPTIONS = [
    ("SNAKE",          ["snake"],           "SNAKE",      False),
    ("TETRIS",         ["bastet"],          "TETRIS",     False),
    ("SPACE INVADERS", ["ninvaders"],       "INVADERS",   False),
    ("PAC-MAN",        ["pacman4console"],  "PACMAN",     False),
    ("MOON BUGGY",     ["moon-buggy"],      "BUGGY",      False),
    ("BOMBERMAN",      ["bombardier"],      "BOMBERMAN",  False),
    ("BREAKOUT",       ["lbreakout2"],      "BREAKOUT",   False),
    ("SPACE SHOOTER",  ["chromium-bsu"],    "SHOOTER",    False),
    ("ASTEROID BELT",  ["kobodeluxe"],      "ASTEROIDS",  False),
    ("BATTLESHIP [2P]",["bs"],             "BATTLESHIP", True),
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
    ("RETRO GAMES",   "GAMES",  "GAMES"),
]


# ─────────────────────────────────────────────────────────────────────
# CONTROLLER  (generic Bluetooth / USB gamepad via evdev)
# ─────────────────────────────────────────────────────────────────────

def _handle_controller_event(event) -> None:
    global _last_joy_time
    if not EVDEV_OK:
        return

    if event.type == ecodes.EV_KEY and event.value == 1:   # key-down only
        c = event.code

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

        elif c == ecodes.BTN_MODE:           # HOME button → go to main menu
            input_queue.append("HOME")

    elif event.type == ecodes.EV_ABS:
        if event.code == ecodes.ABS_HAT0Y:
            if event.value == -1:
                input_queue.append("UP")
            elif event.value == 1:
                input_queue.append("DOWN")
        elif event.code == ecodes.ABS_HAT0X:
            if event.value == -1:
                input_queue.append("BACK")   # hat LEFT  → back
            elif event.value == 1:
                input_queue.append("SELECT") # hat RIGHT → select
        elif event.code == ecodes.ABS_Y:
            # Left joystick Y: up/down navigation with debounce
            now = time.time()
            if now - _last_joy_time >= _JOY_COOLDOWN:
                if event.value < -8000:
                    input_queue.append("UP")
                    _last_joy_time = now
                elif event.value > 8000:
                    input_queue.append("DOWN")
                    _last_joy_time = now


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


def listen_cec_remote():
    while True:
        try:
            proc = subprocess.Popen(
                ["cec-client", "-d", "8"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in iter(proc.stdout.readline, ""):
                if "key pressed:" in line:
                    key = line.split("key pressed:")[1].strip().split(" ")[0].lower()
                    log(f"CEC: {key}")
                    if   key == "up":                        input_queue.append("UP")
                    elif key == "down":                      input_queue.append("DOWN")
                    elif key in ("select","enter"):          input_queue.append("SELECT")
                    elif key in ("exit","back","clear","return"): input_queue.append("BACK")
            proc.wait()
        except Exception:
            pass
        time.sleep(1)


threading.Thread(target=listen_fifo,        daemon=True).start()
threading.Thread(target=listen_cec_remote,  daemon=True).start()
threading.Thread(target=listen_controllers, daemon=True).start()

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
    binary = cmd_list[0]

    # ── Pre-flight: is the binary installed? (checks /usr/games too) ──
    resolved = resolve_binary(binary)
    if not resolved:
        pkg_hint = {
            "snake":          "bsdgames",
            "bastet":         "bastet",
            "ninvaders":      "ninvaders",
            "pacman4console": "pacman4console",
            "moon-buggy":     "moon-buggy",
            "bombardier":     "bombardier",
            "lbreakout2":     "lbreakout2",
            "chromium-bsu":   "chromium-bsu",
            "kobodeluxe":     "kobodeluxe",
            "bs":             "bsdgames",
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
    curses.def_prog_mode()
    curses.endwin()
    _reset_terminal()
    os.system("clear")

    SDL_GAMES = {"chromium-bsu", "lbreakout2", "kobodeluxe"}
    is_sdl    = binary in SDL_GAMES

    # Controller-to-keys mapper for ncurses games
    mapper_proc = None
    if EVDEV_OK and not is_sdl:
        mapper = "/opt/pitv/controller-to-keys.py"
        if os.path.exists(mapper):
            try:
                mapper_proc = subprocess.Popen(
                    ["python3", mapper],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                log(f"Mapper start failed: {e}")

    env = os.environ.copy()
    if is_sdl:
        env["SDL_VIDEODRIVER"] = "kmsdrm"
        env["SDL_AUDIODRIVER"]  = "alsa"

    try:
        proc = subprocess.Popen(cmd_list, env=env)
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

    log(f"Game stop: {binary}")
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
    y  = 6
    guard = max_y - _LOG_GUARD if log_visible else max_y - 2

    for idx, label in enumerate(items):
        if y >= guard:
            break
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
        if not resolve_binary(GAME_KEYS.get(key, [key])[0]): tags.append("N/A")
        return "  [" + "/".join(tags) + "]"

    labels = [label + badge(key, is2p) for label,_,key,is2p in GAME_OPTIONS]
    _draw_card_list(
        stdscr, labels, sel,
        title    = "RETRO GAMES",
        subtitle = "  A/Right=select   B/Left=back   [N/A]=not installed",
        color    = curses.color_pair(5),
    )


def draw_duration_menu(stdscr, art_label, sel):
    _draw_card_list(
        stdscr, [label for label,_ in DURATION_OPTIONS], sel,
        title    = f"DURATION: {art_label}",
        subtitle = None,
        color    = curses.color_pair(1),
    )


def draw_keypad(stdscr, game_name, entered, error, is_lock=False):
    """On-screen numpad. Navigate with D-pad/joystick, A/Right to press."""
    global keypad_row, keypad_col, lock_entered
    max_y, max_x = stdscr.getmaxyx()
    stdscr.erase()

    title = "SYSTEM LOCKED — ENTER EMERGENCY CODE" if is_lock else f"ENTER CODE: {game_name}"
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
    try:
        stdscr.addstr(gy+9, max(0,(max_x-len(hint))//2), hint, curses.color_pair(3)|curses.A_DIM)
    except curses.error:
        pass


def draw_mirror_screen(stdscr, ok):
    max_y, max_x = stdscr.getmaxyx()
    stdscr.erase()
    title = "SCREEN MIRRORING ACTIVE" if ok else "SCREEN MIRROR — ERROR"
    color = curses.color_pair(2) if ok else curses.color_pair(1)
    try:
        stdscr.addstr(2, max(0,(max_x-len(title))//2), title, color|curses.A_BOLD)
    except curses.error:
        pass
    lines = ([
        "", "iPhone / iPad / Mac:",
        "   Control Centre → Screen Mirroring → PiTV",
        "", "Android:", "   Requires scrcpy or a Miracast-compatible app.",
        "", "Press BACK / B to stop.",
    ] if ok else [
        "", "uxplay failed to start.", "",
        "Install: sudo apt install uxplay gstreamer1.0-plugins-bad",
        "         gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly",
        "", "Press BACK to return.",
    ])
    for i, line in enumerate(lines):
        try: stdscr.addstr(4+i, 4, line, curses.color_pair(4))
        except curses.error: pass


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
    global otp_fail_count, system_locked, log_visible

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.idlok(False); stdscr.idcok(False)

    curses.start_color(); curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED,     curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_GREEN,   curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_CYAN,    curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_WHITE,   curses.COLOR_BLACK)
    curses.init_pair(5, curses.COLOR_MAGENTA, curses.COLOR_BLACK)

    frame      = 0
    last_view  = None
    mirror_proc = None
    uxplay_ok   = True

    # W-then-3 log toggle state
    last_key_char = ""
    last_key_time = 0.0

    while True:
        # ── View transition ──────────────────────────────────────────
        if current_view != last_view:
            stdscr.clear()
            reset_renderer_state()
            should_clear_screen = True

            if current_view == "MIRROR_ACTIVE":
                log("Starting uxplay (kmssink + software decode)")
                try:
                    # Pi OS Lite has no X11 — kmssink is required for framebuffer
                    # output, and -avdec forces software decoding since VAAPI
                    # hardware decode isn't configured on this system.
                    mirror_proc = subprocess.Popen(
                        ["uxplay", "-n", "PiTV", "-vs", "kmssink", "-avdec"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    uxplay_ok = True
                except FileNotFoundError:
                    log("ERROR: uxplay not found")
                    mirror_proc = None; uxplay_ok = False

            if last_view == "MIRROR_ACTIVE" and mirror_proc:
                if mirror_proc.poll() is None:
                    mirror_proc.terminate()
                    log("uxplay stopped")
                mirror_proc = None

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

            # ── System locked: only unlock keypad works ──────────────
            if system_locked:
                if current_view != "LOCKED":
                    current_view = "LOCKED"
                if cmd == "UP":
                    keypad_row = (keypad_row-1) % len(KEYPAD_LAYOUT)
                elif cmd == "DOWN":
                    keypad_row = (keypad_row+1) % len(KEYPAD_LAYOUT)
                elif cmd in ("LEFT","BACK"):
                    keypad_col = (keypad_col-1) % 3
                elif cmd in ("RIGHT",):
                    keypad_col = (keypad_col+1) % 3
                elif cmd in ("SELECT",):
                    key = KEYPAD_LAYOUT[keypad_row][keypad_col]
                    entered, verify = keypad_press(key, "LOCK")
                    if verify:
                        if entered == EMERGENCY_CODE:
                            system_locked = False
                            otp_fail_count = 0
                            log("System unlocked via emergency code")
                            current_view = "MENU"
                        else:
                            lock_error   = "WRONG CODE"
                            lock_entered = ""
                # All other commands (HOME, CLEAR, etc.) are ignored while locked
                continue

            # ── HOME: return to main menu from anywhere ──────────────
            if cmd == "HOME":
                if mirror_proc and mirror_proc.poll() is None:
                    mirror_proc.terminate(); mirror_proc = None
                current_view = "MENU"
                break

            if cmd == "CLEAR":
                if mirror_proc and mirror_proc.poll() is None:
                    mirror_proc.terminate(); mirror_proc = None
                current_view = "CLEAR"
                break

            if isinstance(cmd, tuple) and cmd[0] == "DIRECT_RUN":
                _, art_key, dur_sec, payload = cmd
                if art_key == "GAME":
                    gc = GAME_KEYS.get(payload.strip().upper())
                    if gc: run_game(stdscr, gc)
                    current_view = "MENU"
                elif art_key == "MIRROR":
                    current_view = "MIRROR_ACTIVE"
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
                    if art_key == "MIRROR": current_view = "MIRROR_ACTIVE"
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
                    if game_key in FREE_GAMES:
                        run_game(stdscr, game_cmd)
                        current_view = "GAMES_MENU"
                    else:
                        pending_game_key = game_key
                        pending_game_cmd = game_cmd
                        current_view     = "KEYPAD"

            elif current_view == "KEYPAD":
                if cmd in ("BACK","LEFT"):
                    current_view = "GAMES_MENU"
                elif cmd == "UP":
                    keypad_row = (keypad_row-1) % len(KEYPAD_LAYOUT)
                elif cmd == "DOWN":
                    keypad_row = (keypad_row+1) % len(KEYPAD_LAYOUT)
                elif cmd in ("LEFT",):
                    keypad_col = (keypad_col-1) % 3
                elif cmd in ("RIGHT",):
                    keypad_col = (keypad_col+1) % 3
                elif cmd in ("SELECT",):
                    key = KEYPAD_LAYOUT[keypad_row][keypad_col]
                    entered, verify = keypad_press(key, "GAME")
                    if verify:
                        if verify_otp(pending_game_key, entered):
                            otp_fail_count = 0
                            run_game(stdscr, pending_game_cmd)
                            current_view = "GAMES_MENU"
                        else:
                            otp_fail_count += 1
                            remaining = OTP_MAX_FAILS - otp_fail_count
                            if otp_fail_count >= OTP_MAX_FAILS:
                                log("OTP max failures — system locked")
                                system_locked = True
                                current_view  = "LOCKED"
                            else:
                                keypad_error   = f"WRONG CODE — {remaining} attempt(s) left"
                                keypad_entered = ""

            elif current_view == "MIRROR_ACTIVE":
                if cmd in ("BACK","LEFT"):
                    if mirror_proc and mirror_proc.poll() is None:
                        mirror_proc.terminate(); mirror_proc = None
                    current_view = "MENU"

            elif current_view in ("ART_RUNNING","CLEAR"):
                if cmd in ("BACK","SELECT","LEFT"):
                    current_view = "MENU"

        # Auto-return from timed art
        if current_view == "ART_RUNNING" and art_stop_time:
            if time.time() >= art_stop_time:
                current_view = "MENU"

        # ── Render ──────────────────────────────────────────────────
        if current_view in ("MENU","DURATION_SELECT","GAMES_MENU","CLEAR","LOCKED"):
            stdscr.erase()

        if system_locked or current_view == "LOCKED":
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

        elif current_view == "KEYPAD":
            draw_keypad(stdscr,
                        GAME_OPTIONS[selected_game_idx][0],
                        keypad_entered, keypad_error)

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

        elif current_view == "MIRROR_ACTIVE":
            draw_mirror_screen(stdscr, uxplay_ok)
            draw_log_panel(stdscr)

        elif current_view == "CLEAR":
            pass

        stdscr.refresh()
        frame += 1
        time.sleep(0.08)


if __name__ == "__main__":
    curses.wrapper(main)