#!/usr/bin/env bash
# Proton Command Center — per-user installer (no sudo required)
set -euo pipefail

APP_NAME="proton-command-center"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_HOME/$APP_NAME/app"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$DATA_HOME/applications"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

c_ok()   { printf '\033[32m  ok\033[0m  %s\n' "$1"; }
c_warn() { printf '\033[33mwarn\033[0m  %s\n' "$1"; }
c_err()  { printf '\033[31mfail\033[0m  %s\n' "$1"; }

echo "== Proton Command Center installer =="
echo

# ---- dependency checks -----------------------------------------------------
MISSING=0

if command -v python3 >/dev/null; then
    c_ok "python3 $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')"
else
    c_err "python3 is required.  Install it:  sudo pacman -S python"
    MISSING=1
fi

if command -v steam >/dev/null; then
    c_ok "steam"
else
    c_warn "steam not found in PATH — install it (sudo pacman -S steam) or the app will find no games"
fi

if command -v fossilize_replay >/dev/null; then
    c_ok "fossilize_replay (system)"
elif ls "$DATA_HOME"/Steam/steamapps/common/SteamLinuxRuntime*/*/fossilize_replay >/dev/null 2>&1 \
  || ls "$HOME"/.steam/steam/steamapps/common/SteamLinuxRuntime*/*/fossilize_replay >/dev/null 2>&1; then
    c_ok "fossilize_replay (Steam runtime — auto-detected at runtime)"
else
    c_warn "fossilize_replay not found — shader precompile needs it."
    c_warn "It ships with Steam's Linux runtime (launch any Proton game once), or: AUR 'fossilize'"
fi

if command -v nvidia-smi >/dev/null || [ -r /sys/module/nvidia/version ]; then
    c_ok "NVIDIA driver detected (compile tracking will use driver-version invalidation)"
else
    c_warn "No NVIDIA driver detected — DLSS features are NVIDIA-only; everything else works"
fi

command -v mangohud  >/dev/null && c_ok "mangohud"  || c_warn "mangohud not installed (optional): sudo pacman -S mangohud"
command -v gamescope >/dev/null && c_ok "gamescope" || c_warn "gamescope not installed (optional): sudo pacman -S gamescope"

[ "$MISSING" -eq 1 ] && { echo; c_err "Fix the required dependencies above, then re-run."; exit 1; }
echo

# ---- install files ----------------------------------------------------------
# purge any previous app files first so no stale code can linger
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
install -m 644 "$SRC_DIR/pcc.py"     "$APP_DIR/pcc.py"
install -m 644 "$SRC_DIR/index.html" "$APP_DIR/index.html"
install -m 644 "$SRC_DIR/README.md"  "$APP_DIR/README.md"
c_ok "app files -> $APP_DIR"

# systemd user service: keeps the backend running permanently
SYSTEMD_DIR="$HOME/.config/systemd/user"
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$SYSTEMD_DIR"
    cat > "$SYSTEMD_DIR/$APP_NAME.service" << SERVICE
[Unit]
Description=Proton Command Center backend
After=network.target

[Service]
ExecStart=$(command -v python3) $APP_DIR/pcc.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
SERVICE
    systemctl --user daemon-reload
    systemctl --user enable --now "$APP_NAME.service" >/dev/null 2>&1         && c_ok "systemd user service enabled — backend runs at login and auto-restarts"         || c_warn "couldn't enable the user service; the launcher will start the backend on demand"
else
    c_warn "systemd user session not available — the launcher will start the backend on demand"
fi

# launcher: start backend if needed, open the UI
cat > "$BIN_DIR/$APP_NAME" << LAUNCHER
#!/usr/bin/env bash
APP_DIR="$APP_DIR"
PORT=8686
alive() { curl -sf -m1 "http://127.0.0.1:\$PORT/api/status" >/dev/null 2>&1; }
if ! alive; then
    if command -v systemctl >/dev/null && systemctl --user cat proton-command-center.service >/dev/null 2>&1; then
        systemctl --user start proton-command-center.service
    else
        nohup python3 "\$APP_DIR/pcc.py" >/dev/null 2>&1 &
    fi
    for _ in \$(seq 1 40); do alive && break; sleep 0.25; done
fi
xdg-open "http://localhost:\$PORT" >/dev/null 2>&1 || \
    echo "Backend running — open http://localhost:\$PORT in your browser"
LAUNCHER
chmod 755 "$BIN_DIR/$APP_NAME"
c_ok "launcher -> $BIN_DIR/$APP_NAME"

cat > "$DESKTOP_DIR/$APP_NAME.desktop" << DESKTOP
[Desktop Entry]
Type=Application
Name=Proton Command Center
Comment=Launch options, DLSS DLLs, and shader caches for Steam games
Exec=$BIN_DIR/$APP_NAME
Icon=steam
Terminal=false
Categories=Game;Utility;
Keywords=steam;proton;dlss;shader;launch;
DESKTOP
chmod 644 "$DESKTOP_DIR/$APP_NAME.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
c_ok "desktop entry -> app launcher menu (Games)"

echo
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) c_warn "$BIN_DIR is not in your PATH — add it, or launch from the app menu" ;;
esac
printf '\n\033[32mInstall successful\033[0m — warnings above (if any) are informational, not errors.\n'
echo "Launch 'Proton Command Center' from your app menu, or run: $APP_NAME"
echo "Note: if you installed via pacman/AUR, use that instead — don't mix the two."
