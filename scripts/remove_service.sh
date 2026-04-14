#!/usr/bin/env bash
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
  echo "This script must be run with sudo."
  echo "Usage: sudo ./remove_service.sh"
  exit 1
fi

DEFAULT_NAME="webos-tv-api"

echo "=== Remove WebOS TV API systemd service ==="
echo

read -r -p "Service name [${DEFAULT_NAME}]: " INPUT_NAME
SERVICE_BASENAME="${INPUT_NAME:-$DEFAULT_NAME}"
SERVICE_NAME="${SERVICE_BASENAME}.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

if [[ ! -f "${SERVICE_PATH}" ]]; then
  echo "Service file not found:"
  echo "  ${SERVICE_PATH}"
  exit 1
fi

echo
echo "This will:"
echo "  Stop   ${SERVICE_NAME}"
echo "  Disable ${SERVICE_NAME}"
echo "  Remove ${SERVICE_PATH}"
echo

read -r -p "Proceed? [y/N]: " CONFIRM
if [[ "${CONFIRM,,}" != "y" ]]; then
  echo "Aborted."
  exit 1
fi

if systemctl list-unit-files | grep -q "^${SERVICE_NAME}"; then
  echo "Stopping service..."
  systemctl stop "${SERVICE_NAME}" || true

  echo "Disabling service..."
  systemctl disable "${SERVICE_NAME}" || true
fi

echo "Removing service file..."
rm -f "${SERVICE_PATH}"

echo "Reloading systemd..."
systemctl daemon-reload
systemctl reset-failed || true

echo
echo "Removed ${SERVICE_NAME}."
