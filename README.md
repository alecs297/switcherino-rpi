# switcherino-rpi

Utility for controlling an LG TV from a Raspberry Pi Zero W over WebOS.

## Overview

The Raspberry Pi is powered by the TV over USB-PD and connects to Wi-Fi. At boot, it can also expose a second Wi-Fi network so the TV joins the Pi directly. Once the TV is on that hotspot, the API served by the Pi can control the TV through LG WebOS instead of HDMI-CEC.

The current repository contains:

- a FastAPI app in `app.py`
- a hotspot setup script in `scripts/setup_wifi.sh`
- a pairing helper in `scripts/pairing.py`
- helper scripts for certificates and service installation

## Requirements

This project targets a Raspberry Pi Zero W and an LG TV running WebOS.

Important notes:

- the TV must be able to connect to the hotspot exposed by the Pi
- WebOS network control must be available on the TV
- powering the TV on cannot be done through WebOS alone; if you want `turn_on`, configure Wake-on-LAN with the TV MAC address in `config.json`

## TV Settings Checklist

Before pairing the TV with the Pi, review these settings on the TV itself.

Recommended settings:

- disable `Energy Saving` or `Energy Saving Step`
- enable `TV On With Mobile`
- inside `TV On With Mobile`, enable `Turn on via Wi-Fi`

Exact LG menu names vary slightly by webOS version.

Common paths:

- webOS 23: `Settings` -> `General` -> `External Devices` -> `TV On With Mobile` -> `Turn on via Wi-Fi`
- webOS 22 / webOS 6.0: `Settings` -> `General` -> `Devices` -> `External Devices` -> `TV On With Mobile` -> `Turn on via Wi-Fi`
- older models may expose a similar option under `Mobile TV On` or `Mobile Connection Management`

For power saving:

- many models use `Settings` -> `Picture` -> `Energy Saving` -> `Off`
- some newer models expose `Settings` -> `General` -> `OLED Care` -> `Device Self Care` -> `Energy Saving Step` -> `Off`

If these options are enabled differently on your model, keep the same intent:

- avoid aggressive energy saving modes
- keep Wi-Fi wake enabled so the TV can respond to network wake features

## Installation

### 1 - Set up the Pi

Flash Raspberry Pi OS Lite (32-bit) to the microSD card with Raspberry Pi Imager. The headless version is recommended.

Follow the Raspberry Pi getting started guide [here](https://www.raspberrypi.com/documentation/computers/getting-started.html) and make sure the Pi has:

- SSH access
- Internet access
- a working Wi-Fi client connection on `wlan0`

### 2 - Install system packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git openssl hostapd dnsmasq iw
```

### 3 - Disable the default `hostapd` and `dnsmasq` services

Those packages are used directly by the custom hotspot script, not via their stock services.

```bash
sudo systemctl disable --now hostapd.service
sudo systemctl disable --now dnsmasq.service
sudo systemctl mask hostapd.service
sudo systemctl mask dnsmasq.service
```

### 4 - Clone the repository and install Python dependencies

```bash
cd ~
git clone https://github.com/alecs297/switcherino-rpi
cd switcherino-rpi
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Hotspot Setup

Edit [`scripts/setup_wifi.sh`](./scripts/setup_wifi.sh) and adapt at least:

```bash
SSID="PiHotspot"
PASSPHRASE="12345678"
```

You may also want to review:

- `STA_IF`
- `AP_IF`
- `AP_IP_CIDR`

Install the hotspot script as a one-shot systemd service:

```bash
sudo cp scripts/setup_wifi.sh /usr/local/bin/setup_wifi.sh
sudo chmod +x /usr/local/bin/setup_wifi.sh
sudo bash -c 'cat > /etc/systemd/system/pihotspot.service <<EOF
[Unit]
Description=Dynamic Pi Hotspot (client + AP)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/setup_wifi.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable pihotspot.service'
```

Test it manually:

```bash
sudo /usr/local/bin/setup_wifi.sh
```

If the script succeeds, the TV should see the hotspot in its Wi-Fi list and be able to join it.

## Application Setup

### 1 - Create the application config

Run the app once:

```bash
python3 app.py
```

On first start, this creates `config.json`, prints the generated admin key, and exits.

Review `config.json` and adjust the important fields:

```json
{
  "default_target": "HDMI_1",
  "pc_target": "HDMI_2",
  "change_volume": false,
  "tv_mac": ""
}
```

Notes:

- `default_target` and `pc_target` should preferably use WebOS source ids such as `HDMI_1` or `HDMI_2`
- labels are accepted too, but they are not guaranteed to be unique on LG TVs
- if you want the `turn_on` action to work, set `tv_mac` to the TV MAC address for Wake-on-LAN
- if you leave `tv_mac` empty, `turn_on` will return an error by design

### 2 - Generate HTTPS certificates

```bash
./scripts/gen_certs.sh
```

### 3 - Pair the Pi with the TV

Make sure the TV is connected to the Pi hotspot, then run:

```bash
python3 scripts/pairing.py
```

The script will:

- ask whether the TV is already connected to the hotspot
- ask for the TV IP, or try to discover it automatically if left blank
- initiate WebOS pairing
- ask for the code shown on the TV if needed
- save everything required for future connections in `pairing.json`

If auto-discovery finds more than one TV, rerun the script and enter the IP manually.

The generated `pairing.json` is required by `app.py` and is intentionally ignored by git.

### 4 - Start the API

```bash
python3 app.py
```

By default the API listens on:

- `https://0.0.0.0:8443`
- with HTTP Basic auth using `admin_username` and `admin_key` from `config.json`

Interactive docs are available at:

- `/docs`
- `/redoc`

## API Behavior

Main API routes:

- `GET /tv/status`
- `POST /tv/action`

Hidden compatibility aliases also exist:

- `GET /webos/status`
- `POST /webos/action`

Supported actions:

- `turn_on`
- `turn_off`
- `change_source`
- `game`
- `default`

Action behavior:

- `change_source` switches to a source identified by id or label
- `game` switches to `pc_target`
- `default` switches to `default_target`
- `turn_off` powers the TV off via WebOS
- `turn_on` sends a Wake-on-LAN packet and therefore requires `tv_mac`

Example request:

```bash
curl -k -u admin:YOUR_ADMIN_KEY \
  -H "Content-Type: application/json" \
  -d '{"action":"change_source","target":"HDMI_1"}' \
  https://PI_IP:8443/tv/action
```

Example `GET /tv/status` response shape:

```json
{
  "ok": true,
  "status": {
    "host": "192.168.50.46",
    "secure": true,
    "system": {
      "product_name": "webOSTV 24",
      "model_name": "HE_DTV_W24G_AFABATAA",
      "major_ver": "23",
      "minor_ver": "20.39",
      "device_id": "f8:01:b4:d2:c6:5a"
    },
    "current_app": "com.webos.app.hdmi4",
    "volume": {
      "volumeStatus": {
        "volume": 13,
        "muteStatus": false,
        "soundOutput": "tv_speaker"
      }
    },
    "sources": [
      {
        "id": "HDMI_1",
        "label": "PC",
        "connected": true
      },
      {
        "id": "HDMI_2",
        "label": "PC",
        "connected": true
      },
      {
        "id": "HDMI_4",
        "label": "Apple OTT",
        "connected": true
      }
    ],
    "default_target": "HDMI_1",
    "pc_target": "HDMI_2",
    "volume_control_enabled": false
  }
}
```

Important interpretation notes:

- `sources[*].id` is the safest value to use in `config.json` and in `change_source`
- `sources[*].label` may be duplicated; for example both `HDMI_1` and `HDMI_2` can be labeled `PC`
- `current_app` usually reflects the active HDMI app, such as `com.webos.app.hdmi4`
- the `raw` payload returned by the API contains extra LG metadata such as `appId`, `port`, signal presence, and EDID-derived device information

## Files

- `config.json`: local app configuration generated on first start
- `pairing.json`: WebOS pairing credentials and discovered source metadata
- `certs/server.crt` and `certs/server.key`: HTTPS certificate material

## Caveats

- WebOS cannot power on the TV by itself; Wake-on-LAN is the fallback
- source ids vary between TV models, so verify the values saved in `pairing.json` or returned by `/tv/status`
- source labels may be ambiguous, so prefer `HDMI_1`, `HDMI_2`, and similar ids over labels such as `PC`
- the pairing flow depends on the TV model and WebOS version; some TVs show a code, others only ask for confirmation
