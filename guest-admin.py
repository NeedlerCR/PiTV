#!/usr/bin/env python3
"""
/opt/pitv/guest-admin.py — manage PiTV guest logins for the web portal.

Invoked by `screen guest ...` and `screen pin guest rotate all`.

  screen guest password set <username> <password>   create/update a guest
  screen guest list                                 list guests + NFC links
  screen guest remove <username>                    delete a guest
  screen pin guest rotate all                       fresh PINs for all guests

Data (device-only, never committed): ~/.pitv/guests.json, ~/.pitv/pins.json
Each guest has a hashed password, an NFC auto-login token, and a player PIN
(shown in the portal, works on the game keypad).
"""

import hashlib
import json
import os
import secrets
import socket
import sys

PITV_DIR       = os.path.expanduser("~/.pitv")
GUEST_FILE     = os.path.join(PITV_DIR, "guests.json")
PIN_FILE       = os.path.join(PITV_DIR, "pins.json")
EMERGENCY_CODE = "159753"


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


def new_pin(used):
    used = set(used) | {EMERGENCY_CODE}
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
    data = load_guests()
    pins = _load(PIN_FILE, {})
    g = data["guests"].get(user, {})
    salt = g.get("salt") or secrets.token_hex(8)
    g["salt"]   = salt
    g["pwhash"] = hashlib.sha256((salt + password).encode()).hexdigest()
    g["token"]  = g.get("token") or secrets.token_urlsafe(16)
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
    data["meta"]["pin_rotated"] = int(__import__("time").time())
    _save(GUEST_FILE, data)
    for user in data["guests"]:
        print(f"- {user}: {pins[user]}")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "password":
        cmd_password_set(args[1:])
    elif cmd == "list":
        cmd_list()
    elif cmd == "remove":
        cmd_remove(args[1:])
    elif cmd == "rotate":
        cmd_rotate()
    else:
        print("Usage: screen guest <password set|list|remove> ...")
        sys.exit(1)


if __name__ == "__main__":
    main()
