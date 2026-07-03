#!/usr/bin/env bash
# Proton Command Center — uninstaller
set -euo pipefail

APP_NAME="proton-command-center"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_ROOT="$DATA_HOME/$APP_NAME"

echo "== Proton Command Center uninstaller =="

# stop and remove the systemd user service
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now "$APP_NAME.service" >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/$APP_NAME.service"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "  removed systemd user service"
fi

# stop a running backend
if pgrep -f "$APP_ROOT/app/pcc.py" >/dev/null 2>&1; then
    pkill -f "$APP_ROOT/app/pcc.py" || true
    echo "  stopped running backend"
fi

rm -f  "$HOME/.local/bin/$APP_NAME"                 && echo "  removed launcher"
rm -f  "$DATA_HOME/applications/$APP_NAME.desktop"  && echo "  removed desktop entry"
rm -rf "$APP_ROOT/app"                              && echo "  removed app files"
command -v update-desktop-database >/dev/null && update-desktop-database "$DATA_HOME/applications" 2>/dev/null || true

# user data: compile state, DLL library, DLL backups, art cache, SGDB key
if [ -d "$APP_ROOT" ]; then
    echo
    echo "User data remains in $APP_ROOT"
    echo "  - state.json          (compile tracking)"
    echo "  - dlls/               (your DLSS DLL library)"
    echo "  - backups/            (original game DLLs saved before swaps)"
    echo "  - art/, config.json   (art cache, SteamGridDB key)"
    echo
    echo "NOTE: if you swapped DLSS DLLs in any game, restore originals from the"
    echo "app BEFORE deleting backups/, or verify files with Steam afterwards."
    read -r -p "Delete all user data too? [y/N] " ans
    case "$ans" in
        [yY]*) rm -rf "$APP_ROOT"; echo "  user data deleted" ;;
        *)     echo "  user data kept" ;;
    esac
fi

echo
echo "Uninstalled. Launch-option backups (*.pcc-*.bak) live next to Steam's"
echo "localconfig.vdf and were intentionally not touched."
