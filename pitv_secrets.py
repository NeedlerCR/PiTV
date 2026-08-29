#!/usr/bin/env python3
"""
/opt/pitv/pitv_secrets.py — shared secret handling for PiTV.

One place for the two things every other script needs to agree on:

  * the **emergency code** (the master that unlocks a locked keypad and a full
    lockdown). It lives in ~/.pitv/emergency-code — device-only, never
    committed — and is read fresh on every use, so `screen emergency set`
    applies without restarting anything.
  * **password hashing** for the guest and admin portals: PBKDF2-HMAC-SHA256
    with a per-record salt, replacing the single unsalted-round SHA-256 that
    was there before. Old records still verify and are upgraded in place the
    next time that person signs in, so nobody has to reset a password.

Imported by tv_menu.py, guest-portal.py, guest-admin.py and pin-admin.py; all
of them run out of /opt/pitv, so a plain import finds this file.
"""

import hashlib
import hmac
import os
import secrets

PITV_DIR       = os.path.expanduser("~/.pitv")
EMERGENCY_FILE = os.path.join(PITV_DIR, "emergency-code")

# The code PiTV shipped with. It sat hardcoded in three files that are in git,
# so treat it as public: it stays only as the seed for the device-only file, so
# an existing Pi keeps working across this upgrade. Rotate it with
#     screen emergency set <new 6-digit code>
# after deploying. `is_default_emergency_code()` is what nags about it.
LEGACY_EMERGENCY_CODE = "159753"

# PBKDF2 rounds. The Zero 2 W is slow, and the guest portal may hash once per
# stored guest per login attempt, so this is a compromise between "costly to
# crack" and "signs in before the guest gives up". The portal's per-IP lockout
# is what actually caps how often an attacker can pay this cost.
PBKDF2_ITERATIONS = 120_000


# ── emergency code ───────────────────────────────────────────────────
def _read_code():
    try:
        with open(EMERGENCY_FILE) as f:
            code = f.read().strip()
        return code if _valid_code(code) else None
    except OSError:
        return None


def _valid_code(code):
    return bool(code) and code.isdigit() and len(code) == 6


def emergency_code() -> str:
    """The current emergency code. Seeds the device-only file with the legacy
    code the first time, so upgrading a Pi never leaves it with no way out of a
    lockdown."""
    code = _read_code()
    if code:
        return code
    set_emergency_code(LEGACY_EMERGENCY_CODE)
    return LEGACY_EMERGENCY_CODE


def set_emergency_code(code: str) -> bool:
    """Write a new 6-digit emergency code. Returns False if it isn't 6 digits."""
    if not _valid_code(code):
        return False
    try:
        os.makedirs(PITV_DIR, exist_ok=True)
        with open(EMERGENCY_FILE, "w") as f:
            f.write(code + "\n")
        os.chmod(EMERGENCY_FILE, 0o600)
    except OSError:
        return False
    return True


def is_default_emergency_code() -> bool:
    """True while the Pi is still using the code that's public in git."""
    return hmac.compare_digest(emergency_code(), LEGACY_EMERGENCY_CODE)


def check_emergency_code(entered: str) -> bool:
    """Constant-time comparison against the current emergency code."""
    if not entered:
        return False
    return hmac.compare_digest(entered, emergency_code())


# ── password hashing ─────────────────────────────────────────────────
def hash_password(password: str, salt: str = "") -> dict:
    """A fresh PBKDF2 record: {"algo","salt","iters","pwhash"}."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                             PBKDF2_ITERATIONS)
    return {
        "algo":   "pbkdf2_sha256",
        "salt":   salt,
        "iters":  PBKDF2_ITERATIONS,
        "pwhash": dk.hex(),
    }


def verify_password(rec: dict, password: str) -> tuple[bool, bool]:
    """Check a password against a stored record.

    Returns (ok, needs_upgrade). needs_upgrade is True when the record verified
    but is in an old format (or a weaker cost), so the caller can re-hash and
    save it while it holds the plaintext."""
    if not isinstance(rec, dict):
        return False, False
    salt = rec.get("salt", "")
    stored = rec.get("pwhash", "")
    if not stored:
        return False, False
    algo = rec.get("algo", "")
    try:
        if algo == "pbkdf2_sha256":
            iters = int(rec.get("iters", PBKDF2_ITERATIONS))
            calc = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                       salt.encode(), iters).hex()
            ok = hmac.compare_digest(calc, stored)
            return ok, ok and iters < PBKDF2_ITERATIONS
        # Legacy: one unsalted-cost round of sha256(salt + password).
        calc = hashlib.sha256((salt + password).encode()).hexdigest()
        ok = hmac.compare_digest(calc, stored)
        return ok, ok
    except Exception:
        return False, False
