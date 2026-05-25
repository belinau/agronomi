#!/usr/bin/env bash
# deploy.sh — Fetch verified firmware from rngit and push to devices via mpremote
#
# Usage:
#   ./deploy.sh sn_air              # deploy latest sn_air firmware + secrets
#   ./deploy.sh sn_air 2.1.0-mr     # deploy specific version
#   ./deploy.sh an_pump --flash     # deploy and reboot device
#   ./deploy.sh sn_air --dry-run    # show what would be deployed, don't copy
#   ./deploy.sh sn_air --verify     # only verify the release, don't deploy
#   ./deploy.sh sn_air --force-secrets  # overwrite secrets.py on device
#
# Prerequisites:
#   - rngit configured and hub identity available
#   - mpremote installed (pip install mpremote)
#   - ESP32 connected via USB
#
# The firmware files per device are: main.py, config.py, sensors.py, boot.py
# Plus secrets.py (from local secure storage, NOT from rngit).
# All go to the ROOT of the device filesystem.
# secrets.py is preserved on the device if it already exists, unless --force-secrets.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# rngit remote — change this to your hub's destination hash
RNGIT_REMOTE="${RNGIT_REMOTE:-rns://REPLACE_WITH_YOUR_HUB_DEST_HASH/agronomi}"

# Firmware files that get deployed per device (from rngit release)
FIRMWARE_FILES="main.py config.py sensors.py boot.py"

# Per-device file — deployed from local secure storage, NOT from rngit
SECRETS_FILE="secrets.py"

# Files that must NEVER appear in an rngit release (device-specific, gitignored)
PROTECTED_FILES="secrets.py ble_pin.txt ble_mac.txt bonded.txt force_pair.txt"

# Local directory for per-device secrets (not in git)
# Structure: secrets/sn_air/secrets.py, secrets/an_pump/secrets.py, etc.
SECRETS_DIR="${SECRETS_DIR:-$(dirname "$0")/../secrets}"

# Local cache directory for downloaded releases
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/agronomi-firmware"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[deploy]${NC} $*"; }
ok()    { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()  { echo -e "${YELLOW}[deploy]${NC} $*"; }
error() { echo -e "${RED}[deploy]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
DEVICE=""
VERSION="latest"
FLASH=false
DRY_RUN=false
VERIFY_ONLY=false
FORCE_SECRETS=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --flash)          FLASH=true; shift ;;
        --dry-run)        DRY_RUN=true; shift ;;
        --verify)         VERIFY_ONLY=true; shift ;;
        --force-secrets)  FORCE_SECRETS=true; shift ;;
        --remote)         RNGIT_REMOTE="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,12p' "$0"
            exit 0
            ;;
        -*)
            error "Unknown option: $1"
            ;;
        *)
            if [[ -z "$DEVICE" ]]; then
                DEVICE="$1"
            elif [[ "$VERSION" == "latest" ]]; then
                VERSION="$1"
            else
                error "Too many arguments: $1"
            fi
            shift
            ;;
    esac
done

[[ -z "$DEVICE" ]] && error "Specify a device type. Available: sn_air, sn_soil, sn_support, an_pump, an_greenhouse"

VALID_DEVICES="sn_air sn_soil sn_support an_pump an_greenhouse"
echo "$VALID_DEVICES" | grep -qw "$DEVICE" || error "Unknown device: $DEVICE. Must be one of: $VALID_DEVICES"

# ---------------------------------------------------------------------------
# Step 1: Fetch firmware from rngit
# ---------------------------------------------------------------------------
RELEASE_DIR="${CACHE_DIR}/${DEVICE}/${VERSION}"
mkdir -p "$RELEASE_DIR"

info "Fetching ${DEVICE} firmware (version: ${VERSION}) from rngit..."

if [[ "$VERSION" == "latest" ]]; then
    FETCH_TARGET="latest:${DEVICE}"
else
    FETCH_TARGET="${VERSION}:${DEVICE}"
fi

# rngit release fetch downloads artifacts and verifies the signed manifest
if ! rngit release "${RNGIT_REMOTE}" fetch "${FETCH_TARGET}"; then
    error "rngit fetch failed. Check your RNS connection and hub destination hash."
fi

# rngit puts downloaded files in CWD — move them to cache
for f in $FIRMWARE_FILES; do
    [[ -f "$f" ]] && mv "$f" "${RELEASE_DIR}/"
done
for manifest in *.rsm; do
    [[ -f "$manifest" ]] && mv "$manifest" "${RELEASE_DIR}/"
done

# ---------------------------------------------------------------------------
# Step 2: Verify we have all required files
# ---------------------------------------------------------------------------
info "Verifying firmware files in ${RELEASE_DIR}..."

MISSING=0
for f in $FIRMWARE_FILES; do
    if [[ ! -f "${RELEASE_DIR}/${f}" ]]; then
        warn "Missing: ${f}"
        MISSING=$((MISSING + 1))
    fi
done

if [[ $MISSING -gt 0 ]]; then
    error "Missing ${MISSING} firmware files. The release may not include ${DEVICE} artifacts."
fi

# Verify signed manifest
MANIFEST_FILE=""
for f in "${RELEASE_DIR}"/*.rsm; do
    [[ -f "$f" ]] && MANIFEST_FILE="$f" && break
done

if [[ -n "$MANIFEST_FILE" ]]; then
    ok "Found signed manifest: $(basename "$MANIFEST_FILE")"
    rngit release "$MANIFEST_FILE" fetch --offline
else
    warn "No signed manifest found. Cannot verify firmware integrity."
    warn "Proceed with caution — files downloaded but not cryptographically verified."
fi

if $VERIFY_ONLY; then
    ok "Verification complete. Exiting (--verify mode)."
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 3: Safety check — no protected files in the release
# ---------------------------------------------------------------------------
for f in $PROTECTED_FILES; do
    if [[ -f "${RELEASE_DIR}/${f}" ]]; then
        error "RELEASE CONTAINS PROTECTED FILE: ${f}. This must NEVER be in an rngit release. Aborting."
    fi
done

# ---------------------------------------------------------------------------
# Step 4: Dry-run output (before we touch the device)
# ---------------------------------------------------------------------------
if $DRY_RUN; then
    info "DRY RUN — would deploy these files to device root:"
    for f in $FIRMWARE_FILES; do
        echo "  ${RELEASE_DIR}/${f} → :/${f}"
    done
    SECRETS_SRC="${SECRETS_DIR}/${DEVICE}/${SECRETS_FILE}"
    if [[ -f "$SECRETS_SRC" ]]; then
        if $FORCE_SECRETS; then
            echo "  ${SECRETS_SRC} → :/${SECRETS_FILE} (overwriting existing)"
        else
            echo "  ${SECRETS_SRC} → :/${SECRETS_FILE} (only if missing on device)"
        fi
    else
        echo "  (${SECRETS_FILE}: no local template at ${SECRETS_SRC}, preserving device version)"
    fi
    if $FLASH; then
        echo "  Then: soft reboot device"
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 5: Check mpremote and device
# ---------------------------------------------------------------------------
if ! command -v mpremote &>/dev/null; then
    error "mpremote not found. Install with: pip install mpremote"
fi

DEVICE_INFO=$(mpremote run "import sys; print(sys.implementation.name + ' ' + '.'.join(str(x) for x in sys.implementation.version))" 2>/dev/null) || \
    error "No MicroPython device detected on USB. Connect the device and try again."
info "Connected device: ${DEVICE_INFO}"

# Verify the device matches what we're deploying
ON_DEVICE_NAME=$(mpremote run "import config; print(config.NODE_NAME)" 2>/dev/null) || true
if [[ -n "$ON_DEVICE_NAME" ]]; then
    case "$DEVICE" in
        sn_air)       EXPECTED_PREFIX="SN-AIR" ;;
        sn_soil)       EXPECTED_PREFIX="SN-SOIL" ;;
        sn_support)   EXPECTED_PREFIX="GW-SUPPORT" ;;
        an_pump)       EXPECTED_PREFIX="AN-PUMP" ;;
        an_greenhouse) EXPECTED_PREFIX="AN-GREENHOUSE" ;;
    esac
    if [[ "$ON_DEVICE_NAME" != "${EXPECTED_PREFIX}"* ]]; then
        warn "Device reports NODE_NAME='${ON_DEVICE_NAME}' but you're deploying ${DEVICE} (expected ${EXPECTED_PREFIX}*)"
        warn "Press Ctrl+C to abort, or wait 5s to continue..."
        sleep 5
    else
        ok "Device identity confirmed: ${ON_DEVICE_NAME}"
    fi
fi

# ---------------------------------------------------------------------------
# Step 6: Deploy secrets.py (from local secure storage, NOT from rngit)
# ---------------------------------------------------------------------------
SECRETS_SRC="${SECRETS_DIR}/${DEVICE}/${SECRETS_FILE}"

if [[ -f "$SECRETS_SRC" ]]; then
    # Check if device already has secrets.py
    EXISTING=$(mpremote run "
try:
    import os; os.stat('${SECRETS_FILE}'); print('exists')
except:
    print('missing')
" 2>/dev/null) || EXISTING="unknown"

    if [[ "$EXISTING" == "exists" ]] && ! $FORCE_SECRETS; then
        ok "${SECRETS_FILE} already on device — preserving existing. Use --force-secrets to overwrite."
    else
        info "Deploying ${SECRETS_FILE} from local secure storage..."
        mpremote cp "${SECRETS_SRC}" ":${SECRETS_FILE}"
        ok "${SECRETS_FILE} deployed."
    fi
else
    # No local secrets template — check if device has one already
    EXISTING=$(mpremote run "
try:
    import os; os.stat('${SECRETS_FILE}'); print('exists')
except:
    print('missing')
" 2>/dev/null) || EXISTING="unknown"

    if [[ "$EXISTING" == "exists" ]]; then
        ok "${SECRETS_FILE} already on device — no local template at ${SECRETS_SRC}, preserving existing."
    else
        warn "No ${SECRETS_FILE} on device AND no local template at ${SECRETS_SRC}"
        warn "The node will run in BLE-only mode (no WiFi) until secrets.py is deployed."
    fi
fi

# ---------------------------------------------------------------------------
# Step 7: Deploy firmware files (from verified rngit release)
# ---------------------------------------------------------------------------
for f in $FIRMWARE_FILES; do
    info "Copying ${f}..."
    mpremote cp "${RELEASE_DIR}/${f}" ":${f}"
done

ok "All firmware files deployed to device."

# ---------------------------------------------------------------------------
# Step 8: Reboot (optional)
# ---------------------------------------------------------------------------
if $FLASH; then
    info "Rebooting device..."
    mpremote run "import machine; machine.reset()" 2>/dev/null || true
    ok "Device rebooted. New firmware should be running."
else
    info "Firmware files are on the device. Reboot manually with:"
    info "  mpremote run 'import machine; machine.reset()'"
    info "Or the device will use the new files on next deep-sleep wake cycle."
fi

ok "Done. ${DEVICE} firmware ${VERSION} deployed successfully."
