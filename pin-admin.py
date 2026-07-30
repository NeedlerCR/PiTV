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
"""

import json
import os
import random
import sys

PIN_FILE       = os.path.expanduser("~/.pitv/pins.json")
EMERGENCY_CODE = "159753"   # reserved; never auto-generate this


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
    used = set(existing.values()) | {EMERGENCY_CODE}
    while True:
        pin = f"{random.randint(0, 999999):06d}"
        if pin not in used:
            return pin


def show(d: dict) -> None:
    if not d:
        print("No PINs assigned yet.  Add one:  screen pin assign <name>")
        return
    for name, pin in d.items():
        print(f"- {name}: {pin}")


def main() -> None:
    args = sys.argv[1:]
    cmd  = args[0].lower() if args else "list"
    d    = load()

    if cmd == "list":
        show(d)

    elif cmd == "assign":
        if len(args) < 2:
            print("Usage: screen pin assign <name>"); sys.exit(1)
        name = " ".join(args[1:])
        pin  = d.get(name) or new_pin(d)
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
