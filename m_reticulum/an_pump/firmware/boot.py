"""µReticulum — ESP32-C6 Boot Script with OTA Update Support

Runs on every boot (including deep-sleep wakeups).  Checks for a
pending firmware update staged in /update/ and swaps files into place
before main.py is loaded.

If /update/.reboot_needed exists, all .py files from /update/ are
moved to / (overwriting old versions), the marker is deleted, and
the system resets to load the new firmware.

This file intentionally imports only gc, uos, and machine — no heavy
modules.  If the update swap fails partway through, the node boots
with whatever old files survived.
"""

import gc

import uos

gc.collect()

# ---------------------------------------------------------------------------
# Apply pending OTA update if one was staged
# ---------------------------------------------------------------------------

try:
    import updater

    updater.check_pending_update()
except Exception as e:
    # Never let update errors prevent booting with old firmware
    print("[boot] update check failed: " + str(e))

gc.collect()
