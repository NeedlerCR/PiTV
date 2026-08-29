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
import time

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


# ── authenticated control channel ────────────────────────────────────
#
# The UDP channel on 127.0.0.1:8129 used to accept any datagram that reached
# it: every command (TV power, the kill switch, guest mode, menu keys) was
# taken on trust. Now each datagram carries an HMAC over a timestamp and a
# one-shot nonce, so only a sender holding the shared key can drive the TV, and
# a captured datagram can't be replayed.
#
# The key is shared with Homebridge, which runs as a DIFFERENT user, so it
# lives in /etc/pitv/control.key (group-readable) when that exists; deploy.sh
# creates it and puts both users in the 'pitv' group. ~/.pitv/control-key is
# the fallback for everything running as the PiTV user.
CONTROL_KEY_SYSTEM = "/etc/pitv/control.key"
CONTROL_KEY_USER   = os.path.join(PITV_DIR, "control-key")
CONTROL_PREFIX     = "PITV1"
# How far a datagram's clock may be from ours. Everything is on one box, so
# this only needs to cover scheduling jitter.
CONTROL_MAX_SKEW   = 120


def control_key_path() -> str:
    """Where the shared control key is (or should be) read from."""
    env = os.environ.get("PITV_CONTROL_KEY_FILE")
    if env:
        return env
    if os.path.exists(CONTROL_KEY_SYSTEM):
        return CONTROL_KEY_SYSTEM
    return CONTROL_KEY_USER


def control_key() -> bytes:
    """The shared key, creating a user-level one the first time. Returns b"" if
    it can't be read AND can't be created — callers treat that as 'no auth
    possible' and say so loudly rather than silently accepting anything."""
    path = control_key_path()
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key.encode()
    except OSError:
        pass
    if path != CONTROL_KEY_USER:
        return b""                      # a system key we can't read: don't mint one
    key = secrets.token_hex(32)
    try:
        os.makedirs(PITV_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(key + "\n")
        os.chmod(path, 0o600)
    except OSError:
        return b""
    return key.encode()


def key_fingerprint(key: bytes) -> str:
    """A short, non-reversible id for a key, so two machines can be compared
    without either printing the secret."""
    if not key:
        return "none"
    return hashlib.sha256(key).hexdigest()[:12]


def sign_command(payload: str, key: bytes = b"") -> str:
    """Wrap a command as 'PITV1 <ts> <nonce> <sig> <payload>'."""
    key = key or control_key()
    ts    = str(int(time.time()))
    nonce = secrets.token_hex(8)
    sig   = hmac.new(key, f"{ts}|{nonce}|{payload}".encode(),
                     hashlib.sha256).hexdigest()
    return f"{CONTROL_PREFIX} {ts} {nonce} {sig} {payload}"


class CommandVerifier:
    """Checks signed commands and remembers nonces so none is accepted twice."""

    def __init__(self, key: bytes = b""):
        self._pinned = bool(key)        # a key passed in is never reloaded
        self.key     = key or control_key()
        self._seen   = {}               # nonce -> expiry
        self._stamp  = self._key_stamp()

    def _key_stamp(self):
        try:
            st = os.stat(control_key_path())
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def maybe_reload(self) -> bool:
        """Re-read the key if the file changed — so `screen control key rotate`
        (and deploy.sh writing /etc/pitv/control.key for the first time) apply
        without restarting the menu. Returns True if the key changed."""
        if self._pinned:
            return False
        stamp = self._key_stamp()
        if stamp == self._stamp and self.key:
            return False
        self._stamp = stamp
        new = control_key()
        if new != self.key:
            self.key = new
            self._seen.clear()
            return True
        return False

    def _reap(self, now):
        if len(self._seen) < 256:
            return
        for nonce, exp in list(self._seen.items()):
            if exp < now:
                self._seen.pop(nonce, None)

    def verify(self, raw: str) -> tuple[str, str]:
        """Returns (payload, "") when good, or ("", reason) when not."""
        if not self.key:
            return "", "no control key available"
        parts = raw.split(" ", 4)
        if len(parts) < 5 or parts[0] != CONTROL_PREFIX:
            return "", "unsigned or malformed"
        _, ts, nonce, sig, payload = parts
        try:
            age = abs(time.time() - int(ts))
        except ValueError:
            return "", "bad timestamp"
        if age > CONTROL_MAX_SKEW:
            return "", "stale timestamp"
        good = hmac.new(self.key, f"{ts}|{nonce}|{payload}".encode(),
                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return "", "bad signature"
        now = time.time()
        if nonce in self._seen:
            return "", "replayed nonce"
        self._reap(now)
        self._seen[nonce] = now + CONTROL_MAX_SKEW * 2
        return payload, ""
