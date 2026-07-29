#!/usr/bin/env python3
"""
/opt/pitv/remote.py

Interactive SSH keyboard remote for PiTV.
Run via:  screen remote on

Reads keystrokes from the SSH terminal and forwards them to the
tv_menu.py FIFO pipe as navigation commands.

  Arrow keys / WASD  →  UP / DOWN / LEFT / RIGHT
  Enter              →  SELECT
  Backspace / Delete →  BACK
  ESC or Ctrl+C      →  exit remote mode

Games running on tty1 handle their own input from the physical
keyboard / wireless controller.  This remote only controls the
PiTV menu and lets you exit a game via BACK.
"""

import os
import sys
import termios
import tty

FIFO = "/tmp/tv_menu.fifo"


def send(cmd: str) -> None:
    try:
        with open(FIFO, "w") as f:
            f.write(cmd + "\n")
    except OSError:
        pass


def read_key() -> bytes:
    """Read one keypress, including multi-byte escape sequences."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            # Try to read the rest of an escape sequence non-blockingly
            os.set_blocking(fd, False)
            try:
                rest = os.read(fd, 4)
                ch = ch + rest
            except (BlockingIOError, OSError):
                pass
            finally:
                os.set_blocking(fd, True)
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main() -> None:
    if not os.path.exists(FIFO):
        print(f"Error: FIFO not found at {FIFO}")
        print("Is tv_menu.py running?  Check: systemctl status pitv-menu")
        sys.exit(1)

    print("PiTV Remote — active")
    print("  Arrow keys / WASD = navigate   Enter = select")
    print("  Backspace = back               ESC = exit remote mode")
    print()

    try:
        while True:
            key = read_key()

            # Exit conditions
            if key in (b"\x1b", b"\x03", b"q", b"Q"):
                # Bare ESC (no following bytes) or Ctrl+C
                if key == b"\x1b":
                    break
                break

            # Arrow keys (ESC [ A/B/C/D)
            elif key in (b"\x1b[A", b"w", b"W"):
                send("UP")
            elif key in (b"\x1b[B", b"s", b"S"):
                send("DOWN")
            elif key in (b"\x1b[C", b"d", b"D"):
                send("RIGHT")
            elif key in (b"\x1b[D", b"a", b"A"):
                send("LEFT")

            # Confirm / select
            elif key in (b"\r", b"\n"):
                send("SELECT")

            # Back / cancel
            elif key in (b"\x7f", b"\x08"):
                send("BACK")

            # Ctrl+C fallback
            elif key == b"\x03":
                break

    except KeyboardInterrupt:
        pass

    print("\nRemote mode exited.")


if __name__ == "__main__":
    main()
