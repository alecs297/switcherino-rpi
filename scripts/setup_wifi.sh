#!/usr/bin/env bash
set -euo pipefail

STA_IF="wlan0"
AP_IF="wlan0_ap"

SSID="PiHotspot"
PASSPHRASE="12345678"

AP_IP_CIDR="192.168.50.1/24"

HOSTAPD_CONF="/etc/hostapd/hostapd.conf"
DNSMASQ_CONF="/etc/dnsmasq.d/pihotspot.conf"

log() {
  echo "[setup_wifi] $*"
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root"
    exit 1
  fi
}

require_cmds() {
  local missing=0
  for cmd in iw ip awk hostapd dnsmasq pkill; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "Missing command: $cmd"
      missing=1
    fi
  done
  if [[ "$missing" -ne 0 ]]; then
    exit 1
  fi
}

freq_to_channel() {
  local freq="$1"

  if [[ "$freq" -eq 2484 ]]; then
    echo 14
    return 0
  elif [[ "$freq" -ge 2412 && "$freq" -le 2472 ]]; then
    echo $(( (freq - 2407) / 5 ))
    return 0
  fi

  return 1
}

wait_for_wlan() {
  local tries=20
  while [[ "$tries" -gt 0 ]]; do
    if iw dev "$STA_IF" info >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    tries=$((tries - 1))
  done

  log "Interface $STA_IF not ready"
  exit 1
}

get_connected_channel() {
  local freq
  freq="$(iw dev "$STA_IF" link 2>/dev/null | awk '/freq:/ {print int($2); exit}')"
  if [[ -n "${freq:-}" ]]; then
    freq_to_channel "$freq" || true
  fi
}

scan_best_channel() {
  local best_chan=""
  local best_signal="-999"

  iw dev "$STA_IF" scan 2>/dev/null | awk '
    /^BSS / {
      if (freq != "" && sig != "") print freq, sig
      freq=""
      sig=""
    }
    /freq:/ {freq=$2}
    /signal:/ {sig=$2}
    END {
      if (freq != "" && sig != "") print freq, sig
    }
  ' | while read -r freq sig; do
    chan="$(freq_to_channel "$freq" 2>/dev/null || true)"
    [[ -n "${chan:-}" ]] || continue
    printf '%s %s\n' "$chan" "$sig"
  done > /tmp/pihotspot_scan.$$ || true

  if [[ -s /tmp/pihotspot_scan.$$ ]]; then
    while read -r chan sig; do
      awk "BEGIN {exit !($sig > $best_signal)}" && {
        best_signal="$sig"
        best_chan="$chan"
      }
    done < /tmp/pihotspot_scan.$$
  fi

  rm -f /tmp/pihotspot_scan.$$
  echo "${best_chan:-6}"
}

cleanup_ap_interface() {
  pkill dnsmasq 2>/dev/null || true
  pkill hostapd 2>/dev/null || true

  if iw dev | grep -q "Interface ${AP_IF}\b"; then
    ip addr flush dev "$AP_IF" 2>/dev/null || true
    ip link set "$AP_IF" down 2>/dev/null || true
    iw dev "$AP_IF" del 2>/dev/null || true
    sleep 1
  fi
}

write_hostapd_conf() {
  local channel="$1"

  mkdir -p "$(dirname "$HOSTAPD_CONF")"

  cat > "$HOSTAPD_CONF" <<EOF
interface=${AP_IF}
driver=nl80211
ssid=${SSID}
hw_mode=g
channel=${channel}
country_code=BE
ieee80211d=1
ieee80211n=0
wmm_enabled=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=${PASSPHRASE}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF
}

write_dnsmasq_conf() {
  mkdir -p "$(dirname "$DNSMASQ_CONF")"

  cat > "$DNSMASQ_CONF" <<EOF
interface=${AP_IF}
bind-interfaces
dhcp-range=192.168.50.10,192.168.50.100,255.255.255.0,24h
dhcp-option=6,9.9.9.9,1.1.1.1
EOF
}

start_hotspot() {
  local channel="$1"

  log "Using channel $channel"

  cleanup_ap_interface

  iw dev "$STA_IF" interface add "$AP_IF" type __ap
  sleep 1

  if ! iw dev | grep -q "Interface ${AP_IF}\b"; then
    log "Failed to create ${AP_IF}"
    iw dev || true
    exit 1
  fi

  write_hostapd_conf "$channel"
  write_dnsmasq_conf

  hostapd -B "$HOSTAPD_CONF"
  sleep 2

  ip addr flush dev "$AP_IF" 2>/dev/null || true
  ip addr add "$AP_IP_CIDR" dev "$AP_IF"

  dnsmasq --conf-file="$DNSMASQ_CONF"

  log "Hotspot started on ${AP_IF} at ${AP_IP_CIDR}"
}

main() {
  require_root
  require_cmds
  wait_for_wlan

  local channel=""
  channel="$(get_connected_channel || true)"

  if [[ -n "${channel:-}" ]]; then
    log "$STA_IF is connected; reusing channel $channel"
  else
    channel="$(scan_best_channel)"
    log "$STA_IF not connected; strongest nearby 2.4 GHz channel is $channel"
  fi

  start_hotspot "$channel"
}

main "$@"