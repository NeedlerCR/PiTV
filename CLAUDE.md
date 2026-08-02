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
   gamepads into real keyboard events via **`/dev/uinput`**. The left face
   button is select/fire (Space+Enter), the bottom is back (Esc); Player 2 →
   WASD + F/G. `run_game()` passes the game binary as `argv[1]`, so a few games
   get tailored buttons: **nudoku** (right button types a number by pressing it
   that many times, top = hint, bottom/B = remove) and **freesweep** (right =
   reveal, top = flag). For **vitetris** 2-player on a single pad, the left
   stick is P1 and the right stick is P2 (WASD) — unless `screen remote` is on,
   in which case P2 is the SSH keyboard and the whole pad stays P1
   (`_remote_running()` decides). vitetris's Player-2 keys must be set to
   W/A/S/D (+ rotate) once in its own Options menu for this to reach P2.
3. **CEC remote** (`listen_cec_remote`) and the **FIFO** `/tmp/tv_menu.fifo`
   (written by the `screen` CLI, `remote.py`, and the web portals) also feed
   `input_queue`. During an external game, FIFO nav tokens and `TYPE <char>`
   lines are injected as real keystrokes (uinput) instead — so `screen remote`
   now types **any** key into a running game (numbers, letters, punctuation),
   e.g. picking a Minesweeper grid size or playing NetHack over SSH. The Apple
   remote uses the same path (`_remote_inject` / `_inject_char`).

> **Critical setup:** the service user needs access to `/dev/uinput` and
> `/dev/input/event*`, or key injection silently fails and games ignore the
> controller. Run **`./setup-input.sh`** once, then reboot. Symptoms of it not
> being done: menu works, but games don't respond to the pad.
> `remote.py` (`screen remote on`) drives the menu AND (while a game is up)
> types straight into it, but it still can't be a *second* local player — use a
> real USB keyboard or a 2nd gamepad for P2.

## Games

Defined in `GAME_KEYS` / `GAME_OPTIONS` in `tv_menu.py`. `resolve_binary()`
also searches `/usr/games` (systemd's PATH omits it). SDL games (the
`SDL_GAMES` set in `run_game`: `chromium-bsu`, `lbreakouthd`, `opentyrian`,
`supertux2`, `frozen-bubble`) get `SDL_VIDEODRIVER=kmsdrm`; console/ncurses
games just run on the framebuffer. Every game needs a personal PIN (or the
emergency code) — `FREE_GAMES` is empty but kept as the lever if you want to
exempt any. PINs are per-person in `~/.pitv/pins.json`, managed by
`screen pin assign|list|remove|rename` (`pin-admin.py`), and every unlock is
logged with the person's name so you can see who played. `run_game` logs each
game's exit code and duration to `/tmp/pitv.log` (an instant `rc!=0` =
crash-on-launch). A binary that isn't installed shows an `[N/A]` badge in the
menu and a "how to install" screen instead of crashing.

**Built-in curses games** (`run_snake`, `run_noughts`) read `input_queue`, so
the controller, CEC and Apple remote all drive them. Snake: R button speeds it
up; end score gets a speed multiplier. Noughts & Crosses offers 1-player (vs
computer, `_ttt_ai`) or 2-player (`TTT_MODE` view) before the PIN keypad.

**Two-player PINs:** any game whose `GAME_OPTIONS` row is flagged 2-player
collects **both** players' PINs on the keypad (stage 1 = P1, stage 2 = P2,
tracked by `pending_game_2p` + `ttt_stage`/`ttt_p1`) before launching — Tetris
and Noughts & Crosses today.

**The roster** (all PIN-locked). Retro arcade: Space Invaders (`ninvaders`),
Breakout (`lbreakouthd`), Space Shooter (`chromium-bsu`), Tyrian
(`opentyrian`, contrib), Moon Buggy (`moon-buggy`), Boulder Dash (`phear`, pkg
`cavezofphear`). Modern: Tetris (`vitetris`, 1-/2-player in its own menu),
Super Tux (`supertux2`, GL — may be slow on a Zero 2 W). Puzzle/strategy: 2048
(`2048`), Sudoku (`nudoku`), Minesweeper (`freesweep`), Curse of War
(`curseofwar`, RTS vs AI). Roguelike/RPG (keyboard recommended — too many keys
for a pad): NetHack (`nethack`, pkg `nethack-console`), Dungeon Crawl
(`crawl`), Dope Wars (`dopewars`). (Frozen Bubble was dropped — it's SDL 1.2
and won't run on the bare framebuffer console, so it crashed on launch.)
Install the lot:
`sudo apt install -y vitetris ninvaders lbreakouthd chromium-bsu opentyrian moon-buggy cavezofphear supertux 2048 nudoku freesweep curseofwar nethack-console crawl dopewars`
(`opentyrian` and `chromium-bsu` are in `contrib`/`non-free`, so enable those
components in `/etc/apt/sources.list` first). The games menu (`_draw_card_list`)
scrolls now that the list is long — the highlighted card stays in view with
`^ more ^` / `v more v` hints.

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

## Guest web portal

`guest-portal.py` (stdlib `http.server`, run by `pitv-guest.service` on port
**8080**) lets guests on the Wi-Fi control TV power + HDMI input and drive the
menu via an on-screen **D-pad** — but ONLY while the Home **Guest Mode** switch
is ON. The plugin's Guest Mode switch sends `GUEST_ON`/`GUEST_OFF` over the 8129
UDP channel; `tv_menu` writes `/tmp/pitv-guest-mode`, which the portal reads.
Guests sign in with a password (`screen guest password set <user> <pw>`) or an
NFC link `/nfc?t=<token>` (1-week HMAC-signed cookie that redirects to hide the
URL), see their assigned player PIN (rotates weekly or via
`screen pin guest rotate all`), and cannot use the kill switch or admin.
Actuation: TV/input via the 8129 UDP channel, menu nav by writing the FIFO.
Data lives in `~/.pitv/guests.json` + `~/.pitv/pins.json` (device-only).

The **same `guest-portal.py`** run with `--admin` (`pitv-admin.service`,
`AmbientCapabilities=CAP_NET_BIND_SERVICE` so it binds **port 80** →
`http://raspberrypi.local`) is the admin portal: username+password login
(`screen admin password set <user> <pw>`, hashed in `~/.pitv/admin.json`), the
same controls, **not** gated by Guest Mode, plus a Guest Mode on/off toggle
(the plugin polls `/tmp/pitv-guest-mode` so the Home switch stays in sync).

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
