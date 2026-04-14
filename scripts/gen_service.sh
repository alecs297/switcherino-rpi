#!/usr/bin/env bash
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
  echo "This script must be run with sudo."
  echo "Usage: sudo ./gen_service.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_USER="${SUDO_USER:-$(whoami)}"
DEFAULT_WORKDIR="${SCRIPT_DIR}"
DEFAULT_APP="${SCRIPT_DIR}/app.py"
DEFAULT_VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python"

if [[ ! -f "${DEFAULT_APP}" ]]; then
  echo "Could not find app.py at:"
  echo "  ${DEFAULT_APP}"
  echo "Run this script from your project directory."
  exit 1
fi

if [[ ! -x "${DEFAULT_VENV_PYTHON}" ]]; then
  echo "Could not find a virtualenv Python at:"
  echo "  ${DEFAULT_VENV_PYTHON}"
  exit 1
fi

echo "=== WebOS TV API systemd service setup ==="
echo

read -r -p "Service name [webos-tv-api]: " INPUT_NAME
SERVICE_BASENAME="${INPUT_NAME:-webos-tv-api}"
SERVICE_NAME="${SERVICE_BASENAME}.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

read -r -p "Run as user [${DEFAULT_USER}]: " SERVICE_USER
SERVICE_USER="${SERVICE_USER:-$DEFAULT_USER}"

read -r -p "Working directory [${DEFAULT_WORKDIR}]: " WORKDIR
WORKDIR="${WORKDIR:-$DEFAULT_WORKDIR}"

DEFAULT_EXEC="${DEFAULT_VENV_PYTHON} ${DEFAULT_APP}"
read -r -p "ExecStart command [${DEFAULT_EXEC}]: " EXEC_CMD
EXEC_CMD="${EXEC_CMD:-$DEFAULT_EXEC}"

echo
echo "Validating paths..."

if [[ ! -d "${WORKDIR}" ]]; then
  echo "Working directory does not exist:"
  echo "  ${WORKDIR}"
  exit 1
fi

EXEC_BIN="$(awk '{print $1}' <<< "${EXEC_CMD}")"
if [[ ! -x "${EXEC_BIN}" ]]; then
  echo "ExecStart binary is not executable or does not exist:"
  echo "  ${EXEC_BIN}"
  exit 1
fi

echo "Configuration:"
echo "  Name: ${SERVICE_NAME}"
echo "  User: ${SERVICE_USER}"
echo "  Workdir: ${WORKDIR}"
echo "  Exec: ${EXEC_CMD}"
echo

read -r -p "Proceed? [y/N]: " CONFIRM
if [[ "${CONFIRM,,}" != "y" ]]; then
  echo "Aborted."
  exit 1
fi

cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=LG WebOS TV API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${WORKDIR}
ExecStart=${EXEC_CMD}
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

echo
echo "Wrote service file:"
echo "  ${SERVICE_PATH}"

echo "Reloading systemd..."
systemctl daemon-reload

echo "Enabling service..."
systemctl enable "${SERVICE_NAME}"

echo "Starting service..."
systemctl start "${SERVICE_NAME}"

echo
echo "Service status:"
systemctl status "${SERVICE_NAME}" --no-pager

echo
echo "Done."
echo "Follow logs with:"
echo "  journalctl -u ${SERVICE_NAME} -f"
