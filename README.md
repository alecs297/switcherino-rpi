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
sudo apt install -y python3 python3-venv python3-pip git cec-utils libcec-dev openssl
```

### 3 - Debug your environment

Make sure HDMI CEC is working as expected. The following command should return a list of devices connected to your TV

```bash
echo 'scan' | cec-client -s -d 1
```

If this command returns an error, good luck lol.

One debug tip would be to verify CEC is enabled in your TV's settings, and also to try all your HDMI ports (HDMI #3 did not work on the LG C5 for some obscure reason).

At this step you should also note the address of your default HDMI source (eg: Apple TV) and the address of your PC.

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

### 5 - Configure the software

