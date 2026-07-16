# Proton Command Center

A local web app for managing your Steam library on Linux without digging
through Steam's UI: per-game launch options, Proton version selection, DLSS
DLL management with one-click updates, shader cache management,
MangoHud overlay configuration and before/after benchmarks, full controller
navigation, a ProtonDB
rating check, and a full owned-library view with one-click installs. Built
for Arch-based distros (CachyOS, EndeavourOS, vanilla Arch) with an NVIDIA
slant, though nothing Arch-specific is required beyond the install scripts.

Runs entirely on your machine at `http://localhost:8686`. Python standard
library only — no dependencies to install.

## Requirements

- Python 3 (`sudo pacman -S python`) — the only hard dependency
- Steam, installed and logged in at least once
- Optional: `mangohud` (overlay + benchmarks), an NVIDIA driver (DLSS
  features, shader cache management)

## Install

```bash
tar xzf proton-command-center-*.tar.gz
cd proton-command-center
./install.sh
```

Installs to `~/.local/share/proton-command-center`, adds a launcher and an
app-menu entry, and sets up a **systemd user service** that runs the backend
from login and restarts it if it dies. Open it from the app menu, run
`proton-command-center`, or browse to **http://localhost:8686**.

Prefer a real package? `makepkg -si` with the included PKGBUILD, then
`systemctl --user enable --now proton-command-center`.

## Optional API keys

Click the **gear icon** (top right):

1. **SteamGridDB key** — artwork for games Steam's CDN doesn't cover (betas,
   demos, delisted titles). Free at [steamgriddb.com](https://www.steamgriddb.com)
   → Profile → Preferences → API.
2. **Steam Web API key** — the full owned-library view: every game you own,
   uninstalled ones greyed out with an Install button. Free at
   [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey).
   Your SteamID is detected automatically.

Both are stored locally in `config.json` (permissions 0600), used only
server-side. Skip them and everything else still works — you just get
CDN-only art and installed games only.

## Using it

The grid shows your games with a status rail on each card: launch options
set, DLSS detected, shader cache present, shaders compiled. Hover for
**▶ Play** (launches through Steam). Greyed cards are owned but not
installed — click to install, with live progress on the card. Click an
installed game to open its panel.

**Launch tab** — pick a compatibility tool (Proton-CachyOS, GE-Proton, and
official builds are auto-detected), then build the launch string with
toggles: native Wayland, HDR, MangoHud, game-performance, and Steam Deck
spoof on/off. Full DLSS controls sit below: Super Resolution modes with
custom ratio and transformer presets, Frame Generation, Ray Reconstruction,
and a frame-rate cap. **One Save** writes the Proton version and launch
string together, closing Steam cleanly first (it would otherwise overwrite
the change on exit) with a timestamped backup next to `localconfig.vdf`.

**🔍 ProtonDB check** — fetches the game's community rating and shows a
coloured tier badge with a star score. Click the badge to open the game's
ProtonDB page. Once checked, the badge persists across restarts.

**DLSS tab** — each DLSS DLL in the game with its version. The three buttons
fetch the latest Super Resolution, Frame Generation, and Ray Reconstruction
DLLs into your local library, sourced from the DLSS Swapper manifest (the
same version-tracked source that tool uses). An **update available** chip
appears when a game's DLL is behind your library's newest. Originals are
backed up before any swap; Restore is one click. Restore originals before
playing anti-cheat titles that enforce file integrity.

**Shader cache tab** — a global toggle writes the NVIDIA shader-disk-cache
environment variables to `/etc/environment`, consolidating the driver's
compiled shaders into one capped location that survives cleanup (this is the
part that meaningfully reduces in-game shader stutter). Shows cache sizes per
game, with clear-cache and delete-everything actions under **Advanced**.

Command Center does **not** try to pre-empt Steam's own "Processing Vulkan
shaders" pass. Steam replays its `.foz` pipeline caches into its own database
and gates on its own ledger, so nothing outside Steam can convince it to skip
that work. If the processing screen bothers you, the real switch is Steam →
Settings → Downloads → Shader Pre-Caching → untick *Allow background
processing of Vulkan shaders* — the trade-off being that shaders then compile
during play, causing brief first-encounter stutter.

**System & MangoHud panel** (🖥 button) — detects and names your CPU and
GPU(s), reads VRAM, and generates a MangoHud config in a compact horizontal
layout (orange labels, white values, frametime graph, pinned to the discrete
GPU on hybrid laptops). Enabling the MangoHud launch toggle writes this
config automatically on first use. Presets range from a minimal readout to a
full benchmark overlay.

**Benchmark tab** — save the benchmark launch options, play, make a change,
play again. Logs are split at the marked time and compared on avg FPS, 1% and
0.1% lows, and stutter count, with frametime graphs.

**Controller navigation** — plug in a pad (Xbox, DualSense, Steam Deck) and the
whole UI becomes navigable from the couch. A hint bar appears along the bottom
with the mapping:

| Input | Action |
|---|---|
| **D-pad / left stick** | Move between cards and controls |
| **A** | Select / activate |
| **B** | Back — closes the drawer or settings |
| **X** | Play (or Install) the focused game |
| **Y** | Jump to search |
| **LB / RB** | Cycle the drawer's tabs |
| **Start** | Settings |
| **Select / Back** | Toggle fullscreen |

Navigation is spatial, so pressing down from a card lands on the card directly
below rather than wandering diagonally; left/right at the end of a row wraps to
the next one. A game card is a single stop — its Play/Install button isn't a
separate target, and the focused card reveals its ▶ so you can see what **X**
will act on. The pad drives real DOM focus, so keyboard navigation improves
alongside it, and nothing runs until a pad is connected. Text fields still need
a keyboard — in Game Mode, Steam's on-screen keyboard (**Steam + X**) covers it.

Confirmation prompts are drawn in-page rather than using the browser's native
`confirm()`, because a native dialog halts JavaScript — which freezes the pad
polling loop and leaves the prompt literally unanswerable from a controller.
Installing a game asks nothing at all: Steam opens its own install dialog, so a
second confirmation was only ever confirming that you'd like to be asked.

**Fullscreen** — the ⛶ header button (or **F11**, or **Select** on a pad)
toggles fullscreen, and **Esc** always leaves it. *Settings → Display → Open
fullscreen* makes it automatic. One caveat worth knowing: browsers forbid a page
from going fullscreen on load without a user gesture, so it fires on your first
click or controller button rather than the instant the page appears. That's a
browser security rule, not a setting. Command Center deliberately doesn't launch
your browser in `--kiosk` mode — that traps you (F11 won't exit, only Alt+F4)
and silently does nothing if a browser window is already open.

**🎮 Game Mode button** *(CachyOS only)* — appears in the header only on
CachyOS Handheld / systems with `gamescope-session-cachyos` installed.
Switches into the gamescope Game Mode session (the Steam Deck UI). Since this
ends the desktop session, it also closes PCC and the browser tab — return to
desktop from Steam's Power menu → Switch to Desktop. Hidden entirely on
systems without the CachyOS handheld session.

## Settings (gear icon)

Beyond the optional API keys above, the Settings panel holds two occasional
tools kept out of the main UI:

**Backup & restore** — export your DLL library, launch settings, MangoHud
config, API keys, and ProtonDB ratings to a single timestamped `.tar.gz`
(saved to Downloads). After a reinstall, paste the archive path and Restore
pulls it all back. The art cache is excluded (it re-fetches itself).

**Proton versions** — lists recent GE-Proton releases from GitHub with a
clear "up to date" or "update available" status. Install any version with one
click; it extracts into `~/.local/share/Steam/compatibilitytools.d/` and
Steam picks it up on next restart. Already-installed versions are flagged.
(Proton-CachyOS is installed through pacman — `proton-cachyos` — which is the
better path on CachyOS since it's system-optimised.)

## Updating

Re-run `./install.sh` — it copies the new files and restarts in one go. If
the UI shows a red banner, the backend is running older code than the
frontend: `systemctl --user restart proton-command-center` and refresh.

## Troubleshooting

- **Missing artwork** — gear icon → "Clear art cache & re-fetch".
- **Backend not responding** — `systemctl --user restart proton-command-center`.
- **Save says Steam is running** — that's the auto-close flow: confirm, and
  it shuts Steam down cleanly, saves, and offers a one-click restart.
- **Shader env toggle needs a re-login** — `/etc/environment` changes only
  apply to newly-started sessions.

## Uninstall

```bash
./uninstall.sh           # interactive, asks before deleting user data
./uninstall.sh --purge   # remove everything including user data
```

Removes the service, launcher, menu entry, and app files. User data (DLSS
DLL library, DLL backups, compile state, art cache, API keys) is kept unless
you purge — restore any swapped DLLs first if you do.

## Development

Single-file stdlib backend. Tests build a mock Steam install in a temp dir,
so they run anywhere:

```bash
python3 tests/test_pcc.py
```

Run a second instance without touching your installed service:
`PCC_PORT=8687 python3 pcc.py`. Games launched via Play run in their own
systemd scope, so restarting the backend never kills a running game.

MIT licensed. Copyright (c) 2026 Marc Gibb.
