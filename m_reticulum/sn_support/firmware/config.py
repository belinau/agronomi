"""µReticulum — Support Node Configuration (GW-SUPPORT-01)

Hardware: ESP32-C6 Super Mini
Transport: BLE → RAK4631 RNode (primary), WiFi UDP (secondary)
Sensors: battery ADC only
"""

# ---- Node identity ----
NODE_NAME = "GW-SUPPORT-01"
DEVICE_TYPE = "support_node"
FIRMWARE_VERSION = "2.0.0-mr"

# ---- WiFi ----
# Credentials are loaded from secrets.py (not tracked by git).
# If secrets.py is missing, WiFi is disabled (BLE-only mode).
WIFI_SSID = ""
WIFI_PASS = ""
try:
    from secrets import WIFI_PASS as _pass
    from secrets import WIFI_SSID as _ssid

    WIFI_SSID = _ssid
    WIFI_PASS = _pass
except ImportError:
    pass

# ---- Deep sleep ----
ENABLE_DEEPSLEEP = False  # True for production
SLEEP_INTERVAL_SEC = 300

# ---- Battery ADC (ESP32-C6 Super Mini, 100k/100k divider) ----
BAT_ADC_PIN = 1
BAT_DIVIDER_RATIO = 2.0

# ---- Logging: 0=silent 1=info 2=debug ----
DEBUG = 1

# ---- RNS interfaces ----
# No serial_port — this runs on the C6, not the Mac.
# BLE PIN is injected at runtime from ble_pin.txt by pair_rnode.py.
CONFIG = {
    "loglevel": 2,
    "enable_transport": False,
    "interfaces": [
        {
            "type": "RNodeBLEInterface",
            "name": "RNode BLE",
            "target_name": "",  # auto-discover by NUS UUID
            "pairing_passkey": 0,  # overwritten from ble_pin.txt at boot
            "frequency": 868000000,
            "bandwidth": 125000,
            "spreadingfactor": 11,
            "codingrate": 5,
            "txpower": 17,
            "enabled": True,
        },
        {
            "type": "UDPInterface",
            "name": "WiFi UDP",
            "listen_port": 4242,
            "forward_port": 4242,
            "enabled": True,
        },
    ],
}

# ---- Hub destinations ----
TELEMETRY_APP = "farm"
TELEMETRY_ASPECT = "telemetry_readings"
COMMAND_APP = "farm"
COMMAND_ASPECT = "gateway_commands"

# ---- Announce ----
RNS_ANNOUNCE_PREFIX = "agronomi-sensor"
HUB_ANNOUNCE_FILTER = "agronomi"

# ---- Hub LXMF address (discovered via announce) ----
SENSOR_HUB = ""
