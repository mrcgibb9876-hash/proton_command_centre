#!/usr/bin/env bash
# ============================================================================
#  Proton Command Center - LOCAL TEST INSTALLER
#  Removes ALL previous backends (pacman package, user service, orphans,
#  stale port holders) before installing, so nothing old can keep serving.
# ============================================================================
set -euo pipefail

APP_NAME="proton-command-center"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_HOME/$APP_NAME/app"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
SVC_DIR="$HOME/.config/systemd/user"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Single source of truth: read it from pcc.py rather than hardcoding, which
# previously shipped a stale "1.5.0" into the banner AND the systemd unit
# Description, so journalctl reported the wrong version forever.
VER="$(grep -m1 -o 'VERSION = "[0-9][0-9.]*"' "$SRC/pcc.py" | grep -o '[0-9][0-9.]*' || true)"
[ -n "$VER" ] || { printf '\033[31mfail\033[0m  cannot read VERSION from %s/pcc.py\n' "$SRC"; exit 1; }

g(){ printf '\033[32m  ok\033[0m  %s\n' "$1"; }
y(){ printf '\033[33mwarn\033[0m  %s\n' "$1"; }
r(){ printf '\033[31mfail\033[0m  %s\n' "$1"; }

# --- safety guard: never let a bad HOME turn rm -rf loose ---------------------
[ -n "${HOME:-}" ] && [ "$HOME" != "/" ] && [ -d "$HOME" ] || { r "unsafe HOME"; exit 1; }
case "$APP_DIR" in
  /*"/$APP_NAME/app") : ;;
  *) r "unsafe app path: $APP_DIR"; exit 1 ;;
esac

echo "== Proton Command Center $VER - local test install =="
echo

# --- 1. tear down EVERY previous backend -------------------------------------
echo ":: removing old backends"

# stop + disable the user service if present
if systemctl --user list-unit-files 2>/dev/null | grep -q "$APP_NAME.service"; then
    systemctl --user stop "$APP_NAME.service" 2>/dev/null || true
    systemctl --user disable "$APP_NAME.service" 2>/dev/null || true
    g "stopped & disabled user service"
fi

# remove any user-level unit (this is the one that shadows pacman + causes drift)
if [ -f "$SVC_DIR/$APP_NAME.service" ]; then
    rm -f "$SVC_DIR/$APP_NAME.service"
    g "removed user service unit"
fi

# kill any running backend by script path (catches orphans systemd forgot)
pkill -f "$APP_NAME/app/pcc.py" 2>/dev/null && g "killed user-install backend" || true
pkill -f "/usr/share/$APP_NAME/pcc.py" 2>/dev/null && g "killed packaged backend" || true

# free the port no matter what still holds it
if command -v fuser >/dev/null 2>&1; then
    fuser -k 8686/tcp 2>/dev/null && g "cleared port 8686" || true
fi

# warn about (but don't touch) a pacman package - user removes that deliberately
if command -v pacman >/dev/null 2>&1 && pacman -Q "$APP_NAME" >/dev/null 2>&1; then
    y "pacman package still installed: $(pacman -Q $APP_NAME)"
    echo "     This local install will REPLACE it as the active backend, but the"
    echo "     package stays in pacman's database. To remove it fully:"
    echo "         sudo pacman -R $APP_NAME"
fi

systemctl --user daemon-reload 2>/dev/null || true

# --- 2. purge stale app files (only if clearly ours) -------------------------
if [ -d "$APP_DIR" ]; then
    if [ -f "$APP_DIR/pcc.py" ] || [ -z "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
        rm -rf -- "$APP_DIR"
        g "purged old app files"
    else
        r "$APP_DIR exists but isn't ours - not touching it"; exit 1
    fi
fi

# --- 3. install --------------------------------------------------------
echo
echo ":: installing $VER"
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$SVC_DIR"
install -Dm644 "$SRC/pcc.py"     "$APP_DIR/pcc.py"
install -Dm644 "$SRC/index.html" "$APP_DIR/index.html"
g "app files -> $APP_DIR"

cat > "$BIN_DIR/$APP_NAME" << 'LAUNCHER'
#!/usr/bin/env bash
# Ensure the backend is running (via its service), wait for it, open the UI.
URL="http://localhost:8686"
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user start proton-command-center.service 2>/dev/null || true
fi
# if the service isn't managing it for some reason, start it directly
if ! curl -sf -m1 "$URL/api/status" >/dev/null 2>&1; then
    nohup python3 "$HOME/.local/share/proton-command-center/app/pcc.py" \
        >/dev/null 2>&1 &
fi
for _ in $(seq 1 30); do
    curl -sf -m1 "$URL/api/status" >/dev/null 2>&1 && break
    sleep 0.3
done
if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1
else
    echo "Open $URL in your browser."
fi
LAUNCHER
chmod +x "$BIN_DIR/$APP_NAME"
g "launcher -> $BIN_DIR/$APP_NAME"

cat > "$SVC_DIR/$APP_NAME.service" << EOF
[Unit]
Description=Proton Command Center backend (local $VER)
After=network.target

[Service]
ExecStart=/usr/bin/python3 $APP_DIR/pcc.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
g "service -> $SVC_DIR"

cat > "$DESKTOP_DIR/$APP_NAME.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Proton Command Center (local)
Comment=Steam/Proton gaming toolkit
Exec=$BIN_DIR/$APP_NAME
Icon=steam
Terminal=false
Categories=Game;Utility;
EOF
g "desktop entry -> app menu"

systemctl --user daemon-reload
systemctl --user enable --now "$APP_NAME.service"
g "service enabled & started"

# --- 4. verify the RUNNING backend is actually $VER -------------------------
echo
echo ":: verifying"
FILE_VER=$(grep -o 'VERSION = "[0-9.]*"' "$APP_DIR/pcc.py" | grep -o '[0-9][0-9.]*')
RUN_VER=""
for _ in $(seq 1 20); do
    RUN_VER=$(curl -sf -m1 "http://127.0.0.1:8686/api/status" 2>/dev/null \
              | grep -o '"version": "[0-9.]*"' | grep -o '[0-9][0-9.]*') \
        && [ -n "$RUN_VER" ] && break
    sleep 0.5
done
if [ "$RUN_VER" = "$FILE_VER" ]; then
    g "running backend verified: v$RUN_VER"
    echo
    printf '\033[32mDone.\033[0m Open http://localhost:8686  (hard-refresh: Ctrl+Shift+R)\n'
elif [ -n "$RUN_VER" ]; then
    y "running v$RUN_VER but installed v$FILE_VER - run: systemctl --user restart $APP_NAME"
else
    y "backend not responding yet - try: systemctl --user status $APP_NAME"
fi
