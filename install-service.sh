#!/bin/bash
# Install or update the spe-remote systemd service.
#
#   sudo ./install-service.sh             install (or re-install) the service.
#                                         Existing config.yaml is left untouched,
#                                         so the serial port and Flex IP are kept.
#   sudo ./install-service.sh --update    re-run the interactive configurator
#                                         (shows a diff, lets you keep the saved
#                                         port + Flex IP), then restart the
#                                         running service with the new config.
#   sudo ./install-service.sh --help      show this help.
set -e

RECONFIGURE=0
for arg in "$@"; do
    case "$arg" in
        --update | --reconfigure)
            RECONFIGURE=1
            ;;
        -h | --help)
            # Print the leading comment block (minus the shebang) as help.
            awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            echo "Try: sudo ./install-service.sh --help" >&2
            exit 1
            ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "This script must be run with sudo:"
    echo "  sudo ./install-service.sh"
    exit 1
fi

WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$(logname 2>/dev/null || echo "")}"

if [ -z "$USER_NAME" ] || [ "$USER_NAME" = "root" ]; then
    echo "Could not determine the regular user to run as."
    echo "Re-run with sudo from a normal shell, e.g.:"
    echo "  sudo ./install-service.sh"
    exit 1
fi

if [ ! -x "$WORK_DIR/venv/bin/python" ]; then
    echo "Virtual environment not found at $WORK_DIR/venv"
    echo "Run ./setup.sh first to create it."
    exit 1
fi

if ! id "$USER_NAME" >/dev/null 2>&1; then
    echo "User '$USER_NAME' does not exist."
    exit 1
fi

# Make sure the user can read/write the serial port (best effort).
if id "$USER_NAME" | grep -qv "dialout"; then
    echo "Note: user $USER_NAME is not in the 'dialout' group."
    echo "      Adding it now (takes effect after next login)."
    usermod -aG dialout "$USER_NAME"
fi

# --update / --reconfigure: re-run the interactive configurator before we
# restart the service. Run it as the regular user (not root) so config.yaml
# stays owned by the service user — the dashboard rewrites this file in place
# (e.g. the °C/°F toggle) and must keep write access to it.
if [ "$RECONFIGURE" -eq 1 ]; then
    echo "Updating configuration before restarting the service..."
    echo
    if [ ! -x "$WORK_DIR/configure.sh" ]; then
        echo "configure.sh not found or not executable at $WORK_DIR" >&2
        exit 1
    fi
    sudo -u "$USER_NAME" bash "$WORK_DIR/configure.sh"
    echo
fi

SERVICE_NAME="spe-remote"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TEMPLATE="$WORK_DIR/systemd/spe-remote.service.template"

if [ ! -f "$TEMPLATE" ]; then
    echo "Service template not found at $TEMPLATE"
    exit 1
fi

echo "Installing $SERVICE_NAME service:"
echo "  User:           $USER_NAME"
echo "  Working dir:    $WORK_DIR"
echo "  Service file:   $SERVICE_FILE"
echo

# Stop the running service if we're re-installing.
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "Stopping existing $SERVICE_NAME..."
    systemctl stop "$SERVICE_NAME"
fi

# Render the template — escape '|' substitutions so paths with weird
# characters don't break sed (they shouldn't, but defensively).
sed \
    -e "s|{{USER}}|${USER_NAME}|g" \
    -e "s|{{WORKDIR}}|${WORK_DIR}|g" \
    "$TEMPLATE" >"$SERVICE_FILE"

chmod 644 "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

# Brief pause then status check.
sleep 1
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo
    echo "Service is running."
else
    echo
    echo "WARNING: service did not start cleanly. Check:"
    echo "  sudo systemctl status $SERVICE_NAME"
    echo "  sudo journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi

cat <<EOF

Useful commands:
  sudo systemctl status $SERVICE_NAME
  sudo systemctl restart $SERVICE_NAME
  sudo systemctl stop $SERVICE_NAME
  sudo journalctl -u $SERVICE_NAME -f       # live logs
  sudo journalctl -u $SERVICE_NAME -n 100   # last 100 lines

Change the serial port or Flex radio later (shows a diff, keeps your
saved settings unless you change them, then restarts the service):
  sudo ./install-service.sh --update

Open the dashboard:  http://$(hostname -I | awk '{print $1}'):8888/

To remove the service later:
  sudo ./uninstall-service.sh
EOF
