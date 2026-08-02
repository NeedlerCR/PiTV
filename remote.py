#!/usr/bin/env python3
"""
/opt/pitv/remote.py

Interactive SSH keyboard remote for PiTV.
Run via:  screen remote on

Reads keystrokes from the SSH terminal and forwards them to the
tv_menu.py FIFO pipe.

In the MENU, keys navigate:
  Arrow keys         →  UP / DOWN / LEFT / RIGHT
  Enter              →  SELECT
  Backspace / Delete →  BACK

While an EXTERNAL game is running, EVERY key is forwarded straight into
the game (tv_menu injects it as a real keystroke via uinput), so you can
type numbers, letters and punctuation — e.g. pick a grid size in
Minesweeper, enter digits in Sudoku, or play NetHack over SSH:
  Arrow keys → movement      Space → space (reveal / fire)
  Enter → enter              letters / digits / punctuation → typed as-is
  Backspace → back / Esc     (to delete in Sudoku, press 'x')

  ESC, Ctrl+C or Ctrl+]  →  exit remote mode
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
    print("  Menu: arrow keys navigate, Enter selects, Backspace goes back.")
    print("  In a game: every key is passed straight through.")
    print("  ESC / Ctrl+C / Ctrl+] = exit remote mode")
    print()

    try:
        while True:
            key = read_key()

            # Exit conditions: bare ESC, Ctrl+C, Ctrl+]
            if key in (b"\x1b", b"\x03", b"\x1d"):
                break

            # Arrow keys (ESC [ A/B/C/D)
            elif key == b"\x1b[A":
                send("UP")
            elif key == b"\x1b[B":
                send("DOWN")
            elif key == b"\x1b[C":
                send("RIGHT")
            elif key == b"\x1b[D":
                send("LEFT")

            # Enter / space / backspace / tab as named keys
            elif key in (b"\r", b"\n"):
                send("ENTER")
            elif key == b" ":
                send("SPACE")
            elif key in (b"\x7f", b"\x08"):
                send("BACK")
            elif key == b"\t":
                send("TAB")

            # Any other single printable character → typed into the game.
            elif len(key) == 1 and 0x21 <= key[0] <= 0x7e:
                send("TYPE " + key.decode("ascii"))

    except KeyboardInterrupt:
        pass

    print("\nRemote mode exited.")


if __name__ == "__main__":
    main()
