"""
reticulum_ingest.py — Shared-Instance Compatible LXMF Telemetry Ingestion Engine
"""

import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime

import LXMF
import RNS

# ---------------------------------------------------------------------------
# CONFIGURATION CONSTANTS
# ---------------------------------------------------------------------------
LOG_FILE = os.environ.get("AGRONOMI_LOG", os.path.expanduser("~/agronomi.log"))
DB_PATH = os.environ.get("DB_PATH", "./farm_data.db")
IDENTITY_PATH = "./farm_hub.identity"


def setup_hub_logging():
    log_fh = open(LOG_FILE, "a")

    class TeeStream:
        def __init__(self, original, file_link):
            self._original = original
            self._file_link = file_link

        def write(self, data):
            self._original.write(data)
            if data and data.strip():
                self._file_link.write(data if data.endswith("\n") else data + "\n")
                self._file_link.flush()

        def flush(self):
            self._original.flush()
            self._file_link.flush()

        def fileno(self):
            return self._original.fileno()

    sys.stdout = TeeStream(sys.stdout, log_fh)
    sys.stderr = TeeStream(sys.stderr, log_fh)
    print(f"[HUB] === Unified Logging Engine Live at: {LOG_FILE} ===")


# ---------------------------------------------------------------------------
# SQLITE STORAGE ENGINE SCHEMA
# ---------------------------------------------------------------------------
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sensor_nodes (
    node_id         TEXT PRIMARY KEY,
    name            TEXT,
    location        TEXT,
    last_seen       TEXT,
    battery_level   REAL
);
CREATE TABLE IF NOT EXISTS sensor_readings (
    reading_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      TEXT NOT NULL REFERENCES sensor_nodes(node_id),
    reading_type TEXT NOT NULL,
    value        REAL NOT NULL,
    unit         TEXT DEFAULT '',
    recorded_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hardware_devices (
    device_id           TEXT PRIMARY KEY,
    device_type         TEXT,
    node_id             TEXT REFERENCES sensor_nodes(node_id),
    rns_identity_hash   TEXT,
    rns_destination_hash TEXT,
    rns_interface       TEXT DEFAULT 'wifi',
    firmware_version    TEXT,
    status              TEXT DEFAULT 'active',
    last_seen           TEXT
);
CREATE TABLE IF NOT EXISTS actuator_commands (
    cmd_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL REFERENCES hardware_devices(device_id),
    cmd_type        TEXT,
    cmd_value       REAL,
    cmd_value_text  TEXT,
    requested_at    TEXT NOT NULL DEFAULT (datetime('now')),
    executed_at     TEXT,
    status          TEXT DEFAULT 'pending'
);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_DDL)
    return conn


# ---------------------------------------------------------------------------
# COMMAND DISPATCHER (LXMF COMPATIBLE)
# ---------------------------------------------------------------------------
class OutboundCommandDispatcher:
    def __init__(self, lxm_router):
        self.lxm_router = lxm_router
        self.running = True

    def poll_loop(self):
        RNS.log("[COMMAND ENGINE] Outbound queue scheduler loop active.")
        while self.running:
            try:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT c.cmd_id, c.device_id, c.cmd_type, h.rns_destination_hash
                    FROM actuator_commands c
                    JOIN hardware_devices h ON c.device_id = h.device_id
                    WHERE c.status = 'pending'
                    LIMIT 5
                """)
                rows = cursor.fetchall()

                for row in rows:
                    cmd_id = row["cmd_id"]
                    dev_id = row["device_id"]
                    cmd_type = row["cmd_type"]
                    dest_hex = row["rns_destination_hash"]

                    if not dest_hex or dest_hex == "unknown":
                        RNS.log(
                            f"[COMMAND REJECT] Missing path to {dev_id}.",
                            RNS.LOG_WARNING,
                        )
                        continue

                    RNS.log(
                        f"[COMMAND ROUTE] Relaying LXM command request '{cmd_type}' targeting <{dest_hex}>"
                    )
                    clean_hex = dest_hex.replace("<", "").replace(">", "").strip()
                    dest_bytes = RNS.hex2bytes(clean_hex)

                    command_payload = json.dumps({"cmd": cmd_type}).encode("utf-8")
                    outbound_lxm = LXMF.LXMessage(
                        dest=dest_bytes,
                        source=self.lxm_router.address,
                        content=command_payload,
                        title="Actuator System Directive",
                    )

                    # VERIFIED API CALL
                    self.lxm_router.handle_outbound(outbound_lxm)

                    conn.execute(
                        "UPDATE actuator_commands SET status = 'sent', executed_at = ? WHERE cmd_id = ?",
                        (datetime.now().isoformat(), cmd_id),
                    )
                    conn.commit()
                conn.close()
            except Exception as e:
                RNS.log(
                    f"[CRITICAL LOOP FAULT] Command subsystem error: {e}", RNS.LOG_ERROR
                )
            time.sleep(2)


# ---------------------------------------------------------------------------
# CENTRAL CORE ARCHITECTURE
# ---------------------------------------------------------------------------
class FarmLXMFHub:
    def __init__(self):
        self.reticulum = RNS.Reticulum()
        self.identity = self._load_or_create_identity()

        # VERIFIED API INITIALIZATION
        self.lxm_router = LXMF.LXMRouter(
            identity=self.identity, storagepath="./lxmf_storage"
        )

        # VERIFIED API METHOD SIGNATURE
        self.lxm_router.register_delivery_callback(self._on_lxm_received)

        self.hub_addr_hex = RNS.prettyhexrep(self.identity.hash)
        display_string = f"<Hub> {self.hub_addr_hex}"

        # VERIFIED API METHOD SIGNATURE
        self.lxmf_local_target = self.lxm_router.register_delivery_identity(
            self.identity, display_name=display_string
        )

        RNS.log(
            f"[CORE INIT] Unified LXMF Ingestion Target Ready: <{self.hub_addr_hex}>"
        )
        self.lxm_router.announce(self.lxmf_local_target.hash)

    def _load_or_create_identity(self) -> RNS.Identity:
        if os.path.exists(IDENTITY_PATH):
            try:
                ident = RNS.Identity.from_file(IDENTITY_PATH)
                if ident:
                    RNS.log(
                        f"[CORE] Loaded root system identity: {RNS.prettyhexrep(ident.hash)}"
                    )
                    return ident
            except Exception as e:
                RNS.log(
                    f"[WARN] Cryptographic read structural failure: {e}",
                    RNS.LOG_WARNING,
                )
        ident = RNS.Identity()
        ident.to_file(IDENTITY_PATH)
        RNS.log(
            f"[CORE] Minted new system identity hash: {RNS.prettyhexrep(ident.hash)}"
        )
        return ident

    def _on_lxm_received(self, lxm_message):
        """Callback executed by LXMRouter when a verified LXM message is delivered."""
        try:
            content_bytes = lxm_message.content
            raw_payload = content_bytes.decode("utf-8")

            if "{" in raw_payload:
                raw_payload = raw_payload[
                    raw_payload.find("{") : raw_payload.rfind("}") + 1
                ]

            data = json.loads(raw_payload)

            if "dev_id" in data and "bat_v" in data:
                src_hex = RNS.prettyhexrep(lxm_message.source)
                RNS.log(
                    f"[TELEMETRY INGEST] Decoded LXM frame from device: {data['dev_id']} | Source: <{src_hex}>"
                )
                self._write_telemetry_to_db(data, src_hex)
        except Exception as e:
            RNS.log(
                f"[DROP] Failed parsing incoming LXM frame payload: {e}", RNS.LOG_ERROR
            )

    def _write_telemetry_to_db(self, data, source_hex: str):
        now_str = datetime.now().isoformat()
        node_id = data["dev_id"]
        try:
            conn = get_db()
            with conn:
                conn.execute(
                    """
                    INSERT INTO sensor_nodes (node_id, name, last_seen, battery_level)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        last_seen = excluded.last_seen,
                        battery_level = excluded.battery_level
                """,
                    (node_id, node_id, now_str, data["bat_v"]),
                )

                conn.execute(
                    """
                    INSERT INTO hardware_devices (device_id, device_type, node_id, rns_identity_hash, rns_destination_hash, rns_interface, firmware_version, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id) DO UPDATE SET
                        rns_destination_hash = excluded.rns_destination_hash,
                        rns_interface = excluded.rns_interface,
                        last_seen = excluded.last_seen
                """,
                    (
                        node_id,
                        data.get("device_type", "support_node"),
                        node_id,
                        source_hex,
                        data.get("rns_interface", "wifi"),
                        data.get("fw_ver"),
                        now_str,
                    ),
                )

                conn.execute(
                    """
                    INSERT INTO sensor_readings (node_id, reading_type, value, unit, recorded_at)
                    VALUES (?, 'battery_voltage', ?, 'V', ?)
                """,
                    (node_id, data["bat_v"], now_str),
                )
            RNS.log(
                f"[DB Sync] Successfully synchronized telemetry entries for {node_id}"
            )
        except Exception as e:
            RNS.log(
                f"[DB ERROR] Ingestion relational tracking failure: {e}", RNS.LOG_ERROR
            )


if __name__ == "__main__":
    setup_hub_logging()
    hub_app = FarmLXMFHub()

    dispatcher = OutboundCommandDispatcher(hub_app.lxm_router)
    dispatch_thread = threading.Thread(target=dispatcher.poll_loop, daemon=True)
    dispatch_thread.start()

    try:
        while True:
            time.sleep(30)
            hub_app.lxm_router.announce(hub_app.lxmf_local_target.hash)
            RNS.log(
                "[RNS Shared Daemon] Dispatched standard LXMF target identity announce."
            )
    except KeyboardInterrupt:
        RNS.log("System shutdown operation called. Closing active processing slots...")
        dispatcher.running = False
        sys.exit(0)
