#!/usr/bin/env python3
"""
/opt/pitv/guest-portal.py — PiTV web portals (Python stdlib only).

Runs in one of two modes from the same code:

  guest  (default, port 8080)  — password OR NFC login; TV power, HDMI input,
         menu D-pad; shows the guest's player PIN. ONLY works while the Apple
         Home "Guest Mode" switch is ON.
  admin  (--admin, port 80 -> http://raspberrypi.local) — username + password
         login; the same controls PLUS a Guest Mode on/off toggle; never gated.

Passwords are PBKDF2-HMAC-SHA256 (see pitv_secrets.py); records written by
older versions were a single round of SHA-256 and are re-hashed in place the
next time that person signs in. Login failures are counted per client IP and
locked out with a doubling backoff.

Data (all under ~/.pitv, device-only, never committed):
  guests.json  {"meta":{...},"guests":{user:{algo,salt,iters,pwhash,token}}}
  admin.json   {user:{algo,salt,iters,pwhash}}
  pins.json    shared with pin-admin.py; guest player PINs live here by name
  portal-secret  HMAC key for signed session cookies
  emergency-code the master code (pitv_secrets.py owns it)
State:
  /tmp/pitv-guest-mode   "on"/"off" (Home switch, or the admin toggle)
Actuation:
  TV power / HDMI / Guest Mode -> localhost UDP 127.0.0.1:8129 (tv_menu)
  menu navigation              -> the FIFO /tmp/tv_menu.fifo
"""

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pitv_secrets

ADMIN           = "--admin" in sys.argv[1:]
PORT            = 80 if ADMIN else 8080
COOKIE_NAME     = "pitv_admin" if ADMIN else "pitv_session"

PITV_DIR        = os.path.expanduser("~/.pitv")
GUEST_FILE      = os.path.join(PITV_DIR, "guests.json")
ADMIN_FILE      = os.path.join(PITV_DIR, "admin.json")
PIN_FILE        = os.path.join(PITV_DIR, "pins.json")
# Separate signing keys per portal so `screen guest kick all` (which rotates
# the guest key) can't log admins out. Rotating a key invalidates every
# session cookie signed with it.
GUEST_SECRET    = os.path.join(PITV_DIR, "portal-secret")
ADMIN_SECRET    = os.path.join(PITV_DIR, "admin-secret")
SECRET_FILE     = ADMIN_SECRET if ADMIN else GUEST_SECRET
GUEST_MODE_FILE = "/tmp/pitv-guest-mode"
SKY_FILE        = os.path.join(PITV_DIR, "sky.json")
# Which client addresses may reach the portals at all (`screen network ...`).
# Empty list = anyone who can route to the Pi, which is the historical
# behaviour; add a CIDR to shut out, say, a VPN range.
NETWORK_FILE    = os.path.join(PITV_DIR, "network.json")
FIFO            = "/tmp/tv_menu.fifo"
UDP_ADDR        = ("127.0.0.1", 8129)
# Admin sessions effectively never expire; guest sessions last a week.
SESSION_MAX_AGE = (3650 if ADMIN else 7) * 24 * 3600
ROTATE_PERIOD   = 7 * 24 * 3600

# Login throttling. The guest portal takes a password with no username, so it
# is the one credential worth guessing on this box; without a limiter a phone
# on the Wi-Fi could try thousands a minute. Failures are counted per client IP
# and the lockout doubles, to a ceiling.
MAX_FAILS       = 5
FAIL_WINDOW     = 15 * 60
LOCK_BASE       = 30
LOCK_CEILING    = 15 * 60
MAX_BODY        = 8192        # a login form is a few hundred bytes

NAV = {"up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
       "ok": "SELECT", "back": "BACK"}
UDP = {"tv_on": "TV_ON", "tv_off": "TV_OFF",
       "input_sky": "CEC tx 1f:82:10:00", "input_pitv": "CEC tx 1f:82:20:00"}
ADMIN_UDP = {"guest_on": "GUEST_ON", "guest_off": "GUEST_OFF"}

# Sky Q buttons the portal may press. The payload is a fixed token, never
# anything the browser supplies, and tv_menu ignores the lot unless Sky mode is
# on (`screen sky on`).
SKY_BUTTONS = [
    ("sky_sky",     "SKY",     "Sky"),
    ("sky_guide",   "GUIDE",   "TV Guide"),
    ("sky_info",    "INFO",    "Info"),
    ("sky_ch_up",   "CH_UP",   "CH +"),
    ("sky_ch_down", "CH_DOWN", "CH −"),
    ("sky_rewind",  "REWIND",  "&#9194;"),
    ("sky_play",    "PLAY",    "&#9654;"),
    ("sky_pause",   "PAUSE",   "&#10074;&#10074;"),
    ("sky_forward", "FORWARD", "&#9193;"),
    ("sky_record",  "RECORD",  "&#9679; Rec"),
    ("sky_red",     "RED",     "Red"),
    ("sky_green",   "GREEN",   "Green"),
    ("sky_yellow",  "YELLOW",  "Yellow"),
    ("sky_blue",    "BLUE",    "Blue"),
]
SKY_ACTIONS = {a: tok for a, tok, _ in SKY_BUTTONS}


# ── storage helpers ──────────────────────────────────────────────────
def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_guests():
    d = _load(GUEST_FILE, {})
    d.setdefault("meta", {})
    d.setdefault("guests", {})
    return d


def get_secret():
    try:
        with open(SECRET_FILE, "rb") as f:
            s = f.read().strip()
            if s:
                return s
    except Exception:
        pass
    s = secrets.token_bytes(32)
    try:
        os.makedirs(PITV_DIR, exist_ok=True)
        with open(SECRET_FILE, "wb") as f:
            f.write(s)
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    return s


def guest_mode_on():
    try:
        with open(GUEST_MODE_FILE) as f:
            return f.read().strip().lower() == "on"
    except Exception:
        return False


def guest_pin(username):
    return _load(PIN_FILE, {}).get(username, "—")


# ── PIN rotation (weekly, guest mode only) ───────────────────────────
def _new_pin(used):
    used = set(used) | {pitv_secrets.emergency_code()}
    while True:
        p = f"{secrets.randbelow(1000000):06d}"
        if p not in used:
            return p


_last_rotate_check = 0.0
_rotate_lock       = threading.Lock()


def maybe_rotate():
    """Rotate every guest's player PIN once a week. Cheap to call often — it
    only touches disk once a minute unless a rotation is actually due. The lock
    stops two simultaneous requests from rotating twice and handing one guest a
    PIN that changes again a moment later."""
    global _last_rotate_check
    now = time.time()
    with _rotate_lock:
        if now - _last_rotate_check < 60:
            return
        _last_rotate_check = now
        _rotate_if_due()


def _rotate_if_due():
    data = load_guests()
    last = data["meta"].get("pin_rotated", 0)
    if data["guests"] and time.time() - last > ROTATE_PERIOD:
        pins = _load(PIN_FILE, {})
        for user in data["guests"]:
            pins[user] = _new_pin(pins.values())
        _save(PIN_FILE, pins)
        data["meta"]["pin_rotated"] = int(time.time())
        _save(GUEST_FILE, data)


# ── auth ─────────────────────────────────────────────────────────────
def _upgrade_guest(user, password):
    """Re-hash a legacy guest record with PBKDF2 now that we hold the
    plaintext. Keeps the salt-and-token structure; nobody has to re-register."""
    data = load_guests()
    rec  = data["guests"].get(user)
    if not rec:
        return
    rec.update(pitv_secrets.hash_password(password))
    data["guests"][user] = rec
    _save(GUEST_FILE, data)


def _upgrade_admin(user, password):
    admins = _load(ADMIN_FILE, {})
    rec = admins.get(user)
    if not rec:
        return
    rec.update(pitv_secrets.hash_password(password))
    admins[user] = rec
    _save(ADMIN_FILE, admins)


def check_password(password):
    """Guest login: password only -> matching guest username, or None.

    Every stored guest is checked even after a match so the reply doesn't leak,
    by how long it took, how far down the list the matching guest sits."""
    if not password:
        return None
    match = None
    for user, g in load_guests()["guests"].items():
        ok, stale = pitv_secrets.verify_password(g, password)
        if ok and match is None:
            match = (user, stale)
    if not match:
        return None
    user, stale = match
    if stale:
        _upgrade_guest(user, password)
    return user


def check_admin(username, password):
    """Admin login: username + password -> username, or None."""
    if not username or not password:
        return None
    rec = _load(ADMIN_FILE, {}).get(username)
    if not rec:
        return None
    ok, stale = pitv_secrets.verify_password(rec, password)
    if not ok:
        return None
    if stale:
        _upgrade_admin(username, password)
    return username


# ── login throttling ─────────────────────────────────────────────────
_fail_state = {}                  # ip -> {"fails": n, "until": ts, "seen": ts}
_fail_lock  = threading.Lock()


def _throttle_until(ip):
    """Seconds the caller must wait, or 0 if they may try now."""
    now = time.time()
    with _fail_lock:
        st = _fail_state.get(ip)
        if not st:
            return 0
        if now - st["seen"] > FAIL_WINDOW:
            _fail_state.pop(ip, None)
            return 0
        return max(0, int(st["until"] - now))


def _note_failure(ip):
    now = time.time()
    with _fail_lock:
        st = _fail_state.get(ip)
        if not st or now - st["seen"] > FAIL_WINDOW:
            st = {"fails": 0, "until": 0.0, "seen": now}
        st["fails"] += 1
        st["seen"]   = now
        if st["fails"] >= MAX_FAILS:
            backoff = min(LOCK_BASE * 2 ** (st["fails"] - MAX_FAILS), LOCK_CEILING)
            st["until"] = now + backoff
        _fail_state[ip] = st
        # Don't let a busy network grow this without bound.
        if len(_fail_state) > 512:
            for old, v in list(_fail_state.items()):
                if now - v["seen"] > FAIL_WINDOW:
                    _fail_state.pop(old, None)


def _note_success(ip):
    with _fail_lock:
        _fail_state.pop(ip, None)


def valid_users():
    return (set(_load(ADMIN_FILE, {})) if ADMIN
            else set(load_guests()["guests"]))


def user_for_token(token):
    if not token:
        return None
    for user, g in load_guests()["guests"].items():
        if hmac.compare_digest(g.get("token", ""), token):
            return user
    return None


def sign_session(username):
    exp = int(time.time()) + SESSION_MAX_AGE
    payload = f"{username}|{exp}".encode()
    sig = hmac.new(get_secret(), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload).decode() + "." + sig


def verify_session(cookie_val):
    try:
        b64, sig = cookie_val.split(".", 1)
        payload = base64.urlsafe_b64decode(b64.encode())
        good = hmac.new(get_secret(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        user, exp = payload.decode().split("|")
        if int(exp) < time.time():
            return None
        return user if user in valid_users() else None
    except Exception:
        return None


# ── actuation ────────────────────────────────────────────────────────
def send_udp(msg):
    """Send a SIGNED command to tv_menu. Unsigned datagrams are dropped at the
    far end, so a process without the shared key can't drive the TV even from
    on the Pi itself."""
    try:
        signed = pitv_secrets.sign_command(msg)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(signed.encode(), UDP_ADDR)
        s.close()
    except Exception:
        pass


def sky_on():
    """Is Sky control switched on? Only then does the portal show its panel."""
    try:
        with open(SKY_FILE) as f:
            return bool(json.load(f).get("enabled"))
    except Exception:
        return False


# ── network allowlist ────────────────────────────────────────────────
_allow_cache = (0.0, [])


def allowed_networks():
    """Parsed allowlist, re-read at most once a minute so `screen network`
    changes apply without restarting the portals."""
    global _allow_cache
    now = time.time()
    if now - _allow_cache[0] < 60:
        return _allow_cache[1]
    nets = []
    for entry in _load(NETWORK_FILE, {}).get("allow", []):
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            pass
    _allow_cache = (now, nets)
    return nets


def address_allowed(ip):
    nets = allowed_networks()
    if not nets:
        return True                       # no allowlist configured
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def write_fifo(token):
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, (token + "\n").encode())
        os.close(fd)
    except Exception:
        pass


def set_guest_mode(on):
    """Admin toggle: write the state file and notify tv_menu (which keeps the
    Home switch in sync)."""
    try:
        with open(GUEST_MODE_FILE, "w") as f:
            f.write("on" if on else "off")
    except OSError:
        pass
    send_udp("GUEST_ON" if on else "GUEST_OFF")


# ── HTML ─────────────────────────────────────────────────────────────
PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>PiTV {who}</title><style>
*{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,system-ui,sans-serif;
background:#0b0f1a;color:#e8ecf4;text-align:center}}
.wrap{{max-width:440px;margin:0 auto;padding:22px}}
h1{{font-size:22px;margin:.4em 0}}.muted{{color:#8b93a7;font-size:14px}}
.card{{background:#151b2b;border-radius:16px;padding:18px;margin:14px 0}}
button{{font-size:17px;padding:14px;border:0;border-radius:12px;background:#2a3350;
color:#fff;width:100%;margin:6px 0;cursor:pointer}}button:active{{background:#3a466e}}
input{{font-size:17px;padding:12px;width:100%;border-radius:10px;border:1px solid #2a3350;
background:#0b0f1a;color:#fff;margin:6px 0}}
.row{{display:flex;gap:8px}}.row button{{margin:0}}
.pad{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;max-width:260px;margin:0 auto}}
.skygrid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}
.skygrid button{{font-size:15px;padding:12px 4px;margin:0}}
.pad button{{aspect-ratio:1;font-size:20px}}.pad .sp{{visibility:hidden}}
.pin{{font-size:30px;letter-spacing:4px;font-weight:700;color:#7fd1ff}}
.on{{background:#1f7a3f}}.off{{background:#7a2a2a}}
a{{color:#7fd1ff}}
</style></head><body><div class=wrap>{body}</div></body></html>"""


def page(body):
    return PAGE.format(body=body, who="Admin" if ADMIN else "Guest")


def login_body(err=""):
    e = f'<p style="color:#ff8a8a">{html.escape(err)}</p>' if err else ""
    userfield = ('<input name=username placeholder="Username" autofocus>'
                 if ADMIN else "")
    title = "PiTV Admin" if ADMIN else "PiTV Guest"
    hint = "Sign in." if ADMIN else "Enter the guest password."
    return (f"<h1>{title}</h1><p class=muted>{hint}</p>"
            f'<div class=card><form method=post action="/login">{e}{userfield}'
            f'<input type=password name=password placeholder="Password"'
            f'{"" if ADMIN else " autofocus"}>'
            f'<button type=submit>Sign in</button></form></div>')


def gate_body():
    return ("<h1>PiTV Guest</h1><div class=card>"
            "<p>Please ask the administrator to turn on <b>Guest Mode</b>.</p>"
            "</div>")


def controls_body(username):
    # Look the PIN up with the real name; escape only for display, so a name
    # with an & or < in it still resolves AND can't inject markup.
    pin  = html.escape(str(guest_pin(username)))
    name = html.escape(username)
    if ADMIN:
        gm = guest_mode_on()
        pin_card = (f'<div class=card><p class=muted>Your game PIN</p>'
                    f'<div class=pin>{pin}</div></div>') if pin != "—" else ""
        head = (f"<h1>PiTV Admin</h1><p class=muted>Signed in as {name}</p>"
                + pin_card +
                f'<div class=card><p class=muted>Guest Mode is '
                f'<b>{"ON" if gm else "OFF"}</b></p><div class=row>'
                f'<button class=on onclick="act(\'guest_on\')">Guest Mode On</button>'
                f'<button class=off onclick="act(\'guest_off\')">Guest Mode Off</button>'
                f'</div></div>')
    else:
        head = (f"<h1>PiTV Guest</h1><p class=muted>Signed in as {name}</p>"
                f"<div class=card><p class=muted>Your game PIN</p>"
                f'<div class=pin>{pin}</div></div>')
    sky = ""
    if sky_on():
        rows = "".join(
            f'<button onclick="act(\'{a}\')">{label}</button>'
            for a, _tok, label in SKY_BUTTONS)
        sky = ('<div class=card><p class=muted>Sky Q</p>'
               '<div class=skygrid>' + rows + '</div>'
               '<div class=row style="margin-top:8px">'
               '<button onclick="act(\'sky_power_on\')">Sky On</button>'
               '<button onclick="act(\'sky_power_off\')">Sky Standby</button>'
               '</div></div>')
    return head + sky + """
<div class=card><div class=row>
  <button onclick="act('tv_on')">TV On</button>
  <button onclick="act('tv_off')">TV Off</button></div>
<div class=row style="margin-top:8px">
  <button onclick="act('input_sky')">Sky</button>
  <button onclick="act('input_pitv')">PiTV</button></div></div>
<div class=card><p class=muted>Navigate</p><div class=pad>
  <span class="sp"></span><button onclick="act('up')">&#9650;</button><span class="sp"></span>
  <button onclick="act('left')">&#9664;</button><button onclick="act('ok')">OK</button>
  <button onclick="act('right')">&#9654;</button>
  <button onclick="act('back')">Back</button><button onclick="act('down')">&#9660;</button>
  <span class="sp"></span></div></div>
<p class=muted><a href="/logout">Sign out</a></p>
<script>function act(a){fetch('/action',{method:'POST',
headers:{'Content-Type':'application/x-www-form-urlencoded'},
body:'action='+a}).then(function(){if(a.indexOf('guest_')==0)location.reload();});}</script>"""


# ── HTTP handler ─────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "PiTV"

    def _security_headers(self):
        # The pages use inline styles/handlers, so the CSP allows inline but
        # nothing external and no framing.
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _html(self, body, code=200, cookie=None):
        data = page(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location, cookie=None):
        self.send_response(302)
        self.send_header("Location", location)
        self._security_headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _client_ip(self):
        # Direct LAN connections only — no proxy, so no XFF to trust.
        try:
            return self.client_address[0]
        except Exception:
            return "?"

    def _same_origin(self):
        """Reject a cross-site POST. Browsers omit Origin on some same-origin
        requests (older Safari), so an absent header is allowed — this is
        defence in depth behind the SameSite=Lax cookie, not the only guard."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("Host") or "").strip()
        return urlparse(origin).netloc == host

    def _session_user(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE_NAME in c:
            return verify_session(c[COOKIE_NAME].value)
        return None

    @staticmethod
    def _cookie(value, age):
        return (f"{COOKIE_NAME}={value}; Path=/; Max-Age={age}; "
                f"HttpOnly; SameSite=Lax")

    def _body(self):
        """Parse a form body, refusing anything oversized so a bogus
        Content-Length can't make us allocate arbitrary memory."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/nfc" and not ADMIN:
            # An NFC tag is a bearer credential, so guessing it is throttled the
            # same way a password is.
            ip = self._client_ip()
            if _throttle_until(ip):
                self._redirect("/")
                return
            qs = parse_qs(urlparse(self.path).query)
            user = user_for_token(qs.get("t", [""])[0])
            if user and guest_mode_on():
                _note_success(ip)
                self._redirect("/", self._cookie(sign_session(user), SESSION_MAX_AGE))
            else:
                _note_failure(ip)
                self._redirect("/")
            return
        if path == "/logout":
            self._redirect("/", f"{COOKIE_NAME}=; Path=/; Max-Age=0")
            return
        if path != "/":
            self.send_response(404); self.end_headers(); return

        if not ADMIN and not guest_mode_on():
            self._html(gate_body()); return
        if not ADMIN:
            # Weekly PIN rotation used to be checked only at startup, so on a Pi
            # that never restarts it never actually happened. Check it here, at
            # most once a minute, so the schedule is real.
            maybe_rotate()
        user = self._session_user()
        self._html(controls_body(user) if user else login_body())

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._same_origin():
            self.send_response(403); self.end_headers(); return
        if not ADMIN and not guest_mode_on():
            self._html(gate_body()); return

        if path == "/login":
            ip = self._client_ip()
            wait = _throttle_until(ip)
            if wait:
                self._html(login_body(
                    f"Too many attempts — try again in {wait} s."), code=429)
                return
            b = self._body()
            if b is None:
                self.send_response(413); self.end_headers(); return
            user = (check_admin(b.get("username", ""), b.get("password", ""))
                    if ADMIN else check_password(b.get("password", "")))
            if user:
                _note_success(ip)
                self._redirect("/", self._cookie(sign_session(user), SESSION_MAX_AGE))
            else:
                _note_failure(ip)
                self._html(login_body("Wrong login." if ADMIN else "Wrong password."),
                           code=401)
            return

        if path == "/action":
            if not self._session_user():
                self.send_response(403); self.end_headers(); return
            b = self._body()
            if b is None:
                self.send_response(413); self.end_headers(); return
            action = b.get("action", "")
            if action in NAV:
                write_fifo(NAV[action])
            elif action in UDP:
                send_udp(UDP[action])
            elif action in SKY_ACTIONS:
                # Fixed token from our own table — the browser can't name an
                # arbitrary CEC frame. tv_menu drops it unless Sky mode is on.
                send_udp(f"SKY {SKY_ACTIONS[action]}")
            elif action in ("sky_power_on", "sky_power_off"):
                send_udp("SKY_POWER_ON" if action.endswith("_on")
                         else "SKY_POWER_OFF")
            elif ADMIN and action == "guest_on":
                set_guest_mode(True)
            elif ADMIN and action == "guest_off":
                set_guest_mode(False)
            self.send_response(204); self.end_headers()
            return

        self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


class PiTVServer(ThreadingHTTPServer):
    """Drops connections from outside the allowlist before a single byte of
    request is parsed — the login page isn't even reachable from an address
    that isn't allowed."""

    def verify_request(self, request, client_address):
        if address_allowed(client_address[0]):
            return True
        print(f"refused connection from {client_address[0]}", flush=True)
        return False


def main():
    if not ADMIN:
        maybe_rotate()
    get_secret()
    if not pitv_secrets.control_key():
        print("WARNING: no control key readable — TV commands will be refused "
              "by tv_menu. Run ./deploy.sh", flush=True)
    nets = allowed_networks()
    if nets:
        print("Allowing only: " + ", ".join(str(n) for n in nets), flush=True)
    httpd = PiTVServer(("0.0.0.0", PORT), Handler)
    print(f"PiTV {'admin' if ADMIN else 'guest'} portal on :{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
