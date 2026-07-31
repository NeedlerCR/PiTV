# PiTV — project guide

A Raspberry Pi Zero 2 W wired to a TV that boots straight into a full-screen
curses menu (screensavers, retro games, AirPlay screen mirroring) and exposes
the TV's power to Apple HomeKit over HDMI-CEC.

Versioning is **ChronosVer** (`vYYYY.MAJOR.MINOR.BUG`); the canonical value is
in `VERSION` (and `tv_menu.py`'s `VERSION`), shown by `screen version`.

Security escalation: 3 wrong game PINs → lockout keypad; 3 wrong tries there →
**full lockdown** (`hard_locked`): the TV is forced off every 7 s and the
screen is blocked. Only `screen unlock authorise <emergency code>` clears it
(kill switch off, TV on, input → PiTV).

## Git settings

- Git author + Commitor + anything else: NeedlerCR <jackdaw.juncos-8n@icloud.com>
- Commits: clean single line. No chat links, URLs, or trailers.

## Runtime layout

- The menu is `tv_menu.py`, run by the **`pitv-menu`** systemd service
  (`pitv-menu.service`) as user **`charlieneedler`** on **tty1**
  (`TERM=linux`, no X11 — it draws to the Linux framebuffer console).
- Deployed copy lives in **`/opt/pitv/`**. Deploy with **`./deploy.sh`**
  (copies `*.sh *.py *.service` + the plugin folder to `/opt/pitv`, reloads
  the service, offers a reboot).
- Logs: `/tmp/pitv.log` (menu), `/tmp/pitv-mapper.log` (controller mapper),
  `/tmp/pitv-game.log` (last game's stderr), `/tmp/uxplay.log` (mirroring).

## Input paths (how the menu and games are driven)

1. **`tv_menu.py` controller listener** — reads gamepads directly via evdev
   and pushes menu tokens (UP/DOWN/SELECT/BACK/HOME…) into `input_queue`.
   `controller_mode` (`MENU` / `GAME_INTERNAL` / `GAME_EXTERNAL` / `KEYPAD`)
   gates what it injects. Built-in games (Snake) read `input_queue` directly.
2. **`controller-to-keys.py`** — for external games, turns up to **two**
   gamepads into real keyboard events via **`/dev/uinput`**. Player 1 → arrows
   + Space/Enter/Esc; Player 2 → WASD + F/G (for 2-player games like
   vitetris). Started by `run_game()` for every external game.
3. **CEC remote** (`listen_cec_remote`) and the **FIFO** `/tmp/tv_menu.fifo`
   (written by the `screen` CLI and `remote.py`) also feed `input_queue`.

> **Critical setup:** the service user needs access to `/dev/uinput` and
> `/dev/input/event*`, or key injection silently fails and games ignore the
> controller. Run **`./setup-input.sh`** once, then reboot. Symptoms of it not
> being done: menu works, but games don't respond to the pad.
> `remote.py` (`screen remote on`) only drives the *menu* — it can't act as a
> second player inside a game. Use a real USB keyboard or a 2nd gamepad for P2.

## Games

Defined in `GAME_KEYS` / `GAME_OPTIONS` in `tv_menu.py`. `resolve_binary()`
also searches `/usr/games` (systemd's PATH omits it). SDL games
(`chromium-bsu`, `lbreakout2`) get `SDL_VIDEODRIVER=kmsdrm`. Every game needs
a personal PIN (or the emergency code) — `FREE_GAMES` is empty but kept as the
lever if you want to exempt any. PINs are per-person in `~/.pitv/pins.json`,
managed by
`screen pin assign|list|remove|rename` (`pin-admin.py`), and every unlock is
logged with the person's name so you can see who played. Snake is built-in
(R button speeds it up; end score gets a speed
multiplier). Snake and Noughts & Crosses are built-in curses games
(`run_snake`, `run_noughts`) that read `input_queue`, so the controller, CEC
and the Apple remote all drive them. Tetris is `vitetris` (its own menu has
1- and 2-player). Install extras: `sudo apt install -y vitetris`. `run_game`
logs each game's exit code and duration to `/tmp/pitv.log` (an instant
`rc!=0` = crash-on-launch).

## Screen mirroring

`run_mirror()` releases the console (`endwin`) and runs **uxplay** with a
`kmssink` render-rectangle sized to show the phone centred at its own
(portrait) shape, falling back to a fullscreen sink if that fails.
Needs `uxplay` + gstreamer plugins + `avahi-daemon` (mDNS).

## HomeKit TV (Homebridge)

`homebridge-pitv-tv/` is a local Homebridge platform plugin that publishes a
**Television** accessory as an **external accessory**
(`publishExternalAccessories`) so it appears as a TV tile in the Home app —
*not* inside the bridge. It talks to `tv_menu.py` over a **localhost UDP
datagram** to `127.0.0.1:8129` (the `listen_cec_udp` thread), which accepts:
`TV_ON`/`TV_OFF` (power → `cec-cmd.sh on 0`/`standby 0`), `CEC tx <frame>`
(input switching via a whitelisted raw CEC frame), and `KEY <TOKEN>` (menu
navigation, so the **Apple Home / Control Centre remote** drives the menu by
feeding `input_queue`). UDP is used because the official Homebridge service is
sandboxed (`ProtectSystem=strict`) and can't write a `/tmp` FIFO, but it can
always send a localhost packet. `tv_menu.py` owns the CEC bus and runs
`cec-cmd.sh` itself with output suppressed (so it never scribbles on the menu).
Power state is bidirectional: `listen_cec_remote` polls the TV's CEC power
status and writes `on`/`off` to `/tmp/pitv-tv-state`, which the plugin reads +
polls so the physical remote is reflected in the Home app.
`deploy.sh` reinstalls the plugin into Homebridge (npm copies it at install
time, so refreshing `/opt/pitv` alone isn't enough).
(`tv-state.sh` was the older homebridge-cmd4 approach and is superseded.)

## Secrets — never commit

The Homebridge PIN, the player PINs (`~/.pitv/pins.json`), and the emergency
bypass code are **device-only**. Keep them out of git. If any leaks into the
repo, rotate it. Config files that contain them (e.g. `~/.homebridge/config.json`)
live on the Pi, not here.

## Branch / deploy workflow

Development branch: `claude/pitv-onboarding-gu3vbq`, pushed to the remote
`claude` branch (the remote can't hold `claude/…` because a ref named
`claude` already occupies that namespace). After pulling on the Pi:
`git pull origin claude && ./deploy.sh`.
