#!/usr/bin/env bash
#
# Install the Last Hunt watcher as a systemd service on Debian/Ubuntu.
#
#   ./install-systemd.sh              # user service (no root, starts at logon)
#   sudo ./install-systemd.sh --system  # system service (starts at boot)
#
# Either way the watcher restarts itself automatically if it ever crashes.

set -euo pipefail

SERVICE_NAME="tlhwatch"
MODE="user"

for arg in "$@"; do
    case "$arg" in
        --system) MODE="system" ;;
        --user)   MODE="user" ;;
        -h|--help)
            sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# --- locate everything -----------------------------------------------------

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SCRIPT="$HERE/tlhwatch.py"

if [[ ! -f "$SCRIPT" ]]; then
    echo "error: tlhwatch.py not found next to this installer ($SCRIPT)" >&2
    exit 1
fi

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
    echo "error: python3 is not installed. Try: sudo apt install python3" >&2
    exit 1
fi

if [[ ! -f "$HERE/config.json" ]]; then
    echo "warning: config.json is missing; the watcher will write a default one"
    echo "         on first run, but you must set your ntfy topic in it."
fi

echo "python  : $PYTHON"
echo "watcher : $SCRIPT"
echo "mode    : $MODE service"
echo

# --- build the unit --------------------------------------------------------

if [[ "$MODE" == "system" ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "error: --system needs root. Re-run with sudo." >&2
        exit 1
    fi
    # Run as the human who invoked sudo, not as root.
    RUN_AS="${SUDO_USER:-root}"
    UNIT_DIR="/etc/systemd/system"
    SYSTEMCTL=(systemctl)
    EXTRA_UNIT="After=network-online.target
Wants=network-online.target"
    EXTRA_SERVICE="User=$RUN_AS
Group=$(id -gn "$RUN_AS")"
    WANTED_BY="multi-user.target"
    echo "will run as user: $RUN_AS"
else
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    SYSTEMCTL=(systemctl --user)
    # network-online.target is a system target; it has no meaning in the user
    # manager, and the watcher retries on its own anyway.
    EXTRA_UNIT=""
    EXTRA_SERVICE=""
    WANTED_BY="default.target"
fi

mkdir -p "$UNIT_DIR"
UNIT_PATH="$UNIT_DIR/$SERVICE_NAME.service"

cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=The Last Hunt stock watcher
Documentation=file://$HERE/README.md
$EXTRA_UNIT
# Never stop trying to restart, no matter how often it has failed.
StartLimitIntervalSec=0

[Service]
Type=simple
$EXTRA_SERVICE
WorkingDirectory=$HERE
ExecStart=$PYTHON $SCRIPT
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10

[Install]
WantedBy=$WANTED_BY
UNIT

echo "wrote $UNIT_PATH"

# --- enable and start ------------------------------------------------------

"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" enable --now "$SERVICE_NAME.service"

echo
echo "Installed and started."
echo

if [[ "$MODE" == "user" ]]; then
    # Without lingering, a user service is killed at logout and only comes
    # back at the next login - which would silently pause the watch.
    if loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q 'Linger=yes'; then
        echo "Lingering is already enabled, so the watcher also survives logout."
    else
        echo "To keep watching after you log out (and to start it at boot),"
        echo "enable lingering once:"
        echo
        echo "    sudo loginctl enable-linger $USER"
    fi
    echo
    echo "  status : systemctl --user status $SERVICE_NAME"
    echo "  logs   : journalctl --user -u $SERVICE_NAME -f"
    echo "  stop   : systemctl --user stop $SERVICE_NAME"
else
    echo "  status : systemctl status $SERVICE_NAME"
    echo "  logs   : journalctl -u $SERVICE_NAME -f"
    echo "  stop   : systemctl stop $SERVICE_NAME"
fi
