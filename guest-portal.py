#!/usr/bin/env python3
"""
/opt/pitv/guest-portal.py — PiTV web portals (Python stdlib only).

Runs in one of two modes from the same code:

  guest  (default, port 8080)  — password OR NFC login; TV power, HDMI input,
         menu D-pad; shows the guest's player PIN. ONLY works while the Apple
         Home "Guest Mode" switch is ON.
  admin  (--admin, port 80 -> http://raspberrypi.local) — username + password
         login; the same controls PLUS a Guest Mode on/off toggle; never gated.

Data (all under ~/.pitv, device-only, never committed):
  guests.json  {"meta":{...},"guests":{user:{salt,pwhash,token}}}
  admin.json   {user:{salt,pwhash}}
  pins.json    shared with pin-admin.py; guest player PINs live here by name
  portal-secret  HMAC key for signed session cookies
State:
  /tmp/pitv-guest-mode   "on"/"off" (Home switch, or the admin toggle)
Actuation:
  TV power / HDMI / Guest Mode -> localhost UDP 127.0.0.1:8129 (tv_menu)
  menu navigation              -> the FIFO /tmp/tv_menu.fifo
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import sys
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ADMIN           = "--admin" in sys.argv[1:]
PORT            = 80 if ADMIN else 8080
COOKIE_NAME     = "pitv_admin" if ADMIN else "pitv_session"

PITV_DIR        = os.path.expanduser("~/.pitv")
GUEST_FILE      = os.path.join(PITV_DIR, "guests.json")
ADMIN_FILE      = os.path.join(PITV_DIR, "admin.json")
PIN_FILE        = os.path.join(PITV_DIR, "pins.json")
SECRET_FILE     = os.path.join(PITV_DIR, "portal-secret")
GUEST_MODE_FILE = "/tmp/pitv-guest-mode"
FIFO            = "/tmp/tv_menu.fifo"
UDP_ADDR        = ("127.0.0.1", 8129)
SESSION_MAX_AGE = 7 * 24 * 3600
ROTATE_PERIOD   = 7 * 24 * 3600
EMERGENCY_CODE  = "159753"

NAV = {"up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
       "ok": "SELECT", "back": "BACK"}
UDP = {"tv_on": "TV_ON", "tv_off": "TV_OFF",
       "input_sky": "CEC tx 1f:82:10:00", "input_pitv": "CEC tx 1f:82:20:00"}
ADMIN_UDP = {"guest_on": "GUEST_ON", "guest_off": "GUEST_OFF"}


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
    used = set(used) | {EMERGENCY_CODE}
    while True:
        p = f"{secrets.randbelow(1000000):06d}"
        if p not in used:
            return p


def maybe_rotate():
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
def _pw_ok(rec, password):
    try:
        calc = hashlib.sha256((rec["salt"] + password).encode()).hexdigest()
    except Exception:
        return False
    return hmac.compare_digest(calc, rec.get("pwhash", ""))


def check_password(password):
    """Guest login: password only -> matching guest username, or None."""
    for user, g in load_guests()["guests"].items():
        if _pw_ok(g, password):
            return user
    return None


def check_admin(username, password):
    """Admin login: username + password -> username, or None."""
    rec = _load(ADMIN_FILE, {}).get(username)
    return username if rec and _pw_ok(rec, password) else None


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
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(msg.encode(), UDP_ADDR)
        s.close()
    except Exception:
        pass


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
.pad button{{aspect-ratio:1;font-size:20px}}.pad .sp{{visibility:hidden}}
.pin{{font-size:30px;letter-spacing:4px;font-weight:700;color:#7fd1ff}}
.on{{background:#1f7a3f}}.off{{background:#7a2a2a}}
a{{color:#7fd1ff}}
</style></head><body><div class=wrap>{body}</div></body></html>"""


def page(body):
    return PAGE.format(body=body, who="Admin" if ADMIN else "Guest")


def login_body(err=""):
    e = f'<p style="color:#ff8a8a">{err}</p>' if err else ""
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
    if ADMIN:
        gm = guest_mode_on()
        head = (f"<h1>PiTV Admin</h1><p class=muted>Signed in as {username}</p>"
                f'<div class=card><p class=muted>Guest Mode is '
                f'<b>{"ON" if gm else "OFF"}</b></p><div class=row>'
                f'<button class=on onclick="act(\'guest_on\')">Guest Mode On</button>'
                f'<button class=off onclick="act(\'guest_off\')">Guest Mode Off</button>'
                f'</div></div>')
    else:
        head = (f"<h1>PiTV Guest</h1><p class=muted>Signed in as {username}</p>"
                f"<div class=card><p class=muted>Your game PIN</p>"
                f'<div class=pin>{guest_pin(username)}</div></div>')
    return head + """
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

    def _html(self, body, code=200, cookie=None):
        data = page(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location, cookie=None):
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

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
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode() if length else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/nfc" and not ADMIN:
            qs = parse_qs(urlparse(self.path).query)
            user = user_for_token(qs.get("t", [""])[0])
            if user and guest_mode_on():
                self._redirect("/", self._cookie(sign_session(user), SESSION_MAX_AGE))
            else:
                self._redirect("/")
            return
        if path == "/logout":
            self._redirect("/", f"{COOKIE_NAME}=; Path=/; Max-Age=0")
            return
        if path != "/":
            self.send_response(404); self.end_headers(); return

        if not ADMIN and not guest_mode_on():
            self._html(gate_body()); return
        user = self._session_user()
        self._html(controls_body(user) if user else login_body())

    def do_POST(self):
        path = urlparse(self.path).path
        if not ADMIN and not guest_mode_on():
            self._html(gate_body()); return

        if path == "/login":
            b = self._body()
            user = (check_admin(b.get("username", ""), b.get("password", ""))
                    if ADMIN else check_password(b.get("password", "")))
            if user:
                self._redirect("/", self._cookie(sign_session(user), SESSION_MAX_AGE))
            else:
                self._html(login_body("Wrong login." if ADMIN else "Wrong password."),
                           code=401)
            return

        if path == "/action":
            if not self._session_user():
                self.send_response(403); self.end_headers(); return
            action = self._body().get("action", "")
            if action in NAV:
                write_fifo(NAV[action])
            elif action in UDP:
                send_udp(UDP[action])
            elif ADMIN and action == "guest_on":
                set_guest_mode(True)
            elif ADMIN and action == "guest_off":
                set_guest_mode(False)
            self.send_response(204); self.end_headers()
            return

        self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


def main():
    if not ADMIN:
        maybe_rotate()
    get_secret()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"PiTV {'admin' if ADMIN else 'guest'} portal on :{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
