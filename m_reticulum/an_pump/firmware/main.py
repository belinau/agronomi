"""µReticulum — Pump Actuator Node Firmware (AN-PUMP-01)

MicroPython firmware for a pump relay actuator node that communicates
over µReticulum via LXMF (LoRa / WiFi / BLE).

Unlike sensor nodes that deep-sleep between readings, actuator nodes stay
awake continuously so they can receive commands at any time.  The main loop
is an asyncio event loop that keeps all interfaces alive and listening.

All telemetry and ACKs are sent via LXMF fields to the hub's lxmf.delivery
destination — no separate ACK or telemetry destinations needed.

Commands received via LXMF:
  - pump_on  → activate pump relay
  - pump_off → deactivate pump relay
"""

import gc
import time

import config
import machine
import uasyncio as asyncio
from sensors import read_all
from urns import Reticulum
from urns.destination import Destination
from urns.identity import Identity
from urns.lxmf import LXMessage, LXMRouter
from urns.packet import Packet

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_pump_on = False  # Current pump relay state
_cmd_counter = 0  # Auto-increment for command tracking

_hub_identity = None
_hub_lxmf_hash = None
_lxm_router = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg, level=1):
    if config.DEBUG >= level:
        print("[AN-PUMP] " + str(msg))


def _get_rns_interface_name(rns):
    for iface in rns.interfaces:
        if hasattr(iface, "online") and iface.online:
            return getattr(iface, "name", iface.__class__.__name__).lower()
    return "none"


def _connect_wifi(ssid, password, timeout=15):
    import network

    wlan = network.WLAN(network.STA_IF)
    wlan.active(False)
    time.sleep(0.1)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        start = time.ticks_ms()
        while not wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), start) // 1000 > timeout:
                wlan.active(False)
                raise RuntimeError("WiFi connection timed out")
            time.sleep(0.2)
    return wlan.ifconfig()[0]


def _find_or_create_identity(storage_path):
    Identity.storagepath = storage_path
    identity_path = storage_path + "/identity"
    try:
        ident = Identity.from_file(identity_path)
        if ident:
            _log("Loaded identity: " + ident.hexhash)
            return ident
    except Exception:
        pass
    ident = Identity()
    try:
        ident.to_file(identity_path)
        _log("Created new identity: " + ident.hexhash)
    except Exception as e:
        _log("Warning: could not persist identity: " + str(e))
    return ident


# ---------------------------------------------------------------------------
# Actuator control — pump relay
# ---------------------------------------------------------------------------

_pump_pin = machine.Pin(config.PIN_PUMP_RELAY, machine.Pin.OUT, value=0)


def pump_on():
    global _pump_on
    _pump_pin.value(1)
    _pump_on = True
    _log("Pump relay ON")


def pump_off():
    global _pump_on
    _pump_pin.value(0)
    _pump_on = False
    _log("Pump relay OFF")


# ---------------------------------------------------------------------------
# LXMF command handler
# ---------------------------------------------------------------------------


def _on_lxmf_delivery(message):
    global _cmd_counter
    try:
        fields = message.fields or {}
        cmd = fields.get("cmd", "")

        cmd_id = fields.get("cmd_id", _cmd_counter)
        _cmd_counter += 1
        _log("LXMF command: " + cmd + " (cmd_id=" + str(cmd_id) + ")", 1)

        error = None
        if cmd == "pump_on":
            pump_on()
        elif cmd == "pump_off":
            pump_off()
        else:
            error = "unknown_command: " + cmd
            _log("Unknown command: " + cmd, 1)

        # ACK via LXMF fields
        if _hub_lxmf_hash is not None:
            ack_fields = {
                "dev_id": config.NODE_NAME,
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "ok" if error is None else "error",
            }
            if error:
                ack_fields["error"] = error
            _lxm_router.send_message(_hub_lxmf_hash, content=b"", fields=ack_fields)
            _log("ACK sent for cmd_id=" + str(cmd_id))

    except Exception as e:
        _log("LXMF handler error: " + str(e), 1)


# ---------------------------------------------------------------------------
# Announce handler — discovers the hub and seeds destination hashes
# ---------------------------------------------------------------------------


def _on_announce(destination_hash, app_data, packet):
    global _hub_identity, _hub_lxmf_hash
    if app_data is None:
        return
    try:
        data_str = (
            app_data.decode("utf-8")
            if isinstance(app_data, (bytes, bytearray))
            else str(app_data)
        )
        _log("Announce from " + destination_hash.hex()[:8] + ": " + data_str, 2)
        if _hub_identity is None:
            ident = Identity.recall(destination_hash)
            if ident is not None:
                _hub_identity = ident
                _hub_lxmf_hash = Destination.hash(ident, "lxmf", "delivery")
                Identity.remember(None, _hub_lxmf_hash, ident.get_public_key())
                _log("Hub discovered: " + ident.hexhash)
    except Exception as e:
        _log("Announce handler error: " + str(e), 2)


# ---------------------------------------------------------------------------
# Telemetry builder — flat LXMF fields dict
# ---------------------------------------------------------------------------


def _build_telemetry_fields(interface_name):
    readings = read_all(config)
    fields = {
        "dev_id": config.NODE_NAME,
        "type": config.DEVICE_TYPE,
        "fw": config.FIRMWARE_VERSION,
        "pump_on": _pump_on,
        "bat": readings.get("battery_v", -1.0),
        "if": interface_name,
    }
    return fields


# ---------------------------------------------------------------------------
# Main entry point — async event loop (NO deep sleep)
# ---------------------------------------------------------------------------


def main():
    global _hub_identity, _hub_lxmf_hash, _lxm_router

    gc.collect()
    _log("=" * 40)
    _log("AN-PUMP-01 boot — µReticulum v" + config.FIRMWARE_VERSION)
    _log("=" * 40)

    # ------------------------------------------------------------------
    # 1. Connect WiFi if any WiFi interfaces are configured
    # ------------------------------------------------------------------
    wifi_interfaces = [
        i
        for i in config.CONFIG.get("interfaces", [])
        if i.get("enabled", True)
        and i.get("type", "") in ("UDPInterface", "TCPClientInterface")
    ]
    if wifi_interfaces and config.WIFI_SSID:
        try:
            ip = _connect_wifi(config.WIFI_SSID, config.WIFI_PASS)
            _log("WiFi connected — IP: " + ip)
        except Exception as e:
            _log("WiFi failed: " + str(e))

    # ------------------------------------------------------------------
    # 2. Initialise µReticulum
    # ------------------------------------------------------------------
    try:
        rns = Reticulum(loglevel={0: 0, 1: 0, 2: 2}.get(config.DEBUG, 0))
        rns.config = config.CONFIG
        storage = rns.storagepath
        ident = _find_or_create_identity(storage)
        rns.identity = ident
        rns.setup_interfaces()
        _log("µReticulum initialised — identity: " + ident.hexhash)
    except Exception as e:
        _log("FATAL: µReticulum init failed: " + str(e))
        time.sleep(10)
        machine.reset()
        return

    # ------------------------------------------------------------------
    # 3. Set up LXMRouter for LXMF receive + command destination
    # ------------------------------------------------------------------
    _lxm_router = LXMRouter(storagepath=storage)
    lxmf_dest = _lxm_router.register_delivery_identity(
        ident,
        display_name=config.NODE_NAME,
    )
    _lxm_router.register_delivery_callback(_on_lxmf_delivery)
    _log("LXMF delivery dest: " + lxmf_dest.hexhash)

    cmd_dest = Destination(
        ident,
        Destination.IN,
        Destination.SINGLE,
        config.COMMAND_APP,
        config.COMMAND_ASPECT,
    )
    cmd_dest.set_proof_strategy(Destination.PROVE_ALL)
    cmd_dest._announce_handler = _on_announce

    # ------------------------------------------------------------------
    # 4. Start poll loops + announce
    # ------------------------------------------------------------------
    from urns.transport import Transport

    poll_tasks = []
    for iface in rns.interfaces:
        if hasattr(iface, "poll_loop"):
            task = asyncio.create_task(iface.poll_loop())
            poll_tasks.append(task)
            _log("Started poll loop for " + str(iface))

    transport_task = asyncio.create_task(Transport.job_loop())
    poll_tasks.append(transport_task)
    _log("Started transport job loop")

    app_data = (config.RNS_ANNOUNCE_PREFIX + ":" + config.NODE_NAME).encode("utf-8")
    cmd_dest.announce(app_data=app_data)
    _log("Announced on " + config.COMMAND_APP + "." + config.COMMAND_ASPECT)

    # ------------------------------------------------------------------
    # 5. Run the async event loop (blocks forever)
    # ------------------------------------------------------------------
    async def periodic_announce():
        while True:
            await asyncio.sleep(config.ANNOUNCE_INTERVAL_SEC)
            try:
                cmd_dest.announce(app_data=app_data)
                _log(
                    "Re-announced on "
                    + config.COMMAND_APP
                    + "."
                    + config.COMMAND_ASPECT
                )
            except Exception as e:
                _log("Re-announce error: " + str(e), 1)

    async def periodic_telemetry():
        while True:
            await asyncio.sleep(config.TELEMETRY_INTERVAL_SEC)
            try:
                if _hub_lxmf_hash is not None:
                    iface_name = _get_rns_interface_name(rns)
                    telemetry_fields = _build_telemetry_fields(iface_name)
                    _lxm_router.send_message(
                        _hub_lxmf_hash, content=b"", fields=telemetry_fields
                    )
                    _log("Telemetry sent via LXMF fields")
                else:
                    _log("No hub — skipping telemetry", 1)
            except Exception as e:
                _log("Telemetry error: " + str(e), 1)

    async def keep_alive():
        while True:
            await asyncio.sleep(60)

    _log("Starting async event loop (actuator stays awake)")
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(periodic_announce())
        loop.create_task(periodic_telemetry())
        loop.create_task(keep_alive())
        loop.run_forever()
    except KeyboardInterrupt:
        _log("Shutdown requested")
        rns.shutdown()
    except Exception as e:
        _log("Event loop error: " + str(e), 1)
        time.sleep(10)
        machine.reset()


main()
