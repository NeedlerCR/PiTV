#!/usr/bin/env python3
"""
/opt/pitv/guest-admin.py — manage PiTV guest logins for the web portal.

Invoked by `screen guest ...` and `screen pin guest rotate all`.

  screen guest password set <username> <password>   create/update a guest
  screen guest list                                 list guests + NFC links
  screen guest remove <username>                    delete a guest
  screen guest code [show]                          the connection password
  screen guest lock on|off|status                   pause the guests' buttons
  screen pin guest rotate all                       fresh PINs for all guests

Data (device-only, never committed): ~/.pitv/guests.json, ~/.pitv/pins.json
Each guest has a PBKDF2-hashed password (pitv_secrets.hash_password), an NFC
auto-login token, and a player PIN (shown in the portal, works on the game
keypad).
"""

import json
import os
import secrets
import socket
import sys
import time

import pitv_secrets

PITV_DIR       = os.path.expanduser("~/.pitv")
GUEST_FILE     = os.path.join(PITV_DIR, "guests.json")
ADMIN_FILE     = os.path.join(PITV_DIR, "admin.json")
PIN_FILE       = os.path.join(PITV_DIR, "pins.json")
GUEST_SECRET   = os.path.join(PITV_DIR, "portal-secret")
PAIR_FILE      = os.path.join(PITV_DIR, "pairing.json")
GUEST_MODE     = "/tmp/pitv-guest-mode"
GUEST_LOCK     = "/tmp/pitv-guest-lock"

# A username goes into the signed session cookie as "user|expiry", so a "|" in
# one would make the cookie ambiguous. Everything else is fine — the portal
# HTML-escapes names now — so this stays narrow enough not to reject a real
# name like "Anna's iPad".
BAD_NAME_CHARS = set('|\r\n\t')


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


def valid_name(name):
    return bool(name) and len(name) <= 40 and not (set(name) & BAD_NAME_CHARS)


def new_pin(used):
    used = set(used) | {pitv_secrets.emergency_code()}
    while True:
        p = f"{secrets.randbelow(1000000):06d}"
        if p not in used:
            return p


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "<pi-ip>"


def nfc_link(token):
    return f"http://{local_ip()}:8080/nfc?t={token}"


def cmd_password_set(args):
    # args: ["set", <username>, <password>]
    if len(args) < 3 or args[0] != "set":
        print("Usage: screen guest password set <username> <password>")
        sys.exit(1)
    user, password = args[1], args[2]
    if not valid_name(user):
        print("Username can't contain '|', a tab or a newline, and must be at "
              "most 40 characters."); sys.exit(1)
    if len(password) < 6:
        print("Password must be at least 6 characters."); sys.exit(1)
    data = load_guests()
    pins = _load(PIN_FILE, {})
    g = data["guests"].get(user, {})
    g.update(pitv_secrets.hash_password(password))   # replaces salt + hash
    g["token"]  = g.get("token") or secrets.token_urlsafe(32)
    data["guests"][user] = g
    if user not in pins:
        pins[user] = new_pin(pins.values())
        _save(PIN_FILE, pins)
    _save(GUEST_FILE, data)
    print(f"Guest '{user}' set.  PIN: {pins[user]}")
    print(f"NFC link: {nfc_link(g['token'])}")


def cmd_list():
    data = load_guests()
    pins = _load(PIN_FILE, {})
    if not data["guests"]:
        print("No guests yet.  Add one: screen guest password set <name> <password>")
        return
    for user, g in data["guests"].items():
        print(f"- {user}: PIN {pins.get(user, '—')}   NFC {nfc_link(g.get('token',''))}")


def cmd_remove(args):
    if not args:
        print("Usage: screen guest remove <username>"); sys.exit(1)
    user = args[0]
    data = load_guests()
    if data["guests"].pop(user, None) is None:
        print(f"No guest '{user}'."); sys.exit(1)
    _save(GUEST_FILE, data)
    pins = _load(PIN_FILE, {})
    if pins.pop(user, None) is not None:
        _save(PIN_FILE, pins)
    print(f"Removed guest '{user}'.")


def cmd_rotate():
    data = load_guests()
    pins = _load(PIN_FILE, {})
    if not data["guests"]:
        print("No guests to rotate."); return
    for user in data["guests"]:
        pins[user] = new_pin(pins.values())
    _save(PIN_FILE, pins)
    data["meta"]["pin_rotated"] = int(time.time())
    _save(GUEST_FILE, data)
    for user in data["guests"]:
        print(f"- {user}: {pins[user]}")


def cmd_admin_set(args):
    # args: [<username>, <password>]
    if len(args) < 2:
        print("Usage: screen admin password set <username> <password>"); sys.exit(1)
    user, password = args[0], args[1]
    if not valid_name(user):
        print("Username can't contain '|', a tab or a newline, and must be at "
              "most 40 characters."); sys.exit(1)
    if len(password) < 8:
        print("Admin password must be at least 8 characters."); sys.exit(1)
    admins = _load(ADMIN_FILE, {})
    admins[user] = pitv_secrets.hash_password(password)
    _save(ADMIN_FILE, admins)
    print(f"Admin '{user}' set.  Portal: http://{local_ip()}/  (raspberrypi.local)")


def cmd_admin_list():
    admins = _load(ADMIN_FILE, {})
    if not admins:
        print("No admins yet.  Add one: screen admin password set <name> <password>")
        return
    for user in admins:
        print(f"- {user}")


def cmd_admin_remove(args):
    if not args:
        print("Usage: screen admin remove <username>"); sys.exit(1)
    admins = _load(ADMIN_FILE, {})
    if admins.pop(args[0], None) is None:
        print(f"No admin '{args[0]}'."); sys.exit(1)
    _save(ADMIN_FILE, admins)
    print(f"Removed admin '{args[0]}'.")


def _guest_mode_on():
    try:
        with open(GUEST_MODE) as f:
            return f.read().strip().lower() == "on"
    except OSError:
        return False


def cmd_code():
    """What the guest is asked for after their password: a code shown on the
    TV. Guest Mode is its switch — there is no separate one."""
    if not _guest_mode_on():
        print("Guest Mode is OFF, so there is no connection password.")
        print("Guests can't sign in at all until it is on (Home app, the admin")
        print("portal, or: screen guest lock is a softer pause).")
        return
    d = _load(PAIR_FILE, {})
    left = int(d.get("expires", 0) - time.time())
    print("Guest Mode is ON — guests sign in with their password, then the")
    print("connection password shown on the TV.")
    if d.get("code") and left > 0:
        print(f"  Code now  : {d['code']}   (changes in {left}s)")
    else:
        print("  Code now  : none live — one is minted when a guest asks")
    if d.get("show_until", 0) > time.time():
        print("  On the TV : yes, right now")
    print("  Put it on the TV with:  screen guest code show")


def cmd_lock(args):
    """The admin Lock switch, from the CLI. Guests stay signed in; every button
    greys out until it is lifted."""
    sub = (args[0] if args else "status").lower()
    if sub in ("on", "off"):
        try:
            with open(GUEST_LOCK, "w") as f:
                f.write("on" if sub == "on" else "off")
        except OSError as e:
            print(f"Could not write {GUEST_LOCK}: {e}"); sys.exit(1)
        print("Guest controls LOCKED — their buttons are greyed out."
              if sub == "on" else
              "Guest controls unlocked.")
        print("Open guest pages update within a few seconds.")
        return
    if sub == "status":
        try:
            with open(GUEST_LOCK) as f:
                on = f.read().strip().lower() == "on"
        except OSError:
            on = False
        print("Guest controls: " + ("LOCKED" if on else "unlocked"))
        return
    print("Usage: screen guest lock <on|off|status>"); sys.exit(1)


def cmd_kick():
    """Sign every guest out (even offline ones) by rotating the guest session
    key — all existing guest cookies become invalid. Admins are unaffected."""
    os.makedirs(PITV_DIR, exist_ok=True)
    with open(GUEST_SECRET, "wb") as f:
        f.write(secrets.token_bytes(32))
    try:
        os.chmod(GUEST_SECRET, 0o600)
    except OSError:
        pass
    print("All guests signed out (guest session key rotated).")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "password":
        cmd_password_set(args[1:])
    elif cmd == "kick":
        cmd_kick()
    elif cmd == "list":
        cmd_list()
    elif cmd == "remove":
        cmd_remove(args[1:])
    elif cmd == "rotate":
        cmd_rotate()
    elif cmd == "code":
        cmd_code()
    elif cmd == "lock":
        cmd_lock(args[1:])
    elif cmd == "admin-set":
        cmd_admin_set(args[1:])
    elif cmd == "admin-list":
        cmd_admin_list()
    elif cmd == "admin-remove":
        cmd_admin_remove(args[1:])
    else:
        print("Usage: screen guest <password set|list|remove|kick all|"
              "code|lock> ...")
        sys.exit(1)


if __name__ == "__main__":
    main()
