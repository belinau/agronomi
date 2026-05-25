#!/usr/bin/env python3
"""firmware_push.py — Push firmware updates to ESP32 nodes over RNS.

This script runs on the hub (Mac Mini) and sends firmware files to
ESP32 nodes over the Reticulum mesh using LXMF Resource transfers.

The update flow:
  1. For each file (main.py, config.py, sensors.py, boot.py):
     - Read the file from the local git repo
     - Compute SHA-256 hash
     - Send an LXMF message with cmd="update_file" containing the
       file data, filename, hash, and version
     - Wait for ACK from the node
  2. After all files are sent:
     - Send cmd="update_commit" which tells the node to reboot
  3. On next boot, boot.py calls updater.check_pending_update()
     which moves the staged files from /update/ to / and reboots

Usage:
  python3 firmware_push.py sn_air                    # push latest to sn_air
  python3 firmware_push.py sn_air --version 2.1.0-mr # push specific version
  python3 firmware_push.py an_pump --no-reboot       # push files, don't commit
  python3 firmware_push.py an_pump --dry-run         # show what would be sent

The script uses the hub's existing RNS identity and LXMF router from
reticulum_ingest.py. It discovers the node's LXMF destination hash
from the hardware_devices table in the SQLite database.
"""

import argparse
import hashlib
import os
import sys
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Files that get pushed to every node (in order)
FIRMWARE_FILES = ["main.py", "config.py", "sensors.py", "boot.py"]

# Also push updater.py if it's different from what's on the device
UPDATER_FILE = "updater.py"

# Device type to directory mapping
DEVICE_DIRS = {
    "sn_air": "sn_air",
    "sn_soil": "sn_soil",
    "sn_support": "sn_support",
    "an_pump": "an_pump",
    "an_greenhouse": "an_greenhouse",
}

# Base directory for firmware files (relative to this script)
FIRMWARE_BASE = os.path.join(os.path.dirname(__file__), "..")

# Default database path (same as reticulum_ingest.py)
DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "../../documents/farm_data.db")
)

# Timeout for ACK from node (seconds)
ACK_TIMEOUT = 120

# Delay between file sends (seconds) — give the node time to write to flash
INTER_FILE_DELAY = 3


def sha256_hex(data):
    """Compute SHA-256 hex digest of bytes data."""
    return hashlib.sha256(data).hexdigest()


def read_firmware_file(device, filename):
    """Read a firmware file from the git repo.

    Returns (data_bytes, sha256_hex) or raises FileNotFoundError.
    """
    device_dir = DEVICE_DIRS.get(device)
    if not device_dir:
        raise ValueError(f"Unknown device: {device}")

    # Check device-specific directory first, then esp32c6 template
    paths = [
        os.path.join(FIRMWARE_BASE, device_dir, "firmware", filename),
    ]

    # updater.py comes from the esp32c6 template (shared by all nodes)
    if filename == UPDATER_FILE:
        paths = [
            os.path.join(FIRMWARE_BASE, "esp32c6", "firmware", filename),
        ]

    for path in paths:
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            return data, sha256_hex(data)

    raise FileNotFoundError(f"Firmware file not found: {filename} for {device}")


def get_node_destination(device, db_path):
    """Look up the node's RNS destination hash from the database.

    Returns the hex destination hash string, or None if not found.
    """
    import sqlite3

    # Map device type to NODE_NAME prefix
    name_map = {
        "sn_air": "SN-AIR",
        "sn_soil": "SN-SOIL",
        "sn_support": "GW-SUPPORT",
        "an_pump": "AN-PUMP",
        "an_greenhouse": "AN-GREENHOUSE",
    }

    prefix = name_map.get(device, "")
    if not prefix:
        return None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Find the most recently seen device matching the prefix
    cursor.execute(
        """
        SELECT rns_destination_hash, node_id, last_seen
        FROM hardware_devices
        WHERE node_id LIKE ? || '%'
        ORDER BY last_seen DESC
        LIMIT 1
    """,
        (prefix,),
    )

    row = cursor.fetchone()
    conn.close()

    if row and row["rns_destination_hash"] and row["rns_destination_hash"] != "unknown":
        return row["rns_destination_hash"]

    return None


def send_update(lxm_router, dest_hash_hex, fields):
    """Send an LXMF message with the given fields to a node.

    Returns the LXMessage object.
    """
    import LXMF
    import RNS

    dest_bytes = RNS.hex2bytes(dest_hash_hex.replace("<", "").replace(">", "").strip())

    recipient_identity = RNS.Identity.recall(dest_bytes)
    if not recipient_identity:
        print(f"  [push] Cannot recall identity for {dest_hash_hex[:16]}...")
        # Try to remember from known destinations
        for dh, idata in RNS.Identity.known_destinations.items():
            if dh.hex() == dest_bytes.hex() or dh == dest_bytes:
                recipient_identity = RNS.Identity.recall(dh)
                break

    if not recipient_identity:
        print(f"  [push] Identity not found for {dest_hash_hex[:16]}...")
        return None

    dest = RNS.Destination(
        recipient_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "lxmf",
        "delivery",
    )

    msg = LXMF.LXMessage(
        destination=dest,
        source=lxm_router.delivery_destination,
        content=b"",
        fields=fields,
        desired_method=LXMF.LXMessage.DIRECT,
    )

    msg.pack()
    lxm_router.handle_outbound(msg)
    return msg


def push_firmware(device, version=None, no_reboot=False, dry_run=False):
    """Push firmware files to a node over the mesh.

    Args:
        device: Device type (e.g. 'sn_air')
        version: Version string to tag the update
        no_reboot: If True, don't send the commit/reboot command
        dry_run: If True, just show what would be sent
    """
    import sqlite3

    # Read version from config.py if not specified
    if not version:
        config_path = os.path.join(
            FIRMWARE_BASE, DEVICE_DIRS[device], "firmware", "config.py"
        )
        with open(config_path, "r") as f:
            for line in f:
                if line.strip().startswith("FIRMWARE_VERSION"):
                    version = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if not version:
            version = "unknown"

    print(f"[push] AgroNomi firmware push")
    print(f"[push]   Device:  {device}")
    print(f"[push]   Version: {version}")
    print(f"")

    # Collect files to send
    files_to_send = list(FIRMWARE_FILES) + [UPDATER_FILE]

    print(f"[push] Files to send:")
    total_size = 0
    for filename in files_to_send:
        try:
            data, file_hash = read_firmware_file(device, filename)
            total_size += len(data)
            print(f"[push]   {filename}: {len(data)} bytes, sha256={file_hash[:16]}...")
        except FileNotFoundError as e:
            print(f"[push]   {filename}: NOT FOUND - {e}")
            return False

    print(f"[push] Total payload: {total_size} bytes")
    print(f"")

    if dry_run:
        print("[push] DRY RUN — no files sent. Use without --dry-run to push.")
        return True

    # Look up the node's destination hash
    dest_hash = get_node_destination(device, DB_PATH)
    if not dest_hash:
        print(f"[push] ERROR: Node {device} not found in database.")
        print(f"[push] Has it announced itself on the mesh?")
        return False

    print(f"[push] Target destination: {dest_hash[:16]}...")

    # Initialize RNS and LXMF
    import LXMF
    import RNS

    print("[push] Initializing RNS...")
    reticulum = RNS.Reticulum(loglevel=RNS.LOG_NOTICE)

    # Load or create identity
    identity_path = os.path.join(
        os.path.dirname(__file__), "../../documents/farm_hub.identity"
    )
    if os.path.exists(identity_path):
        identity = RNS.Identity.from_file(identity_path)
    else:
        identity = RNS.Identity()
        identity.to_file(identity_path)

    lxm_router = LXMF.LXMRouter(identity=identity)
    lxm_router.register_delivery_identity(identity, display_name="AgroNomi Hub Push")

    print("[push] Waiting for RNS path to node...")
    # Wait for path to destination
    dest_bytes = RNS.hex2bytes(dest_hash.replace("<", "").replace(">", "").strip())
    if not RNS.Transport.has_path(dest_bytes):
        RNS.Transport.request_path(dest_bytes)
        timeout = 30
        while not RNS.Transport.has_path(dest_bytes) and timeout > 0:
            time.sleep(1)
            timeout -= 1
        if not RNS.Transport.has_path(dest_bytes):
            print("[push] ERROR: Could not find path to node. Is it online?")
            return False

    print("[push] Path found. Sending files...")

    # Send each file
    cmd_id = 0
    for filename in files_to_send:
        data, file_hash = read_firmware_file(device, filename)
        cmd_id += 1

        print(f"[push] Sending {filename} ({len(data)} bytes, cmd_id={cmd_id})...")

        fields = {
            "cmd": "update_file",
            "cmd_id": cmd_id,
            "filename": filename,
            "data": data,
            "sha256": file_hash,
            "version": version,
            "dev_id": device.upper().replace("_", "-") + "-01",
        }

        msg = send_update(lxm_router, dest_hash, fields)
        if msg is None:
            print(f"[push] ERROR: Failed to send {filename}")
            return False

        print(f"[push]   Sent. Waiting {INTER_FILE_DELAY}s before next file...")
        time.sleep(INTER_FILE_DELAY)

    # Send commit command
    if not no_reboot:
        cmd_id += 1
        print(f"[push] Sending update_commit (reboot command, cmd_id={cmd_id})...")

        commit_fields = {
            "cmd": "update_commit",
            "cmd_id": cmd_id,
            "version": version,
            "dev_id": device.upper().replace("_", "-") + "-01",
        }

        msg = send_update(lxm_router, dest_hash, commit_fields)
        if msg is None:
            print(f"[push] ERROR: Failed to send commit command")
            return False

        print(f"[push] Commit sent. Node will reboot and apply update.")
    else:
        print(
            f"[push] Files sent without commit. Node will apply update on next reboot."
        )

    print(f"[push] Done. {device} firmware {version} pushed successfully.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Push firmware updates to ESP32 nodes over RNS"
    )
    parser.add_argument(
        "device",
        choices=list(DEVICE_DIRS.keys()),
        help="Device type to update",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Version string (auto-detected from config.py if not specified)",
    )
    parser.add_argument(
        "--no-reboot",
        action="store_true",
        help="Send files but don't trigger reboot (apply on next manual reboot)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be sent without actually sending",
    )
    parser.add_argument(
        "--db",
        default=DB_PATH,
        help="Path to farm_data.db (default: documents/farm_data.db)",
    )

    args = parser.parse_args()

    global DB_PATH
    DB_PATH = args.db

    success = push_firmware(
        device=args.device,
        version=args.version,
        no_reboot=args.no_reboot,
        dry_run=args.dry_run,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
