"""µReticulum — Firmware Over-The-Air Update Receiver

Handles receiving firmware files over LXMF and staging them for
application on the next boot.  Designed for ESP32-C6 MicroPython
with constrained memory and flash.

Integration:
  In your main.py _on_lxmf_delivery handler, add:

      import updater
      if cmd in ("update_file", "update_commit"):
          resp = updater.handle_update(fields)
          _lxm_router.send_message(_hub_lxmf_hash, content=b"", fields=resp)
          return

Lifecycle:
  1. Hub sends one or more update_file LXMF messages (one per file).
     Each is verified (SHA-256) and written to /update/<filename>.
  2. Hub sends update_commit — this writes /update/.reboot_needed.
  3. On next boot (including deep-sleep wake), boot.py calls
     updater.check_pending_update() which swaps staged files into
     place and resets.
"""

import gc

import uhashlib
import uos

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UPDATE_DIR = "/update"
_REBOOT_MARKER = _UPDATE_DIR + "/.reboot_needed"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_update_dir():
    """Create /update/ if it doesn't already exist."""
    try:
        uos.stat(_UPDATE_DIR)
    except OSError:
        uos.mkdir(_UPDATE_DIR)


def _sha256(data):
    """Return hex-encoded SHA-256 digest of *data* (bytes)."""
    h = uhashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _write_update_file(filename, data):
    """Write *data* bytes to /update/<filename>, creating the directory if needed.

    Returns True on success, False on failure.
    """
    _ensure_update_dir()
    path = _UPDATE_DIR + "/" + filename
    try:
        # Ensure parent dirs under /update exist (e.g. /update/lib/foo.py)
        # MicroPython uos.mkdir doesn't support exist_ok, so walk each part.
        parts = filename.split("/")
        if len(parts) > 1:
            partial = _UPDATE_DIR
            for part in parts[:-1]:
                partial = partial + "/" + part
                try:
                    uos.stat(partial)
                except OSError:
                    uos.mkdir(partial)
        with open(path, "wb") as f:
            f.write(data)
            # Flush to flash — critical on ESP32 to survive power loss
            try:
                uos.sync(f)
            except (AttributeError, OSError):
                pass  # uos.sync not available on all ports
        return True
    except Exception as e:
        print("[updater] write error " + path + ": " + str(e))
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def handle_update(fields):
    """Process an LXMF update command and return a response dict.

    Expected fields:
      cmd      — "update_file" or "update_commit"
      cmd_id   — int identifier (echoed in response)
      filename — target filename (update_file only)
      data     — file content as bytes (update_file only)
      sha256   — hex SHA-256 of data (update_file only)

    Returns a dict suitable for use as LXMF fields in an ACK.
    """
    cmd = fields.get("cmd", "")
    cmd_id = fields.get("cmd_id", 0)

    if cmd == "update_file":
        filename = fields.get("filename", "")
        data = fields.get("data")
        expected_hash = fields.get("sha256", "")

        if not filename:
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "missing_filename",
            }

        if data is None:
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "missing_data",
            }

        if not expected_hash:
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "missing_hash",
            }

        # Verify SHA-256 before writing anything
        actual_hash = _sha256(data)
        if actual_hash != expected_hash:
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "hash_mismatch",
            }

        if _write_update_file(filename, data):
            # Free the data buffer immediately — memory is scarce
            gc.collect()
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "ok",
                "filename": filename,
            }
        else:
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "write_failed",
            }

    elif cmd == "update_commit":
        # Signal that all files have been transferred and the node
        # should apply them on next boot.
        _ensure_update_dir()
        try:
            with open(_REBOOT_MARKER, "w") as f:
                f.write("1")
                try:
                    uos.sync(f)
                except (AttributeError, OSError):
                    pass
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "ok",
            }
        except Exception as e:
            print("[updater] commit marker error: " + str(e))
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "commit_failed",
            }

    else:
        return {
            "ack": True,
            "cmd_id": cmd_id,
            "cmd": cmd,
            "status": "error",
            "error": "unknown_command",
        }


def check_pending_update():
    """Check if a firmware update is pending and apply it.

    Called from boot.py on every boot (including deep-sleep wakeups).
    If /update/.reboot_needed exists:
      1. Move all .py files from /update/ to / (overwriting old versions).
      2. Delete the marker file.
      3. Call machine.reset() to load the new firmware.

    If any individual file move fails, the error is logged but boot
    continues with whatever old files are still in place.  Partial
    updates are possible but unlikely — the hub should send all files
    before the commit command.
    """
    try:
        uos.stat(_REBOOT_MARKER)
    except OSError:
        # No pending update — normal boot
        return

    import machine

    print("[updater] Pending update found — applying staged files")

    moved = 0
    failed = 0

    try:
        entries = uos.listdir(_UPDATE_DIR)
    except OSError:
        # /update dir missing — nothing to do, clean up marker won't exist either
        return

    for entry in entries:
        # Skip the marker and any non-Python files
        if entry == ".reboot_needed":
            continue
        if not entry.endswith(".py"):
            continue

        src = _UPDATE_DIR + "/" + entry
        dst = "/" + entry

        try:
            uos.rename(src, dst)
            moved += 1
            print("[updater] " + entry + " -> /" + entry)
        except Exception as e:
            failed += 1
            print("[updater] FAILED to move " + entry + ": " + str(e))

    # Remove the marker regardless of move failures
    try:
        uos.remove(_REBOOT_MARKER)
    except OSError:
        pass

    if moved > 0:
        print(
            "[updater] Update applied: "
            + str(moved)
            + " files moved, "
            + str(failed)
            + " failed"
        )
        # Reset to load the new firmware — boot.py will run again but
        # the marker is gone, so it will proceed to main.py normally.
        machine.reset()
    else:
        print("[updater] No files moved — continuing with old firmware")
