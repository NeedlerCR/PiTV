#!/usr/bin/env python3
"""
/opt/pitv/pin-admin.py — manage PiTV player PINs.

Invoked by the `screen pin ...` CLI. PINs live in ~/.pitv/pins.json (device
only, never committed) as {name: "6-digit PIN"}. tv_menu.py reads the same
file to unlock locked games, logging who used which PIN.

  screen pin assign <name>     create (or show) a person's PIN
  screen pin list              list everyone's PIN
  screen pin remove <name>     delete a person's PIN
  screen pin revoke <name>     alias for remove
  screen pin rename <old> <new>

Also owns the emergency (master) code, which lives device-only in
~/.pitv/emergency-code:

  screen emergency status      is it still the code published in git?
  screen emergency set <code>  set a new 6-digit master code
"""

import json
import os
import secrets
import sys

import pitv_secrets

PIN_FILE = os.path.expanduser("~/.pitv/pins.json")


def load() -> dict:
    try:
        with open(PIN_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(d: dict) -> None:
    os.makedirs(os.path.dirname(PIN_FILE), exist_ok=True)
    with open(PIN_FILE, "w") as f:
        json.dump(d, f, indent=2)
    try:
        os.chmod(PIN_FILE, 0o600)
    except OSError:
        pass


def new_pin(existing: dict) -> str:
    """A fresh PIN nobody else holds. `secrets` rather than `random`: these
    unlock the games, and random's Mersenne Twister is predictable from a
    handful of outputs — which `screen pin list` hands you."""
    used = set(existing.values()) | {pitv_secrets.emergency_code()}
    while True:
        pin = f"{secrets.randbelow(1000000):06d}"
        if pin not in used:
            return pin


def cmd_emergency(args) -> None:
    """screen emergency status | set <6 digits>"""
    sub = args[0].lower() if args else "status"
    if sub == "status":
        if pitv_secrets.is_default_emergency_code():
            print("Emergency code: STILL THE DEFAULT that is published in git.")
            print("Set your own now:  screen emergency set <6 digits>")
            sys.exit(1)
        print(f"Emergency code: set (stored in {pitv_secrets.EMERGENCY_FILE}).")
        return
    if sub == "set":
        code = args[1] if len(args) > 1 else ""
        if not (code.isdigit() and len(code) == 6):
            print("The emergency code must be exactly 6 digits."); sys.exit(1)
        clash = next((n for n, p in load().items() if p == code), None)
        if clash:
            print(f"That code is {clash}'s player PIN — pick another."); sys.exit(1)
        if not pitv_secrets.set_emergency_code(code):
            print(f"Could not write {pitv_secrets.EMERGENCY_FILE}."); sys.exit(1)
        print("Emergency code updated. It takes effect immediately — no restart.")
        return
    print("Usage: screen emergency <status|set <6 digits>>"); sys.exit(1)


def show(d: dict) -> None:
    if not d:
        print("No PINs assigned yet.  Add one:  screen pin assign <name>")
        return
    for name, pin in d.items():
        print(f"- {name}: {pin}")


def main() -> None:
    args = sys.argv[1:]
    cmd  = args[0].lower() if args else "list"

    if cmd == "emergency":
        cmd_emergency(args[1:])
        return

    d = load()

    if cmd == "list":
        show(d)

    elif cmd == "assign":
        rest = args[1:]
        custom_pin = None
        if "custom" in rest:                       # assign <name> custom <pin>
            ci = rest.index("custom")
            name = " ".join(rest[:ci])
            custom_pin = rest[ci + 1] if len(rest) > ci + 1 else ""
        else:
            name = " ".join(rest)
        if not name:
            print("Usage: screen pin assign <name> [custom <pin>]"); sys.exit(1)
        if custom_pin is not None:
            if not (custom_pin.isdigit() and len(custom_pin) == 6):
                print("Custom PIN must be exactly 6 digits."); sys.exit(1)
            if custom_pin == pitv_secrets.emergency_code():
                print("That PIN is reserved (emergency code)."); sys.exit(1)
            clash = next((n for n, p in d.items() if p == custom_pin and n != name), None)
            if clash:
                print(f"PIN already used by {clash}."); sys.exit(1)
            pin = custom_pin
        else:
            pin = d.get(name) or new_pin(d)
        d[name] = pin
        save(d)
        print(f"- {name}: {pin}")

    elif cmd in ("remove", "revoke"):
        if len(args) < 2:
            print(f"Usage: screen pin {cmd} <name>"); sys.exit(1)
        name = " ".join(args[1:])
        if d.pop(name, None) is None:
            print(f"No PIN for '{name}'."); sys.exit(1)
        save(d)
        print(f"Removed {name}.")

    elif cmd == "rename":
        if len(args) < 3:
            print("Usage: screen pin rename <old> <new>"); sys.exit(1)
        old, new = args[1], args[2]
        if old not in d:
            print(f"No PIN for '{old}'."); sys.exit(1)
        d[new] = d.pop(old)
        save(d)
        print(f"Renamed {old} -> {new}.")

    else:
        print("Usage: screen pin <assign|list|remove|revoke|rename> <name>")
        sys.exit(1)


if __name__ == "__main__":
    main()
