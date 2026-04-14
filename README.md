# switcherino-rpi
Raspberry PI zero utility

## Requirements

This project was made specifically for the Raspberry Pi Zero W. It requires the Pi to be connected to the TV through a HDMI CEC cable. It also obviously requires the TV to support CEC and CEC to be enabled.

## Installation

### 1 - Set up the PI

Flash Raspberry Pi OS Lite (32-bit) to the microSD card with Raspberry Pi Imager. The headless version is recommended. Follow the steps listed [here](https://www.raspberrypi.com/documentation/computers/getting-started.html) to get started, harden your SSH configuration and configure networking (you will want the Pi to be reachable on the local network)

### 2 - Set up the environment

Install the required dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git libcec-dev openssl hostapd dnsmasq iw
```

### 3 - Disable the dnsmasq service

The service has just been installed but won't be used as is. It can therefore be disabled with

```bash
sudo systemctl disable --now hostapd.service
sudo systemctl disable --now dnsmasq.service
sudo systemctl mask hostapd.service
sudo systemctl mask dnsmasq.service
```

### 4 - Get the software

Clone the repo to your home directory (or somewhere else, but adapt the commands)

```bash
cd ~
git clone https://github.com/alecs297/switcherino-rpi
cd switcherino-rpi
```

Create a python virtual environment and install the dependencies

```
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
```

Configure the hotspot service by editing [scripts/setup_wifi.sh](./scripts/setup_wifi.sh) and setting up the following variables :

```bash
SSID="Pi-C2" # Your hotspot's name
PASSPHRASE="12121212" # Your hotspot's password
```

Set up the hotspot service

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

Test the hotspot service. The command should be a success and an AP should appear on your TV's available WIFI networks. You can proceed to connect the TV. If this doesn't work, good luck troubleshooting !

```bash
sudo /usr/local/bin/setup_wifi.sh
```


### 5 - Configure the software

