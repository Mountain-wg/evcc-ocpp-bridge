# evcc to OCPP Bridge

A lightweight, bidirectional translation bridge (**OCPP 1.6-J Bridge**) that seamlessly connects an [evcc](https://evcc.io)-managed wallbox (e.g. openWB, go-e, cFos) to the CPO and GHG quota billing backend of **PUMP connect**.

## Why this bridge?

evcc acts locally as an intelligent master for PV surplus charging, home battery prioritization, and vehicle detection. PUMP connect serves as a backend for public or semi-public billing, roaming, and GHG quota verification.

This bridge connects both worlds:
- PUMP controls transactions via OCPP 1.6-J remote start/stop.
- evcc switches the wallbox precisely and regulates charging sessions.
- Real meter values (kWh/Wh) and power readings (W) flow directly from evcc into the PUMP billing system.

## Features

- **Standard-compliant OCPP 1.6-J:**
  - `BootNotification` with custom version and vendor identification
  - `StatusNotification` with precise state differentiation (`Available`, `SuspendedEVSE`, `Charging`)
  - Cyclic `Heartbeat` and `MeterValues` (accumulated Wh and active power in W)
  - `Reset` and `UnlockConnector` directly from the PUMP dashboard
- **Billing Security:**
  - Uses the absolute hardware meter reading (`chargeTotalImport`) for `meterStart` and `meterStop`.
  - On `RemoteStop` via PUMP, charging is immediately set to `off` and verified until 0 W is actually flowing.
- **Intelligent Mode Logic (Guest vs. Private):**
  - **Guest / Third-party charger:** After stopping in PUMP, the box remains strictly locked at `off`. No free continued charging at private expense.
  - **Cable disconnection:** The slot is only released once the cable is unplugged.
  - **Private vehicle:** As soon as a vehicle is freshly plugged in, evcc automatically switches back to `pv` mode (smart surplus).

## Prerequisites

- Linux system (Debian, Ubuntu, or Proxmox LXC Container)
- Python 3.10 or newer
- Running evcc instance with accessible REST API (`http://127.0.0.1:7070`)

## Installation

### 1. Clone the repository

git clone https://github.com/Mountain-wg/evcc-ocpp-bridge.git /opt/evcc-ocpp-bridge
cd /opt/evcc-ocpp-bridge

### 2. Set up virtual environment & install dependencies

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

### 3. Create configuration

cp .env.example .env
nano .env

Enter your WebSocket URL from the PUMP Connect portal:
PUMP_OCPP_URL=wss://ocpp.800volt.de/1.6J/id/your-chargebox-id
EVCC_API_URL=http://127.0.0.1:7070
EVCC_LOADPOINT_ID=1
METER_INTERVAL=60

### 4. Enable systemd service

cp evcc-ocpp-bridge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now evcc-ocpp-bridge.service

Check status:
journalctl -u evcc-ocpp-bridge.service -f

## License

MIT License
