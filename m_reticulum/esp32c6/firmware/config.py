"""µReticulum — ESP32-C6 Template Node Configuration

Template configuration for a battery-powered sensor node using
µReticulum. Copy this folder and customise NODE_NAME, DEVICE_TYPE,
and sensor pins for your hardware.

Transport topology:
  PRIMARY: BLE → RNode (via RNodeBLEInterface)
  SECONDARY: WiFi UDP (greenhouse/indoor with WiFi coverage)
"""

# ---- Node identity ----
NODE_NAME = "TEMPLATE-01"  # Unique per device
DEVICE_TYPE = "template_node"
FIRMWARE_VERSION = "2.0.0-mr"  # -mr = microreticulum

# ---- WiFi (leave blank for BLE-only field deployment) ----
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
ENABLE_DEEPSLEEP = True
SLEEP_INTERVAL_SEC = 300  # 5 minutes

# ---- Battery ADC ----
BAT_ADC_PIN = 1  # GPIO1 (100k/100k divider)
BAT_DIVIDER_RATIO = 2.0  # V_bat / V_adc for equal resistors

# ---- Logging: 0=silent 1=info 2=debug ----
DEBUG = 1

# ---- RNS interfaces ----
CONFIG = {
    "loglevel": 2,
    "enable_transport": False,  # Sensor nodes don't relay
    "interfaces": [
        # --- RNode BLE (PRIMARY — connects to RNode over BLE) ---
        {
            "type": "RNodeBLEInterface",
            "name": "RNode BLE",
            "target_name": "",  # Auto-discover by NUS UUID
            "pairing_passkey": 0,
            "serial_port": "",  # Set to RNode USB serial for auto-pairing
            "frequency": 868000000,
            "bandwidth": 125000,
            "spreadingfactor": 11,
            "codingrate": 5,
            "txpower": 17,
            "enabled": True,
        },
        # --- WiFi UDP (secondary — greenhouse/indoor only) ---
        {
            "type": "UDPInterface",
            "name": "WiFi UDP",
            "listen_port": 4242,
            "forward_port": 4242,
            "enabled": True,
        },
    ],
}

# ---- Hub discovery aspects ----
COMMAND_APP = "farm"
COMMAND_ASPECT = "gateway_commands"

# ---- Announce ----
RNS_ANNOUNCE_PREFIX = "agronomi-sensor"

# ---- Hub LXMF address (discovered via announce) ----
SENSOR_HUB = ""
