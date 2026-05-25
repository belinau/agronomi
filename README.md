# AgroNomi Fleet

A multi-node farm telemetry and control fleet running on [Reticulum](https://reticulum.network/) mesh networking. Multiple ESP32 sensor and actuator nodes communicate over LoRa (RNode), WiFi, and BLE with a central hub, using encrypted LXMF messaging. Sensors deep-sleep between readings, actuators stay awake for instant command response — all coordinated through a self-organising mesh that requires no internet connection.

Sensor nodes measure temperature, humidity, soil moisture, and battery voltage — then send readings over LoRa or WiFi to a hub that logs everything to a database. Actuator nodes stay awake to receive commands like turning pumps on/off or opening greenhouse vents.

## Attributions

This project builds on three open-source projects:

- **[µReticulum](https://github.com/X5don/uP-reticulum)** by [varna9000](https://github.com/varna9000) — the MicroPython RNS stack that runs on every ESP32 node. The `urns/` library in each node's firmware is a direct copy from this project. We added custom BLE interfaces (`RNodeBLEInterface` for RAK4631 RNode pairing, `BLEClientInterface` for gateway bridging) that are not in upstream µReticulum. Licensed under MIT.
- **[Reticulum](https://github.com/markqvist/Reticulum)** by [markqvist](https://github.com/markqvist) — the full Python RNS implementation that runs on the hub (Mac Mini). The `reticulum_ingest.py` hub script uses RNS and LXMF directly. Licensed under MIT.
- **[MicroPython](https://micropython.org/)** — the Python runtime on ESP32 microcontrollers. Licensed under the MIT License.
- **[RNode Firmware](https://github.com/markqvist/RNode_Firmware)** by [markqvist](https://github.com/markqvist) — the firmware running on the RAK4631 RNode that bridges BLE to LoRa. We use it unmodified — no custom firmware changes. Licensed under MIT.

The `esp32c6/repo/` directory contains the upstream µReticulum repository for reference and firmware builds. It is not a git submodule — we copy the `urns/` and `lib/` directories from it into each node's firmware when flashing.

## What it does

- **Sensor nodes** wake up every 5 minutes, read their sensors, send telemetry over the mesh, listen for commands for 5 seconds, then go back to sleep. Battery life is measured in months.
- **Actuator nodes** stay awake permanently, sending status updates every minute and responding to commands in real time.
- **The hub** (Mac Mini) collects all telemetry into SQLite, discovers new nodes automatically, and can send commands to any actuator.

All communication uses LXMF (encrypted messaging over Reticulum mesh). No JSON, no HTTP, no MQTT — just native Reticulum packets over LoRa and WiFi.

## Nodes

| Node | What it measures / controls | Power |
|------|-----|------|
| **SN-AIR** | Air temperature, humidity, battery voltage | Deep sleep (5 min) |
| **SN-SOIL** | Soil moisture, soil temperature, battery voltage | Deep sleep (5 min) |
| **GW-SUPPORT** | Battery voltage only (gateway support node) | Deep sleep (5 min) |
| **AN-PUMP** | Pump relay, battery voltage | Always on |
| **AN-GREENHOUSE** | Vent relay, shade PWM (0–100%), fan relay, battery voltage | Always on |

## How data flows

![Field pod detail — sensor to hub packet path](documents/agronomi_field_pod_detail.svg)

**Sensor nodes** wake up, announce themselves on `farm.gateway_commands`, discover the hub, send telemetry via LXMF fields (`dev_id`, `type`, `fw`, `bat`, `temp`, `hum`, `if`), listen for commands for 5 seconds, then deep sleep.

**Actuator nodes** stay awake permanently — they announce every 5 min, send telemetry every 60 sec, and receive commands instantly via LXMF. Every command gets an ACK: `{ack: true, cmd_id: 42, cmd: "pump_on", status: "ok"}`.

## Network topology

![AgroNomi mesh overview](documents/agronomi_mesh_overview.svg)

Nodes connect via **BLE to RNode** (LoRa) as primary transport, with **WiFi UDP** as secondary for indoor/greenhouse deployments. The hub runs all interfaces simultaneously.

## Telemetry data

Every node sends a flat set of LXMF fields. Common fields:

| Field | Meaning | Example |
|-------|---------|---------|
| `dev_id` | Node name | `SN-AIR-01` |
| `type` | Device type | `air_node` |
| `fw` | Firmware version | `2.0.0-mr` |
| `bat` | Battery voltage | `2.60` |
| `if` | Interface used | `rnode ble` |

Node-specific fields:

| Node | Extra fields |
|------|-------------|
| SN-AIR | `temp`, `hum`, `air_temp_valid`, `air_humidity_valid` |
| SN-SOIL | `soil_moist`, `soil_temp`, `soil_temp_valid` |
| AN-PUMP | `pump_on` (true/false) |
| AN-GREENHOUSE | `vent_open`, `shade_pct`, `fan_on` |

## Commands

Commands are sent from the hub to actuator nodes as LXMF fields:

| Command | Node | What it does |
|---------|------|---------------|
| `pump_on` | AN-PUMP | Turn pump on |
| `pump_off` | AN-PUMP | Turn pump off |
| `vent_open` | AN-GREENHOUSE | Open vent |
| `vent_close` | AN-GREENHOUSE | Close vent |
| `fan_on` | AN-GREENHOUSE | Turn fan on |
| `fan_off` | AN-GREENHOUSE | Turn fan off |
| `shade_pct` | AN-GREENHOUSE | Set shade 0–100% (with `value` field) |

Actuators send an ACK back:

| Field | Example |
|-------|---------|
| `ack` | `true` |
| `cmd_id` | `42` |
| `cmd` | `pump_on` |
| `status` | `ok` or `error` |
| `error` | *(only on error)* `unknown_command: reboot` |

## Hub setup

The hub runs `reticulum_ingest.py` on the Mac Mini. It needs:

1. **RNS installed** — `pip install rns`
2. **RNode connected** via USB — configured with `rnodeconf`
3. **RNS config** — `~/.reticulum/config` with RNode interface and UDP interface

When a node announces, the hub automatically:
- Discovers the node and records its identity
- Pre-registers the node's `lxmf.delivery` destination hash
- Logs the node to SQLite (`sensor_nodes`, `hardware_devices` tables)

When telemetry arrives, the hub:
- Parses the LXMF fields
- Writes each reading to `sensor_readings` as individual rows (`reading_type`, `value`, `unit`)

## Node setup

### First-time BLE pairing

Each ESP32 node connects to the RNode over BLE. Before a node can operate on battery power, it needs to be paired once with the RNode so both devices save their bond keys.

**You only do this once per device.** After pairing, the bond is saved to flash and the node will automatically reconnect to the same RNode on every boot.

1. Connect the **RNode** to your Mac via USB
2. Connect the **ESP32-C6** to your Mac via USB
3. Edit the serial ports at the top of `pair_rnode.py` to match your devices:
   ```python
   RNODE_PORT = "/dev/cu.usbmodem23401"   # your RNode
   C6_PORT = "/dev/cu.usbmodem23201"       # your ESP32-C6
   ```
4. Run the pairing script:
   ```bash
   python3 pair_rnode.py
   ```
5. The script will:
   - Put the RNode into pairing mode and read its 6-digit PIN
   - Write the PIN to `ble_pin.txt` on the ESP32-C6
   - Force a fresh pairing session on the C6
   - Reboot the C6 and start `import main`
   - Show interleaved logs from both devices
6. When you see `[RNode BLE] Device is already bonded` and the node connects, pairing is done. Press Ctrl+C to exit.

After this, the ESP32 can run on battery — it will find and connect to the RNode automatically every time it wakes from sleep.

### Flashing

Each ESP32 node needs three things on its filesystem:

1. **MicroPython firmware** (v1.22+) — flashed once with `esptool`
2. **The `urns/` library** — the µReticulum stack, copied from `esp32c6/repo/firmware/urns/`
3. **The node's firmware** — `main.py`, `config.py`, `sensors.py`, `boot.py`, `secrets.py` from the node's `firmware/` folder

**Step 1: Flash MicroPython** (first time only)
```bash
esptool.py --chip esp32c6 erase_flash
esptool.py --chip esp32c6 write_flash -z 0 micropython-esp32c6-1.22.bin
```

**Step 2: Upload µReticulum library**
```bash
mpremote cp -r esp32c6/repo/firmware/urns/ :urns/
mpremote cp -r esp32c6/repo/firmware/lib/ :lib/
```

**Step 3: Upload node firmware**
```bash
mpremote cp sn_air/firmware/main.py :main.py
mpremote cp sn_air/firmware/config.py :config.py
mpremote cp sn_air/firmware/sensors.py :sensors.py
mpremote cp sn_air/firmware/boot.py :boot.py
mpremote cp sn_air/firmware/secrets.py :secrets.py   # edit this file first!
```

**Step 4: Run**
```python
import main
```

Or reboot — `boot.py` runs automatically and launches `main.py`.

> **Updating:** After the initial flash, you only need to repeat steps 2–3 when code changes. MicroPython itself only needs flashing once. If `urns/` hasn't changed, just update the node-specific files (step 3).

### Configuration

Edit `config.py` on each node:

```python
NODE_NAME = "SN-AIR-01"       # Must be unique per node
DEVICE_TYPE = "air_node"       # air_node, soil_node, pump_node, gh_actuator
WIFI_SSID = ""                 # Loaded from secrets.py — leave blank here
WIFI_PASS = ""                 # Loaded from secrets.py — leave blank here
ENABLE_DEEPSLEEP = True         # True for sensors, False for actuators
SLEEP_INTERVAL_SEC = 300        # 5 minutes
```

**WiFi credentials** are stored in `secrets.py` (not tracked by git). Each node's firmware directory has a template:

```python
# secrets.py — fill in your WiFi credentials, this file is gitignored
WIFI_SSID = "YourWiFi"
WIFI_PASS = "YourPassword"
```

If `secrets.py` is missing, the node runs in BLE-only mode (no WiFi). This means:
- A fresh checkout won't accidentally connect to your WiFi
- BLE-only deployments don't need the file at all
- You create `secrets.py` locally per device and never commit it

Interface config in the `CONFIG` dict:

```python
"interfaces": [
    {
        "type": "RNodeBLEInterface",   # BLE → LoRa via RAK4631
        "name": "RNode BLE",
        "frequency": 868000000,        # 868 MHz (EU) or 915 MHz (US)
        "spreadingfactor": 11,         # Must match hub RNode
        "codingrate": 5,
        "txpower": 17,
        "enabled": True,
    },
    {
        "type": "UDPInterface",        # WiFi for indoor/greenhouse
        "name": "WiFi UDP",
        "listen_port": 4242,
        "forward_port": 4242,
        "enabled": True,
    },
]
```

### Adding a new sensor type

1. Copy the `esp32c6/firmware/` template folder to a new directory (e.g. `sn_water/`)
2. Write sensor drivers in `sensors.py` — must expose `read_all(config)` returning a dict with at least `"battery_v"`
3. Add your sensor fields to `_build_telemetry_fields()` in `main.py`
4. Set `NODE_NAME`, `DEVICE_TYPE`, and interface config in `config.py`
5. Flash to ESP32 following the steps above (the `urns/` and `lib/` libraries are the same for all nodes, only the node-specific files change)

## How hub discovery works

This is the key mechanism that makes telemetry delivery reliable.

When a node boots, it doesn't know the hub's LXMF address. It only knows to listen for announces on `farm.gateway_commands`. When the hub announces itself:

1. **Node side** — The node's `_on_announce` callback receives the hub's identity, computes the hub's `lxmf.delivery` destination hash, and seeds `Identity.remember()` so `LXMRouter.send_message()` can find it later.

2. **Hub side** — The `NodeDiscoveryHandler` receives the node's announce, computes the node's `lxmf.delivery` hash, and seeds `RNS.Identity.remember()` so the hub can decrypt incoming telemetry.

Without this seeding, `Identity.recall()` fails silently because RNS stores public keys under **destination hashes**, not identity hashes — and a `farm.gateway_commands` hash is cryptographically different from an `lxmf.delivery` hash.

## Hardware

| Component | Role |
|-----------|------|
| ESP32-C6 Super Mini | Node MCU (MicroPython) |
| RAK4631 RNode | LoRa radio + BLE bridge (connected to Mac Mini USB) — runs [RNode Firmware](https://github.com/markqvist/RNode_Firmware) (unmodified) |
| DHT22 | Air temperature + humidity (SN-AIR) |
| Capacitive soil probe | Soil moisture (SN-SOIL) |
| DS18B20 | Soil temperature (SN-SOIL) |
| Relay module | Pump/vent/fan control (actuators) |
| PWM output | Shade position 0–100% (AN-GREENHOUSE) |

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Hub logs "Pre-registered" but no telemetry | Node not discovering hub | Check node sees hub announce in serial output |
| Node logs "Hub discovered" but no "Telemetry routed" | `send_message` returning None | Node needs `_hub_lxmf_hash` + `Identity.remember()` — check `_on_announce` |
| Hub logs "Cannot send LXMF: unknown identity" | Public key not seeded under correct hash | Both sides must seed `lxmf.delivery` hashes in `_on_announce` / `received_announce` |
| BLE pairing fails | RNode not in pairing mode | Run `rnodeconf --bluetooth-pair` or set `serial_port` in config for auto-pairing |
| Node won't wake from sleep | `ENABLE_DEEPSLEEP = False` in config | Set to `True` for production deployment |

## File structure

```
m_reticulum/
├── sn_air/           # Air temp/humidity sensor
│   └── firmware/
│       ├── main.py       # Sensor firmware
│       ├── config.py      # Node configuration
│       ├── secrets.py      # WiFi credentials (gitignored)
│       ├── sensors.py     # DHT22 + battery drivers
│       └── boot.py        # Minimal boot script
├── sn_soil/          # Soil moisture/temp sensor
│   └── firmware/...
├── sn_support/       # Support/gateway node (battery only)
│   └── firmware/...
├── an_pump/          # Pump actuator
│   └── firmware/...
├── an_greenhouse/    # Greenhouse actuator (vent/shade/fan)
│   └── firmware/...
├── esp32c6/          # Template — copy this to create new nodes
│   └── firmware/
│       ├── main.py
│       ├── config.py
│       ├── secrets.py     # WiFi credentials (gitignored)
│       ├── sensors.py     # Battery ADC driver
│       └── urns/          # µReticulum library (shared)
├── esp32c6/repo/     # Upstream µReticulum repository (reference)
pair_rnode.py             # BLE pairing script
documents/
├── README.md              # This file
└── reticulum_ingest.py   # Hub ingestion engine (Mac Mini)
```

## License

- **AgroNomi application code** (node firmware, hub script): MIT License
- **µReticulum** (`urns/`, `lib/`): MIT License — Copyright (c) varna9000
- **Reticulum**: MIT License — Copyright (c) markqvist
- **RNode Firmware**: MIT License — Copyright (c) markqvist
- **MicroPython**: MIT License — Copyright (c) Damien P. George