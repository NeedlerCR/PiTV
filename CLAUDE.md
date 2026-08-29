# PiTV — project guide

A Raspberry Pi Zero 2 W wired to a TV that boots straight into a full-screen
curses menu (screensavers, retro games, AirPlay screen mirroring) and exposes
the TV's power to Apple HomeKit over HDMI-CEC.

Versioning is **ChronosVer** (`vYYYY.MAJOR.MINOR.BUG`); the canonical value is
in `VERSION` (and `tv_menu.py`'s `VERSION`), shown by `screen version`.

Security escalation: 3 wrong game PINs → lockout keypad; 3 wrong tries there →
**full lockdown** (`hard_locked`): the TV is forced off every 7 s and the
screen is blocked. Only `screen unlock authorise <emergency code>` clears it
(kill switch off, TV on, input → PiTV). Both lock states are persisted to
`~/.pitv/lock-state.json` and restored at startup, so power-cycling the Pi no
longer clears a lockdown.

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
- Shared modules: `pitv_secrets.py` (emergency code, password hashing, signed
  commands) and `pitv-config.py` (the `screen sky|control|network` settings).

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
   reveal, top = flag). For **vitetris** 2-player on a single pad the
   pad is split by *control surface*, so the two players can't fight over the
   same axis: the **D-pad is P1** (arrows, L shoulder rotates) and **either
   analog stick is P2** (WASD, R shoulder rotates). A pad with no real D-pad
   (ABS_HAT0X absent) falls back to the old split — left stick P1, right stick
   P2. Unless `screen remote` is on, in which case P2 is the SSH keyboard and
   the whole pad stays P1 (`_remote_running()` decides). vitetris's Player-2
   keys must be set to W/A/S/D (+ rotate) once in its own Options menu for this
   to reach P2.
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

## Sky Q remote (off by default)

The Sky Q box is on the same CEC bus, so `tv_menu` can drive it by sending
User Control frames (`0x44` pressed / `0x45` released) to its logical address —
no extra hardware. **Nothing is sent until `screen sky on`**; the state lives in
`~/.pitv/sky.json` (`enabled`, `logical`, `always`) and is read fresh on every
key. Sky needs *Settings → Setup → Preferences → Control other devices* on.

- `SKY_KEYS` is the whole vocabulary (nav, Sky/Guide/Info, transport, CH ±,
  colour buttons, 0-9). A token that isn't in it is dropped — nothing else ever
  reaches the bus.
- `route_remote_key()` is the single place deciding where a nav key goes: a
  running game first, then Sky **while the TV is on the Sky input** (or always,
  with `screen sky on always`), else the PiTV menu. `HOME` is always the escape
  hatch — it switches the TV back to the Pi's input and returns to the menu.
- Both remotes reach it: the **Apple Home / Control Centre** remote via
  `KEY <TOKEN>` on the UDP channel, and the **web portal**, which grows a Sky
  panel (Guide, Info, transport, CH ±, colours, Sky standby) whenever Sky mode
  is on. The portal only ever sends fixed tokens from `SKY_BUTTONS`.
- CLI: `screen sky on [<la>] [always]`, `screen sky off`, `screen sky status`,
  `screen sky <button>`, `screen sky power on|off`.

## HomeKit TV (Homebridge)

`homebridge-pitv-tv/` is a local Homebridge platform plugin that publishes a
**Television** accessory as an **external accessory**
(`publishExternalAccessories`) so it appears as a TV tile in the Home app —
*not* inside the bridge. It talks to `tv_menu.py` over a **localhost UDP
datagram** to `127.0.0.1:8129` (the `listen_cec_udp` thread), which accepts:
`TV_ON`/`TV_OFF` (power → `cec-cmd.sh on 0`/`standby 0`), `CEC tx <frame>`
(input switching via a whitelisted raw CEC frame), `KEY <TOKEN>` (menu
navigation, so the **Apple Home / Control Centre remote** drives the menu by
feeding `input_queue`), and `JOKE_ON`/`JOKE_OFF` (see below). UDP is used
because the official Homebridge service is sandboxed (`ProtectSystem=strict`)
and can't write a `/tmp` FIFO, but it can always send a localhost packet.
**Every datagram must be signed** (see *Authenticated commands* below).
`tv_menu.py` owns the CEC bus and runs `cec-cmd.sh` itself with output
suppressed (so it never scribbles on the menu).
Power state is bidirectional: `listen_cec_remote` polls the TV's CEC power
status and writes `on`/`off` to `/tmp/pitv-tv-state`, which the plugin reads +
polls so the physical remote is reflected in the Home app.

**Joke Mode** is a bridged switch (alongside the kill switch and Guest Mode):
while it's on, `_set_joke_mode` waits a random **10–50 minutes**, sends one CEC
standby so the TV appears to die on its own, then picks a fresh delay and
repeats. Unlike the kill switch it never re-sends standby, so the TV can just be
switched back on. State lives in `/tmp/pitv-joke-mode` (cleared at boot, so a
reboot never comes back still pranking); the plugin polls it, and
`screen joke on|off|status` drives the same thing over the FIFO.

`deploy.sh` reinstalls the plugin into Homebridge (npm copies it at install
time, so refreshing `/opt/pitv` alone isn't enough).
(`tv-state.sh` was the older homebridge-cmd4 approach and is superseded.)

## Authenticated commands

Nothing drives the TV on trust any more:

- **UDP control channel** — each datagram is
  `PITV1 <ts> <nonce> <hmac-sha256> <payload>`, keyed on a secret shared with
  Homebridge and the portals (`pitv_secrets.sign_command` /
  `CommandVerifier`). Unsigned, stale (>120 s), replayed or wrongly-keyed
  datagrams are counted and dropped, never logged verbatim. The socket is still
  bound to `127.0.0.1` only.
- **The key** lives in `/etc/pitv/control.key`, mode 0640, group `pitv`;
  `deploy.sh` creates it and puts the PiTV user *and* the Homebridge user in
  that group (the plugin also accepts `controlKey`/`controlKeyFile` in its
  Homebridge config). `~/.pitv/control-key` is the fallback. `tv_menu` notices a
  rotated key without a restart. `screen control status` shows the fingerprint,
  who can read the file and whether Homebridge can; `screen control key
  show|rotate` manages it.
- **The FIFO** is 0660, not 0666 — it's a command channel into the menu (and,
  during a game, into the game as keystrokes), so only the PiTV user and group
  may write it.
- **Portal allowlist** — `screen network allow <cidr>` restricts which client
  addresses may reach the web portals at all; the connection is dropped in
  `verify_request` before the login page is served, so a VPN range you haven't
  allowed never even sees it. Empty (the default) means anyone who can route to
  the Pi, as before. `screen network status` / `screen network clear`.

## Guest web portal

`guest-portal.py` (stdlib `http.server`, run by `pitv-guest.service` on port
**8080**) lets guests on the Wi-Fi control TV power + HDMI input and drive the
menu via an on-screen **D-pad** — but ONLY while the Home **Guest Mode** switch
is ON. The plugin's Guest Mode switch sends `GUEST_ON`/`GUEST_OFF` over the 8129
UDP channel; `tv_menu` writes `/tmp/pitv-guest-mode`, which the portal reads.
Guests sign in with a password (`screen guest password set <user> <pw>`) or an
NFC link `/nfc?t=<token>` (1-week HMAC-signed cookie that redirects to hide the
URL), see their assigned player PIN (rotates weekly — checked on each page load,
not just at startup — or via `screen pin guest rotate all`), and cannot use the
kill switch or admin. Both portals throttle failed logins (and bad NFC tokens)
per client IP with a doubling lockout, cap request bodies, send a CSP plus
`nosniff`/`DENY`/`no-referrer`, and refuse a cross-origin POST.
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

`pitv_secrets.py` is the one place that handles them:

- **Emergency code** — lives in `~/.pitv/emergency-code` (0600), read fresh on
  every check, set with `screen emergency set <6 digits>`. It used to be
  hardcoded in `tv_menu.py`, `guest-portal.py` and `pin-admin.py`, so the
  shipped value (`LEGACY_EMERGENCY_CODE`) is **public in git history** — it is
  kept only to seed the file on upgrade. `screen emergency status` says whether
  a Pi is still on it, and `tv_menu` logs a warning at boot while it is.
- **Portal passwords** — PBKDF2-HMAC-SHA256, per-record salt and iteration
  count (`hash_password` / `verify_password`). Records written by older
  versions (one round of salted SHA-256) still verify and are upgraded in place
  on the owner's next sign-in.
- **Player PINs** and NFC tokens come from `secrets`, never `random`.

## Branch / deploy workflow

Development branch: **`experimental`**, pushed to the remote branch of the same
name and merged to `main` by PR. After pulling on the Pi:
`git pull origin experimental && ./deploy.sh`.
