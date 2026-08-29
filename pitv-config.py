#!/usr/bin/env python3
"""
/opt/pitv/pitv-config.py — local settings the `screen` CLI edits directly.

  screen sky status                  Sky Q control: on/off, device, routing
  screen pair status                 the guest pairing code shown on the TV
  screen control status              the signed-command channel
  screen control key show|rotate     the shared control key
  screen network status              who may reach the web portals
  screen network allow <cidr>        add an allowed client range
  screen network clear               allow any address again

Sky Q switching ON/OFF goes through the FIFO instead (tv_menu owns that state
while it's running); this is the read-only/system half.
"""

import grp
import json
import os
import pwd
import stat
import sys

import pitv_secrets

PITV_DIR     = os.path.expanduser("~/.pitv")
SKY_FILE     = os.path.join(PITV_DIR, "sky.json")
PAIR_FILE    = os.path.join(PITV_DIR, "pairing.json")
NETWORK_FILE = os.path.join(PITV_DIR, "network.json")
TV_INPUT     = "/tmp/pitv-tv-input"
SKY_PHYS     = "10:00"


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


# ── sky ──────────────────────────────────────────────────────────────
def cmd_sky(args):
    d = _load(SKY_FILE, {})
    on = bool(d.get("enabled"))
    print(f"Sky Q control: {'ON' if on else 'OFF (default)'}")
    if not on:
        print("Turn it on with:  screen sky on")
        return
    print(f"  CEC device number : {d.get('logical', 3)}")
    print(f"  Route remote keys : {'always' if d.get('always') else 'only while the TV is on the Sky input'}")
    try:
        with open(TV_INPUT) as f:
            phys = f.read().strip()
    except OSError:
        phys = ""
    where = ("Sky" if (d.get("always") or phys == SKY_PHYS) else "the PiTV menu")
    seen  = phys or "unknown"
    print(f"  TV input now      : {seen}")
    print(f"  Nav keys go to    : {where}")


# ── pairing ──────────────────────────────────────────────────────────
def cmd_pair(args):
    import time
    d = _load(PAIR_FILE, {})
    if not d.get("enabled", True):
        print("Guest pairing: OFF")
        print("Turn it back on with:  screen pair on")
        return
    print("Guest pairing: ON — guests sign in with the code on the TV")
    left = int(d.get("expires", 0) - time.time())
    if d.get("code") and left > 0:
        print(f"  Code now      : {d['code']}  (changes in {left}s)")
    else:
        print("  Code now      : none yet — it appears when the menu next draws")
    if d.get("show_until", 0) > time.time():
        print("  On screen     : yes, right now")
    print("  Session length: 6 hours, then the guest pairs again")
    print("  Put it on the TV with:  screen pair show")


# ── control key ──────────────────────────────────────────────────────
def _readers_of(path):
    """Which users can read the key file, in plain words."""
    try:
        st = os.stat(path)
    except OSError:
        return "the file does not exist yet"
    mode = stat.S_IMODE(st.st_mode)
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    who = [f"{owner} (owner)"]
    if mode & stat.S_IRGRP:
        try:
            members = grp.getgrnam(group).gr_mem
        except KeyError:
            members = []
        who.append(f"group {group}" + (f" [{', '.join(members)}]" if members else ""))
    if mode & stat.S_IROTH:
        who.append("EVERYONE ON THE BOX — tighten this")
    return f"{oct(mode)} {owner}:{group} — readable by " + ", ".join(who)


def cmd_control(args):
    sub = args[0] if args else "status"
    path = pitv_secrets.control_key_path()
    if sub == "status":
        key = pitv_secrets.control_key()
        print("Signed control channel (UDP 127.0.0.1:8129)")
        print(f"  Key file    : {path}")
        print(f"  Permissions : {_readers_of(path)}")
        print(f"  Fingerprint : {pitv_secrets.key_fingerprint(key)}")
        if not key:
            print("  !! No readable key: tv_menu refuses every command. "
                  "Run ./deploy.sh")
            sys.exit(1)
        hb = _homebridge_user()
        if hb:
            ok = _can_read(path, hb)
            print(f"  Homebridge  : user '{hb}' "
                  + ("can read the key" if ok else
                     "CANNOT read the key — the Home app's TV tile won't work. "
                     "Run ./deploy.sh, or paste the key into the plugin config"))
        print("  Rejections  : see `screen log` for 'Control UDP: rejected'")
        return
    if sub == "key":
        what = args[1] if len(args) > 1 else "show"
        if what == "show":
            key = pitv_secrets.control_key()
            if not key:
                print(f"No readable key at {path}."); sys.exit(1)
            print(key.decode())
            return
        if what == "rotate":
            import secrets as _secrets
            new = _secrets.token_hex(32)
            try:
                with open(path, "w") as f:
                    f.write(new + "\n")
            except OSError as e:
                print(f"Could not write {path}: {e}")
                print("Try:  sudo ./deploy.sh"); sys.exit(1)
            print(f"New key written to {path} "
                  f"(fingerprint {pitv_secrets.key_fingerprint(new.encode())}).")
            print("tv_menu picks it up on its own. Restart Homebridge so the "
                  "plugin re-reads it:  sudo hb-service restart")
            return
    print("Usage: screen control <status|key show|key rotate>")
    sys.exit(1)


def _homebridge_user():
    """Best guess at the user Homebridge runs as, for the readability check."""
    for unit in ("/etc/systemd/system/homebridge.service",
                 "/lib/systemd/system/homebridge.service"):
        try:
            for line in open(unit):
                if line.strip().startswith("User="):
                    return line.strip().split("=", 1)[1].strip()
        except OSError:
            continue
    try:
        pwd.getpwnam("homebridge")
        return "homebridge"
    except KeyError:
        return ""


def _can_read(path, user):
    try:
        st = os.stat(path)
        pw = pwd.getpwnam(user)
    except (OSError, KeyError):
        return False
    mode = stat.S_IMODE(st.st_mode)
    if pw.pw_uid == st.st_uid:
        return bool(mode & stat.S_IRUSR)
    groups = {g.gr_gid for g in grp.getgrall() if user in g.gr_mem}
    groups.add(pw.pw_gid)
    if st.st_gid in groups:
        return bool(mode & stat.S_IRGRP)
    return bool(mode & stat.S_IROTH)


# ── network allowlist ────────────────────────────────────────────────
def cmd_network(args):
    import ipaddress
    sub = args[0] if args else "status"
    data = _load(NETWORK_FILE, {})
    allow = data.get("allow", [])
    if sub == "status":
        if not allow:
            print("Web portals: reachable from ANY address that can route to "
                  "the Pi (the default).")
            print("Restrict to your LAN with:  screen network allow "
                  "192.168.1.0/24")
        else:
            print("Web portals: only these client addresses are accepted —")
            for a in allow:
                print(f"  {a}")
            print("Everything else is dropped before the login page is served.")
        return
    if sub == "allow":
        cidr = args[1] if len(args) > 1 else ""
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            print("Usage: screen network allow <cidr>   e.g. 192.168.1.0/24")
            sys.exit(1)
        if str(net) not in allow:
            allow.append(str(net))
        data["allow"] = allow
        _save(NETWORK_FILE, data)
        print(f"Allowed {net}. Now: " + ", ".join(allow))
        print("Restart the portals to apply immediately:  "
              "sudo systemctl restart pitv-guest pitv-admin")
        return
    if sub == "clear":
        data["allow"] = []
        _save(NETWORK_FILE, data)
        print("Allowlist cleared — any address may reach the portals again.")
        return
    print("Usage: screen network <status|allow <cidr>|clear>")
    sys.exit(1)


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "sky":
        cmd_sky(args[1:])
    elif cmd == "pair":
        cmd_pair(args[1:])
    elif cmd == "control":
        cmd_control(args[1:])
    elif cmd == "network":
        cmd_network(args[1:])
    else:
        print("Usage: pitv-config.py <sky|control|network> ...")
        sys.exit(1)


if __name__ == "__main__":
    main()
