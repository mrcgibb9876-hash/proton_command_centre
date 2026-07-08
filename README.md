# Proton Command Center

[![AUR version](https://img.shields.io/aur/version/proton-command-center?logo=archlinux&label=AUR)](https://aur.archlinux.org/packages/proton-command-center)
[![AUR votes](https://img.shields.io/aur/votes/proton-command-center?label=votes)](https://aur.archlinux.org/packages/proton-command-center)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Install on Arch / CachyOS / EndeavourOS:** `yay -S proton-command-center` — [AUR package page](https://aur.archlinux.org/packages/proton-command-center)



One place to run your Steam library on Linux without touching Steam's UI:
one-click per-game Auto-tune cross-verified against Valve's Deck reports,
ProtonDB, and the umu-protonfixes database; per-game launch options; Proton
version selection; DLSS DLL management with one-click updates (SR, Frame
Generation up to 4x, Ray Reconstruction, Smooth Motion); Fossilize shader
precompilation with persistent tracking; MangoHud before/after benchmarks;
full owned-library view with one-click installs; and a Play button. Built for Arch-based distros (CachyOS,
EndeavourOS, vanilla Arch) with an NVIDIA slant, but nothing Arch-specific
is required beyond the install scripts.

## Requirements

- Python 3 (`sudo pacman -S python`) — the only hard dependency
- Steam, installed and logged in at least once
- Optional but recommended: `mangohud` (benchmarks), `gamescope` (launch
  toggle), an NVIDIA driver (DLSS features + driver-aware compile tracking)
- `fossilize_replay` for shader precompilation — ships with Steam's Linux
  runtime automatically once you've launched any Proton game

## Install

```bash
tar xzf proton-command-center-*.tar.gz
cd proton-command-center
./install.sh
```

The installer checks every dependency (with pacman hints for anything
missing), installs to `~/.local/share/proton-command-center`, adds a
launcher and an app-menu entry under Games, and sets up a **systemd user
service** so the backend runs from login and auto-restarts if it ever dies.

Open it from the app menu, run `proton-command-center`, or just browse to
**http://localhost:8686**.

Prefer a real package? `makepkg -si` with the included PKGBUILD, then
`systemctl --user enable --now proton-command-center`.

## First-run setup (two optional keys)

Click the **gear icon** (top right):

1. **SteamGridDB API key** — fills in artwork for games Steam's CDN doesn't
   cover (betas, demos, delisted titles). Free: sign in at
   [steamgriddb.com](https://www.steamgriddb.com) → Profile → Preferences →
   API → Generate. Paste, Save.
2. **Steam Web API key** — unlocks the full-library view: every game you
   own appears, uninstalled ones greyed out with an Install button. Free:
   [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey)
   (any domain name works, e.g. `localhost`). Paste, Save. Your SteamID is
   detected automatically from Steam's `loginusers.vdf`.

Both keys are stored locally in
`~/.local/share/proton-command-center/config.json` (permissions 0600) and
are only ever used server-side. Skip both and everything else still works —
you just get CDN-only art and installed games only.

## Using it

The grid shows your games with a four-light status rail on each card:
**blue** = launch options set, **green** = DLSS detected, **amber** = shader
cache present, **purple** = shaders compiled. Hover a card for **▶ Play**
(launches through Steam in the background — Steam starts itself if needed).
Greyed-out cards are owned but not installed; click one and confirm to start
the install. Click any installed game to open its panel:

**✨ Auto-tune** — the flagship. One click at the top of the Launch tab:

- **Detects the engine from the game files** (Unreal 4/5 via pak/IoStore
  markers, Unity, RE Engine, Source/Source 2, Godot, GameMaker) and whether
  the game targets DX12 or DX11 by reading the executable.
- **Checks your hardware**: VRAM is read from the driver and a
  `dxgi.maxDeviceMemory` headroom cap is applied on 8 GB-class GPUs to
  prevent eviction hitching. Handhelds are detected via DMI (Steam Deck,
  ROG Ally, Legion Go…) so desktop-only fixes — like `SteamDeck=0` for games
  that wrongly load handheld presets (Stellar Blade) — are applied on
  desktops and correctly withheld on real handhelds.
- **Cross-verifies community data** instead of trusting any single source:
  the aggregate ProtonDB tier, Valve's own professionally-tested Deck
  compatibility report, and the maintainer-curated umu-protonfixes database
  (the fix scripts GE-Proton applies automatically). When ProtonDB and
  Valve agree you're told the data is corroborated; when they disagree
  you're warned to treat community tips with caution. If a umu fix exists,
  Auto-tune summarises what it does and points you at GE-Proton in the
  compat dropdown, which applies it with zero extra work.
- **Shows its reasoning**: every applied flag comes with the rule that fired
  and the evidence found. The result lands in the launch builder for review
  — nothing is written until you hit Save.
- **Rules are updatable**: drop a `tuning_rules.json` into
  `~/.local/share/proton-command-center/` to override or extend the built-in
  engine profiles and per-game fixes without touching code. Community
  lookups cache locally (24h–7d) so repeated clicks cost nothing.

**Launch tab** — pick a compatibility tool (Proton-CachyOS, GE-Proton, and
official builds are all auto-detected, including system packages in
`/usr/share/steam/compatibilitytools.d`), then build the launch string with
toggles: native Wayland (still opt-in upstream — better pacing and HDR
without Gamescope, but breaks the Steam Overlay), no-WM-decorations and
bypass-Steam-Input companions for Wayland quirks, HDR, NTSync, NVAPI, Steam
Deck spoof, Proton DLSS auto-upgrade, 64-bit-only NVIDIA libs (RTX 40/50
perf fix), MangoHud, game-performance, Smooth Motion, NVIDIA Reflex, and the
DLSS indicator overlay — plus full DLSS panels: Super Resolution modes with
custom scaling ratio and J/K/L/M transformer presets, Frame Generation up to
4x, Ray Reconstruction presets, and a DXVK frame-rate cap. Presets included for a CachyOS baseline, UE5 anti-stutter, and
anti-cheat spoofing. **Saving closes Steam cleanly first** (it would
overwrite the change on exit otherwise) — you'll be asked to confirm, and a
Start Steam button appears after. Every save makes a timestamped backup next
to `localconfig.vdf`.

**DLSS tab** — shows each DLSS DLL in the game with a friendly version
("DLSS 4 · 310.2.1"). The three green buttons fetch the latest Super
Resolution, Frame Generation, and Ray Reconstruction DLLs straight from
NVIDIA's official GitHub repos into your local library. If a game's DLL is
older than your library's best, an **"update available"** chip and one-click
Update button appear. Originals are backed up automatically before any swap;
Restore is one click. Heads-up: restore originals before playing anti-cheat
titles that enforce file integrity.

**Shader cache tab** — cache size, precompile (Fossilize, all cores minus
two, correct `--device-index`), and two clear options: **Clear compiled
cache** keeps the fozpipelinesv6 recordings so you can precompile again
immediately; **Delete everything** removes those too. Compiled status
persists across restarts and only resets when your NVIDIA driver changes or
you delete the cache — new pipeline data just flags "recompile recommended".
**Compile all** in the header does your whole library, skipping anything
already current.

**Benchmark tab** — save the benchmark launch options, play a few minutes,
precompile, play again. Logs are split at your compile time and compared on
avg FPS, 1% lows, 0.1% lows, and stutter count, with frametime graphs and an
improvement badge.

## Updating

Copy the new `pcc.py` and `index.html` over
`~/.local/share/proton-command-center/app/` and restart the backend:

```bash
systemctl --user restart proton-command-center
```

Or just re-run `./install.sh` — it copies and restarts in one go. If the UI
ever shows a red banner, the backend is running older code than the
frontend: run the restart command above and refresh.

## Troubleshooting

- **Missing artwork** — gear icon → "Clear art cache & re-fetch". For one
  stubborn game, open
  `http://localhost:8686/api/art_debug/<appid>?name=<game name>` to see a
  step-by-step trace of the art lookup (CDN → SteamGridDB by appid →
  SteamGridDB by name).
- **Backend not responding** — `systemctl --user status proton-command-center`
  and `systemctl --user restart proton-command-center`.
- **Want it running even after logout** (e.g. kicking off Compile All over
  SSH): `loginctl enable-linger $USER`.
- **Save button says Steam is running** — that's the auto-close flow: confirm
  and it shuts Steam down cleanly, saves, and offers a one-click restart.

## Uninstall

```bash
./uninstall.sh           # interactive
./uninstall.sh --purge   # remove everything including user data, no questions
```

Removes the service, launcher, menu entry, and app files. It asks before
deleting user data (your DLSS DLL library, original-DLL backups, compile
state, art cache, API keys) — restore any swapped DLLs first if you're
purging backups. Launch-option backups next to Steam's `localconfig.vdf`
are deliberately left in place.

## Repository layout

```
pcc.py            backend — Steam scan, VDF write, DLSS, caches (stdlib only)
index.html        frontend — served by pcc.py at localhost:8686
install.sh        per-user installer with dependency checks (no sudo)
uninstall.sh      uninstaller (asks before touching user data)
PKGBUILD          Arch/CachyOS package build
tests/test_pcc.py test suite — python3 tests/test_pcc.py (no Steam needed)
demo/pcc-demo.html  standalone UI demo with a mocked backend
LICENSE           MIT
```

## Development

Single-file backend, no dependencies. Tests build a mock Steam install in a
temp directory, so they run anywhere:

```bash
python3 tests/test_pcc.py
```

Run a second instance for testing pre-release builds without touching your
installed service: `PCC_PORT=8687 python3 pcc.py`. Games launched from the
Play button run in their own systemd scope, so restarting the backend never
kills a running game.

MIT licensed. Copyright (c) 2026 Marc Gibb.

