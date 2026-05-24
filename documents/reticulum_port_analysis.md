# Reticulum Ingest Port Analysis: pod_peripherals → AgroNomi

## Executive Summary

`reticulum_ingest.py` can be ported to AgroNomi, but it needs 4 new tables, 1 schema alignment, and a database connection refactor. The sensor data flow is clean — `sensor_readings` already exists in AgroNomi with a richer schema than reticulum's version. The main work is adding fleet management tables and switching from raw `sqlite3` to AgroNomi's connection pool.

---

## What AgroNomi Already Has

| Table | Purpose | Compatibility |
|-------|---------|--------------|
| `sensor_nodes` | ESP32-C6 devices (node_id, node_type, field_id, firmware_version, battery_level, calibration, status) | ✅ Superset of reticulum's version. Reticulum's auto-provisioning inserts a subset of these columns. |
| `sensor_readings` | Time-series sensor data (node_id, reading_type, value, unit, depth_cm, recorded_at) | ✅ Superset — has `unit` and `depth_cm` that reticulum's DDL lacks. |
| `sensor_alerts` | Materialized alert snapshot (wiped & rewritten by `sensor_aggregator.py`) | ✅ Reticulum doesn't touch this. Clean separation. |
| `field_thresholds` | Per-field threshold overrides for alerts | ✅ No conflict. |
| `crop_alert_thresholds` | Crop-specific threshold defaults | ✅ No conflict. |
| `node_registry` | BLE address tracking (node_id, ble_address, ble_service_uuid) | ⚠️ Partial overlap with `hardware_devices.ble_mac` — see below. |

## What's Missing (4 new tables needed)

### 1. `hardware_devices` — Physical device fleet registry

Reticulum needs a table that tracks the *physical* device separate from the *logical* sensor node. AgroNomi's `sensor_nodes` is the logical layer; `hardware_devices` is the physical layer.

```sql
CREATE TABLE hardware_devices (
    device_id TEXT PRIMARY KEY,           -- e.g. "SN-AIR-01", "AN-PUMP-01"
    device_type TEXT NOT NULL CHECK(device_type IN (
        'gateway','piw_gateway','soil_node','air_node','pump_node','gh_actuator','vision_node'
    )),
    node_id TEXT UNIQUE REFERENCES sensor_nodes(node_id) ON DELETE SET NULL,
    field_id TEXT REFERENCES fields(field_id) ON DELETE SET NULL,
    ble_mac TEXT,
    ble_target_gateway TEXT,
    firmware_version TEXT DEFAULT '0.0.0',
    hardware_revision TEXT,
    battery_type TEXT DEFAULT '18650_liion',
    install_date TEXT,
    last_seen TEXT,
    status TEXT DEFAULT 'active' CHECK(status IN (
        'active','offline','maintenance','decommissioned'
    ))
);
```

**Relationship**: `hardware_devices.node_id` → `sensor_nodes.node_id`. Auto-provisioning creates rows in both tables on first telemetry.

### 2. `reticulum_gateways` — LoRa gateway tracking

```sql
CREATE TABLE reticulum_gateways (
    gateway_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES hardware_devices(device_id),
    rns_destination_hash TEXT UNIQUE,
    lora_frequency INTEGER DEFAULT 868000000,
    lora_spreading_factor INTEGER DEFAULT 11,
    lora_bandwidth INTEGER DEFAULT 125000,
    lora_coding_rate INTEGER DEFAULT 5,
    lora_tx_power INTEGER DEFAULT 17,
    last_heartbeat TEXT,
    peers_count INTEGER DEFAULT 0,
    mesh_rank INTEGER DEFAULT 0,
    gateway_platform TEXT DEFAULT 'rpi' CHECK(gateway_platform IN ('rak4631','rpi'))
);
```

Auto-populated via RNS announces — no manual entry needed.

### 3. `actuator_commands` — Outbound command queue

AgroNomi has `irrigation_schedule` for water planning but no general-purpose actuator command queue. This is essential for pump/fan/vent control and OTA dispatch.

```sql
CREATE TABLE actuator_commands (
    cmd_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES hardware_devices(device_id),
    cmd_type TEXT NOT NULL CHECK(cmd_type IN (
        'pump_on','pump_off','vent_open','vent_close','shade_pct',
        'fan_on','fan_off','irrigate_mm','ota_request','ota_abort'
    )),
    cmd_value REAL,
    cmd_value_text TEXT,
    requested_by TEXT,
    requested_at TEXT NOT NULL DEFAULT(datetime('now')),
    executed_at TEXT,
    acknowledged_at TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN (
        'pending','transferring','sent','acknowledged','failed','expired','cancelled'
    )),
    retry_count INTEGER DEFAULT 0,
    error_message TEXT
);
```

### 4. `ble_link_log` — BLE diagnostics

```sql
CREATE TABLE ble_link_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT REFERENCES hardware_devices(device_id),
    gateway_id TEXT REFERENCES reticulum_gateways(gateway_id),
    event TEXT CHECK(event IN (
        'connected','disconnected','timeout','rx_packet','tx_packet','rssi_update'
    )),
    rssi INTEGER,
    payload_bytes INTEGER,
    latency_ms INTEGER,
    recorded_at TEXT NOT NULL DEFAULT(datetime('now'))
);
```

## Schema Conflicts to Resolve

| Issue | Reticulum | Pod-Farm | Resolution |
|-------|-----------|----------|------------|
| `sensor_readings` PK | `reading_id` (reticulum) | `id` (AgroNomi) | Use AgroNomi's `id`. Reticulum's `record_telemetry()` just needs the INSERT adjusted. |
| `sensor_nodes` columns | 5 cols (node_id, name, location, last_seen, battery_level) | 9 cols (node_id, node_type, field_id, firmware_version, battery_level, registered_at, calibration_date, calibration_data, status) | Use AgroNomi's richer schema. Reticulum's auto-provision INSERT only populates `node_id` and `name`; the rest default to NULL. `node_type` has no default and is NOT NULL — must be set during auto-provision. |
| `sensor_readings.unit` | Defaults to `''` (reticulum) | NOT NULL, no default (AgroNomi) | Reticulum's `_get_unit()` must always return a value. Currently returns `''` for unknown types — needs to return a non-empty default like `'unknown'` or adjust the AgroNomi schema to allow empty string. |
| `sensor_nodes.node_type` | Not in reticulum schema | NOT NULL in AgroNomi | **Critical**: Auto-provisioning must set `node_type` during INSERT. Map from `device_type` in telemetry: `air_node→'air'`, `soil_node→'soil'`, `pump_node→'pump'`, `gh_actuator→'greenhouse'`. |
| DB path | `./farm_data.db` | `farm_knowledge.db` via `db_pool.py` | **Must switch**. Reticulum needs to use `DatabaseConnectionPool` from AgroNomi's `db_pool.py`. |
| Connection model | `sqlite3.connect()` per call, manual WAL/FK pragmas | `DatabaseConnectionPool` with `get_connection()` / `transaction()` | **Major refactor**. Every `get_db()` call in reticulum_ingest must be replaced with AgroNomi's pool access. Pool already sets WAL, FK, busy_timeout. |
| Thread safety | Fresh `sqlite3.connect()` per call (thread-safe by isolation) | Pool manages connection recycling | Pool handles this, but RNS callbacks must not share connection objects across threads — use `pool.get_connection()` inside each callback. |

## Database Connection Refactor (Critical)

`reticulum_ingest.py` currently uses a standalone `get_db()` function that creates a new `sqlite3.connect()` each time. AgroNomi uses a `DatabaseConnectionPool` with context-managed connections.

**Required changes:**
1. Remove `get_db()` and `_SCHEMA_DDL` from reticulum_ingest.py
2. Import `get_db` / `pool` from AgroNomi's `db_pool.py` (or `farm_knowledge.py`)
3. Replace all `with get_db() as conn:` patterns with `with pool.transaction() as conn:` or `conn = pool.get_connection()`
4. Ensure RNS callbacks (which run on different threads) get fresh connections — the pool handles this, but RNS callbacks must not share connection objects across threads

## `node_registry` vs `hardware_devices` Overlap

AgroNomi's `node_registry` has `ble_address` and `ble_service_uuid`. Reticulum's `hardware_devices` has `ble_mac` and `ble_target_gateway`.

**Recommendation**: Keep `node_registry` for BLE service-level discovery and merge `ble_mac` into `hardware_devices` for fleet routing. They serve different purposes:
- `node_registry`: BLE scanning/discovery metadata
- `hardware_devices`: Fleet management and command routing

## `sensor_aggregator.py` Integration

`sensor_aggregator.py` reads from `sensor_readings` and writes to `sensor_alerts`. It does NOT write to `sensor_readings`. This means:

- **Reticulum writes** → `sensor_readings` (and `hardware_devices`, `sensor_nodes` on auto-provision)
- **Sensor aggregator reads** → `sensor_readings`, evaluates thresholds, writes → `sensor_alerts`
- **No conflict.** Reticulum and sensor aggregator are producer/consumer on the same table.

**One gap**: Sensor aggregator currently doesn't read from `hardware_devices`. It should join through `sensor_nodes` → `hardware_devices` to get `ble_mac`, `device_type`, and `firmware_version` for enriched alert context. This is a minor enhancement.

## `irrigation_schedule` vs `actuator_commands`

AgroNomi has `irrigation_schedule` for planned watering. The new `actuator_commands` table is for real-time device commands (pump on/off, fan on/off, OTA). They serve different purposes:

- `irrigation_schedule`: Planned by the AI/farmer, time-based, stored in AgroNomi DB
- `actuator_commands`: Dispatched by `CommandDispatcher`, device-targeted, status-tracked in real-time

An `irrigate_mm` command type in `actuator_commands` could bridge the two — the irrigation scheduler inserts a row into `actuator_commands` to actually trigger the pump.

## Config: Keep Reticulum Separate

Reticulum uses its own `~/.reticulum/config` for interface, transport, and RNS-level settings. This should **not** be merged into AgroNomi's `config.yaml`. The two configs serve different layers:

- **`~/.reticulum/config`** — RNS interfaces (RNode, TCP, etc.), transport settings, share_instance. Already works standalone.
- **`config.yaml`** — AgroNomi application config (models, DB, sensors, UI).

What **does** need adding to AgroNomi's `config.yaml` is a small `ReticulumConfig` in `config/schemas.py` for **application-level** RNS settings only:

```yaml
reticulum:
  identity_path: "./farm_hub.identity"
  announce_interval: 30
  command_poll_interval: 5
  ota:
    firmware_dir: "/var/agronomi/fw"
    window_start: 21
    window_end: 24
    max_retries: 3
```

RNS aspect names (`farm.telemetry_readings`, `farm.commands_control`, `farm.gateway_commands`) are constants in the code, not config — they must match between hub and gateway, so hardcoding them is correct.

## Reticulum Networking: The Complete Stack

Reticulum is AgroNomi's **complete networking layer** — not just a LoRa transport. It handles routing, encryption, path discovery, multi-path failover, intermittency tolerance, and identity management independently of any underlying IP infrastructure. AgroNomi does **not** require Tailscale, WireGuard, DHCP, or any IP infrastructure for its RNS mesh to function.

The previous version of this document treated Tailscale as the networking foundation with RNS riding on top. That framing was wrong. RNS builds its own mesh from whatever physical transports are available. Tailscale (or any other IP underlay) is **one optional way** to provide a fast transport for RNS TCP interfaces — never a prerequisite.

### RNS is a Toolkit, Not a Network You Join

RNS is not a single network you "join" — it is a **toolkit for creating networks**. AgroNomi uses RNS to build its own private fleet mesh. There is no central RNS network to connect to, no address allocation to coordinate, and no authority to register with. Each AgroNomi node generates its own destinations and announces them. Destination uniqueness is guaranteed by SHA-256 collision resistance and public key inclusion — not by central coordination.

This means:
- **No address planning**: No subnets, no DHCP, no IP coordination. Each node's `farm.telemetry_readings` destination is unique because it includes the node's identity public key in the hash.
- **Global portability**: A destination can move across interfaces, mediums, or even between separate RNS networks by sending an announce on the new medium. A gateway that switches from LoRa to TCP keeps the same destination hash.
- **Implicit authentication**: Communication to a destination is inherently authenticated — only the holder of the corresponding private key can decrypt and respond. No TLS certificates, no PKI.
- **Sender anonymity**: RNS packets do not include source addresses. An observer intercepting a packet cannot determine who sent it, only who it is addressed to (unless IFAC is enabled, in which case nothing can be determined).

### Transport Nodes vs. Instances — Who Should Route?

RNS distinguishes two node types: **Instances** (can send/receive packets, host destinations) and **Transport Nodes** (additionally forward packets, rebroadcast announces, serve path requests, cache public keys). Every Transport Node is also an Instance, but not every Instance needs to be a Transport Node.

The RNS documentation warns: "Letting every node be a transport node will in most cases degrade the performance and reliability of the network."

For AgroNomi:

| Node Type | Transport Node? | Rationale |
|-----------|-----------------|----------|
| Central Hub | ✅ Yes | Stationary, always-on, well-connected. Serves as distributed keystore (caches public keys for the fleet), routes announces between interfaces. |
| Field Gateway (current: mimi) | ⚠️ Probably yes | Currently the only link between field nodes and hub via LoRa. Needs to rebroadcast hub's announces to reach any future LoRa peers. |
| Field Gateway (future: 5+ gateways on same LoRa) | ❌ Consider Instance only | With many gateways on the same LoRa frequency, every gateway rebroadcasting every announce wastes airtime. Only 1-2 gateways need to be Transport Nodes on LoRa. |
| ESP32 sensor/actuator | ❌ No | micoreticulum node — communicates via BLE field rnode. Perhaps local only transport needs to be enabled on microreticulum nodes. |

The key tradeoff: Transport Nodes rebroadcast announces and forward packets for other nodes, which is essential for multi-hop connectivity but consumes bandwidth on slow mediums like LoRa. With only one gateway (mimi), the overhead is minimal. As the fleet scales, designate only the best-connected gateways as Transport Nodes on LoRa.

**Additional Transport Node benefit**: Transport Nodes function as **distributed cryptographic keystores**. When a destination announces itself, Transport Nodes cache its public key. Other nodes can request unknown public keys from the network, and Transport Nodes respond with the cached information. This eliminates the need for a central key server.

### Local Traffic Prioritization

When bandwidth is constrained (LoRa at 250 B/s), RNS prioritizes announces for **nearby destinations** (lower hop count). This means local LoRa traffic between a gateway and the hub is prioritized over announces from distant network segments. The 2% announce bandwidth cap ensures that even under heavy announce traffic, data still gets through.

However, this prioritization only works correctly with proper interface mode configuration. If the hub has a fast TCP interface and a slow LoRa interface, and both are in `full` mode, announces from the fast segment will be forwarded to the slow segment up to the 2% bandwidth cap. This is usually fine for AgroNomi's small fleet, but for larger deployments, the hub's Internet-facing interface should be in `boundary` mode to control announce propagation from fast to slow segments.

### What RNS Provides Natively

| Capability | How RNS Handles It | Without Any IP Infrastructure |
|-----------|---------------------|----------------------------|
| **Routing** | Per-destination path table, next-hop routing, multi-hop forwarding via `enable_transport` | ✅ LoRa-only mesh routes packets between any number of hops |
| **Encryption** | Ed25519 identities + X25519 ECDH key exchange per link; all packets authenticated and encrypted | ✅ No TLS, no cert management, no PKI |
| **Path Discovery** | Announce-based: nodes broadcast signed announce packets, path tables built automatically | ✅ Gateways discover hub and vice versa via RNS announces |
| **Multi-path** | All active interfaces available; **single best-path per destination** selected by hop count + announce emission time; automatic failover when path becomes unresponsive | ✅ LoRa is the baseline; any additional interface adds faster-path *candidates*; unresponsive paths replaced by announces from other interfaces |
| **Intermittency** | TCP interfaces auto-reconnect (5s retry for TCPClient, 15s for I2P); path tables persist across reconnections; unresponsive paths replaced | ✅ Gateway power cycle → RNS reconnects automatically |
| **Identity** | Each node has a persistent RNS Identity (Ed25519+X25519); identities discovered via announces | ✅ No manual key exchange, no certificate authority |
| **Fleet Trust** | Network Identities federate nodes under one administrative domain; encrypted discovery announces | ✅ Private fleet channel without any external CA |
| **Bootstrap** | `bootstrap_only` interfaces auto-detach once faster auto-connected interfaces are available | ✅ LoRa bootstraps discovery of TCP endpoints, then steps aside |

### Available RNS Interface Types

| Interface | Transport | Speed | Self-Provisioning | Notes |
|-----------|-----------|-------|-------------------|-------|
| **AutoInterface** | IPv6 link-local multicast + UDP | Fast (LAN) | ✅ Zero config — discovers peers on same Ethernet/WiFi segment | Works on any LAN without routers, DHCP, or Tailscale. The primary LAN transport for AgroNomi. |
| **TCPClientInterface** | TCP to a remote server | Fast (WAN/LAN) | Needs `target_host` + `target_port` | Outbound connection to a listening peer. Auto-reconnects on link loss (5s). |
| **TCPServerInterface** | TCP listening socket | Fast (WAN/LAN) | Needs `listen_ip` + `listen_port` | Accepts inbound TCP connections from many peers. |
| **BackboneInterface** | TCP (Linux/Android only) | Fastest | Needs `listen_on`/`remote` + `port` | Kernel-event I/O backend — handles thousands of clients efficiently. Linux/Android only. |
| **RNodeInterface** | LoRa via RNode USB | ~250 B/s | Needs `port` + LoRa params | Current production path. Always-on field transport where no IP exists. |
| **I2PInterface** | I2P anonymous overlay | Slow | Needs `i2pd` running | Full NAT traversal, no public IP required. For censored or heavily firewalled deployments. |
| **PipeInterface** | Stdin/stdout to external program | Varies | Needs `command` | Bridges RNS to any custom transport — including `tailscale netcat`. |
| **UDPInterface** | UDP (broadcast or unicast) | Fast | Needs IP + port config | AutoInterface is generally better for LAN discovery. |
| **SerialInterface** | Serial port | Varies | Needs `port` + `speed` | Direct wire or radio modem. |

### Interface Modes — When to Use Each

Interface modes control three distinct behaviors: **announce propagation** (which announces get rebroadcast on which interfaces), **path request handling** (whether the node actively discovers unknown paths on behalf of clients), and **path expiry** (how long learned paths remain valid). These are not like standard networking modes — they are RNS-specific controls for shaping how the mesh self-organizes.

> **Important**: Mode effects on announce propagation and path request proxying only apply when `enable_transport = yes`. Without transport, the node does not rebroadcast announces or forward path requests, so most mode behaviors are moot. Path expiry times always apply regardless of transport status.

| Mode | Announce Propagation | Path Request Proxying | Path Expiry | Use For |
|------|---------------------|---------------------|------------|--------|
| **`full`** (default) | Normal — rebroadcasts on all interfaces (bandwidth-capped) | No — only answers known paths | 7 days | General-purpose interfaces |
| **`gateway`** | Normal — same as `full` for announces | **Yes** — forwards unknown path requests to all other interfaces | 7 days | Interface **facing clients** who need the node to discover paths on their behalf |
| **`access_point`** | **Completely blocked** — no announce broadcasts on this interface (only targeted path responses) | **Yes** — same as `gateway` | 24 hours | Radio interfaces serving transient clients; saves airtime by never broadcasting announces |
| **`roaming`** | Blocked if next-hop is `roaming` or `boundary`; allowed if next-hop is `full`/`gateway`/`access_point` | **Yes** — same as `gateway` | 6 hours | Mobile nodes (vehicle-mounted LoRa); prevents transient positions from becoming routing intermediaries |
| **`boundary`** | Blocked if next-hop is `roaming`; **allowed** if next-hop is `boundary` | **No** — only answers known paths | 7 days | Interface connecting to a **different network segment** (e.g., the Internet-facing side of a LoRa↔IP bridge) |

**Key distinctions that are easy to get wrong:**

- `gateway` and `full` are **identical for announce propagation** — gateway's ONLY difference is the path request proxying behavior. Gateway does NOT block or filter any announces.
- `access_point` does NOT mean "reduces announce rate" — it means **zero outbound announce broadcasts**. The only announces that leave an AP interface are targeted path responses to specific requestors. The node's own local destinations are always announced (local destinations bypass mode checks).
- `boundary` allows announces from other `boundary` next-hops; `roaming` does not. This is the critical code difference: `roaming` blocks both `roaming`+`boundary` next-hops; `boundary` only blocks `roaming` next-hops.
- `boundary` is NOT in `DISCOVER_PATHS_FOR` — it will not proxy path requests for unknown destinations. This differs from `gateway`, `access_point`, and `roaming`, which all do proxy.

**Announce propagation rules (from Transport.outbound() source code):**

| Outgoing Interface Mode ↓ | Next-hop `full`/`gateway` | Next-hop `access_point` | Next-hop `roaming` | Next-hop `boundary` |
|---|---|---|---|---|
| **`full` / `gateway`** | ✅ Forward | ✅ Forward | ✅ Forward | ✅ Forward |
| **`access_point`** | ❌ Block all broadcast | ❌ Block all broadcast | ❌ Block all broadcast | ❌ Block all broadcast |
| **`roaming`** | ✅ Forward | ✅ Forward | ❌ Block | ❌ Block |
| **`boundary`** | ✅ Forward | ✅ Forward | ❌ Block | ✅ Forward |

*(Local/instance-internal destinations always bypass mode checks and announce on all interfaces. Targeted path responses sent via the `attached_interface` mechanism also bypass the AP block — this is how AP clients receive path query answers.)*

**AgroNomi mode assignments:**
- Hub's TCPServerInterface → `full` — gateways discover the hub's destinations via announces (which propagate identically in `full` and `gateway` mode). `gateway` mode would add path request proxying for inter-gateway discovery, but that's unnecessary when announces propagate normally through the hub's rebroadcast. See discussion below.
- Hub's AutoInterface (LAN) → `full` — AutoInterface is a symmetric peer mesh, not a client-server interface. Every node on the LAN discovers every other node directly. `gateway` mode here would cause the hub to proxy path requests for *any* random LAN peer (including unrelated RNS nodes), wasting airtime on LoRa for non-AgroNomi traffic.
- Hub's LoRa RNode → `full` — the hub must broadcast its own announces on LoRa so gateways discover `farm.telemetry_readings`; using `access_point` here would prevent gateways from ever finding the hub over LoRa
- Gateway's LoRa RNode → `full` — same reasoning; the gateway must announce `farm.gateway_commands` on LoRa so the hub can discover it
- Hub's Internet-facing interface → `boundary` — connects a fast segment (Internet) to the local mesh; controls announce propagation from fast to slow segments

> **Why NOT `gateway` everywhere?** In RNS, `gateway` mode does NOT mean "this is a gateway device." It means: "when a node on this interface sends a path request for an unknown destination, forward that request to all other interfaces." This is a narrow, specific behavior — it only matters when path requests arrive for destinations the hub doesn't already know about. In AgroNomi, all destinations announce themselves proactively, so gateways learn paths from announces, not from path requests. Setting `gateway` on AutoInterface is actively harmful — it makes the hub a general-purpose path resolver for any node on the LAN, including unrelated RNS peers.
>
> The one scenario where `gateway` on the TCPServerInterface could help: a multi-gateway fleet where announces get rate-limited and a gateway needs to explicitly request a path to another gateway. This is unlikely with a small fleet (2-5 gateways) but could matter at scale. If you hit this, change the TCPServerInterface from `full` to `gateway` — it's a single config line, no code changes needed.
>
> **Why NOT `access_point` on LoRa?** The `access_point` mode blocks ALL outbound announce broadcasts on that interface. For AgroNomi, the hub MUST announce its destinations (`farm.telemetry_readings`, `farm.commands_control`) on LoRa so gateways can discover them. If the hub's LoRa interface were `access_point`, gateways would never receive the hub's announces — they would only reach the hub by sending a path request first, and they wouldn't know to send a path request because they don't know the hub exists. `access_point` is designed for scenarios like a community LoRa hub where many transient users already know the hub's address and send path requests to it. AgroNomi's gateways discover the hub via announces, not via pre-configured addresses, so `full` is correct.

### AutoInterface — Zero-Config LAN Mesh

`AutoInterface` is the single most important interface for AgroNomi deployments where hub and gateway share a LAN. It requires **zero IP infrastructure** — no router, no DHCP, no DNS, no Tailscale. It works on any Ethernet or WiFi segment that supports IPv6 link-local addresses (`fe80::`), which all modern OSes enable by default.

**How it works:**
1. On startup, enumerates all system network interfaces with `fe80::` link-local IPv6 addresses
2. Computes a multicast discovery address from SHA-256 of the `group_id` (default: `reticulum`)
3. Joins the IPv6 multicast group on port 29716 and sends a 16-byte discovery token every 1.6 seconds
4. When a peer's discovery token is received and verified, an `AutoInterfacePeer` child interface is spawned
5. Data flows over unicast UDP on port 42671 — fast, no multicast overhead for payloads
6. Reverse peering (unicast keepalive) every `announce_interval × 3.25` seconds keeps both directions alive
7. Carrier loss detection: if no multicast echo within 6.5 seconds, interface is flagged down; auto-recovers
8. Link-local address monitoring: if the IPv6 address changes (e.g., WiFi reconnect), the UDP listener is rebuilt automatically

**Configuration:**
```ini
[[LAN Mesh]]
  type = AutoInterface
  enabled = yes
  group_id = agronomi            # isolates AgroNomi RNS traffic from other RNS users on same LAN
  discovery_scope = link         # link-local scope (same LAN segment)
  # discovery_port = 29716      # default, rarely needs changing
  # data_port = 42671           # default, rarely needs changing
```

**Limitations:**
- Same L2 broadcast domain only (no router traversal) — this is a feature, not a bug, for LAN isolation
- Requires UDP ports 29716 and 42671 open (some APs enforce client isolation; disable it)
- Requires `fe80::` IPv6 link-local addresses (enabled by default on macOS, Linux, Windows)
- Cannot traverse NAT or reach across the Internet — use TCP/I2P interfaces for that

### Discoverable Interfaces (RNS 1.2+) — Auto-Connect Over Any Transport

RNS 1.2+ introduces **discoverable interfaces**: any interface can publish its connection details (type, address, port, IFAC credentials, geographic location) as signed announce packets. Other nodes with `autoconnect_discovered_interfaces` enabled auto-discover and auto-connect. This is RNS's built-in zero-config mesh building — no Tailscale, no manual IP exchange.

**Discovery announce contents:**
- Interface type (TCPServerInterface, BackboneInterface, I2PInterface)
- Connection parameters (address, port)
- `reachable_on` address for remote peers (or a script that resolves it dynamically)
- Discovery name, stamp (proof-of-work anti-spam), optional IFAC credentials
- Optional geographic coordinates, radio parameters
- Optionally encrypted via `discovery_encrypt` + Network Identity

**Hub publishes its TCP server as discoverable:**
```ini
[[AgroNomi Hub TCP]]
  type = TCPServerInterface
  mode = full
  listen_on = 0.0.0.0
  port = 4242
  discoverable = yes
  discovery_name = AgroNomi Hub
  reachable_on = /usr/local/bin/get_reachable_ip.sh   # resolves current reachable IP
  latitude = 46.0569
  longitude = 14.5058
  announce_interval = 720    # every 12h — low frequency because discovery is sticky
```

**Gateway auto-connects to discovered interfaces:**
```ini
[reticulum]
  enable_transport = yes
  autoconnect_discovered_interfaces = 3
```

When the gateway receives the hub's discovery announce over LoRa (or any other active interface), it automatically creates a `TCPClientInterface` connecting to the hub's published address. The `reachable_on` script resolves the correct IP regardless of whether it's a Tailscale CGNAT address, a public IP, or a LAN IP — RNS doesn't care what the IP is, only that it can reach it.

### Network Identities — Fleet Trust Boundary

A **Network Identity** is a standard RNS Identity keypair (Ed25519 + X25519) that federates multiple Transport Instances under one verifiable administrative domain. It provides:

- **Encrypted discovery**: When `discovery_encrypt = yes` + `network_identity` is configured, discovery announces are encrypted so only fleet members with the same Network Identity can decode them
- **Verified authorship**: Discovery announces are signed with the Network Identity, proving they come from a node holding the fleet's private key
- **Whitelisted trust zones**: Nodes can configure `interface_discovery_sources` to only accept discovers from specific Network Identities

Generate a fleet Network Identity:
```bash
rnid -g ~/.reticulum/storage/identities/agronomi_fleet
```

Configure on all fleet nodes:
```ini
[reticulum]
  network_identity = ~/.reticulum/storage/identities/agronomi_fleet
```

And enable encrypted discovery on discoverable interfaces:
```ini
[[AgroNomi Hub TCP]]
  type = TCPServerInterface
  discoverable = yes
  discovery_encrypt = yes
  # network_identity is global, not per-interface
```

This makes AgroNomi's mesh completely private — no one outside the fleet can discover or auto-connect to the hub's TCP server, even if they're on the same LoRa frequency.

### IFAC — Per-Interface Authentication

Network Identity with `discovery_encrypt` controls **who can discover** the hub's TCP endpoint. But discovery encryption only protects the discovery announce — once a connection is established, the data stream on that interface is not encrypted by Network Identity. IFAC (Interface Access Code) adds per-interface authentication and encryption that controls **who can use** the interface.

The two serve different purposes and are complementary, not redundant:

| Layer | What It Protects | Mechanism | Scope |
|-------|-----------------|-----------|-------|
| Network Identity | Who can **discover** the interface | `discovery_encrypt` encrypts the discovery announce | Fleet-wide (one identity for all nodes) |
| IFAC | Who can **use** the interface | Per-interface passphrase encrypts all traffic | Per-interface (different keys for different interfaces) |

**For AgroNomi, IFAC is most important on the hub's TCP interface**, where the hub is exposed to the Internet. Without IFAC, anyone who discovers the TCP endpoint (or scans port 4242) can establish an RNS link. With IFAC, only nodes that know the passphrase can connect.

```ini
# Hub's TCP interface with IFAC authentication
[[AgroNomi Hub TCP]]
  type = TCPServerInterface
  mode = full
  listen_on = 0.0.0.0
  port = 4242
  discoverable = yes
  discovery_encrypt = yes          # Network Identity encrypts discovery
  reachable_on = /opt/agronomi/bin/get_reachable_ip.sh
  discovery_stamp_value = 20
  ifac_size = 3                   # 3-byte key = human-shareable passphrase
  # ifac_key = <auto-generated>    # Set during fleet provisioning
```

The `ifac_size = 3` creates a short, human-shareable key (like a WPA password) that can be shared verbally with field operators. The key is generated during `agronomi-setup init-fleet` and distributed alongside the fleet identity.

**LoRa IFAC** is optional — if you're in an area where other RNS nodes share the same LoRa frequency, IFAC prevents them from joining your mesh. If you're in a remote area with no other RNS nodes, IFAC is unnecessary overhead. LoRa traffic is already encrypted per-link by RNS's built-in ECDH key exchange — IFAC adds an additional authentication layer.

**AutoInterface IFAC** is generally unnecessary on LAN — you're already on a trusted network segment. IFAC on AutoInterface would prevent any non-fleet RNS peer on the same LAN from communicating with the hub.

You can mix authenticated and open interfaces on the same system: the hub can have IFAC on its TCP interface (WAN, untrusted) while leaving AutoInterface (LAN, trusted) and LoRa (shared medium, optionally IFAC'd) open. This is the recommended AgroNomi configuration.

### Bootstrap Pattern — LoRa as Discovery, TCP as Data

The `bootstrap_only` flag on an interface designates it as a temporary connectivity bridge. The canonical AgroNomi pattern:

1. Gateway starts with only LoRa (`RNodeInterface`) active
2. Through LoRa, the gateway receives the hub's discovery announce (which includes the hub's TCP address)
3. Gateway auto-connects to the hub's TCP server via `autoconnect_discovered_interfaces`
4. Once the auto-connected interface is up, RNS checks: does the auto-connect limit allow detaching the bootstrap interface?
5. If yes, the `bootstrap_only` LoRa interface is **automatically detached** — resources freed, airtime saved
6. If the TCP link later drops, the `bootstrap_only` interface is **not automatically re-attached** — RNS documentation only describes the detach behavior, not a re-attach mechanism. LoRa stays detached unless the operator manually re-enables it or the RNS instance is restarted. **This is why keeping LoRa as a permanent fallback (not `bootstrap_only`) is recommended for AgroNomi.**

**Recommended configuration (LoRa kept as always-on fallback, not bootstrap_only):**
```ini
[[LoRa Radio]]
  type = RNodeInterface
  mode = full
  # No bootstrap_only — keep LoRa alive for redundancy and field devices
  # mode = full (not access_point) because hub and gateway discover each other via announces on LoRa
```

**Optional configuration (aggressive bandwidth saving, LoRa detaches when TCP is up):**
```ini
[[LoRa Bootstrap]]
  type = RNodeInterface
  mode = full
  bootstrap_only = yes
  # LoRa auto-detaches once TCP auto-connect limit is reached
  # mode = full so announces propagate during bootstrap phase
```

For AgroNomi, **keeping LoRa as a permanent fallback** is recommended — the airtime cost is modest (LoRa at SF11/125kHz has a 2% announce cap, meaning at most a few seconds of airtime per minute), and it guarantees connectivity when all IP paths fail.

### Tailscale: Optional IP Underlay, Not a Dependency

Tailscale provides a WireGuard mesh VPN with stable CGNAT IPs. It is **one way** to give hub and gateway a routable IP for RNS TCP interfaces. It is never required.

| Connectivity Scenario | RNS Transport | Tailscale Needed? |
|---------------------|---------------|-------------------|
| Hub + gateway on same LAN | `AutoInterface` (zero config) | ❌ No |
| Hub + gateway on same LAN + want faster fallback | `AutoInterface` + RNode LoRa | ❌ No |
| Hub + gateway on different networks, behind NAT | `TCPServerInterface` (hub) + `TCPClientInterface` (gateway) via Tailscale IP | ✅ Helpful for NAT traversal |
| Hub + gateway on different networks, no Tailscale | `I2PInterface` (NAT traversal, slow) or public IP | ❌ No (I2P works) |
| Hub + gateway on different networks, custom transport | `PipeInterface` → `tailscale netcat` or SSH tunnel | ⚠️ Optional (PipeInterface handles the bridge) |
| Field deployment, no IP at all | `RNodeInterface` (LoRa only) | ❌ No |

**When Tailscale is useful:**
- Hub and gateway are on **different networks** behind NAT with no port forwarding
- You want a fast, encrypted WAN tunnel for RNS TCP interfaces without exposing ports publicly
- You already run Tailscale for other purposes and want to leverage it

**When Tailscale is unnecessary:**
- Hub and gateway are on the **same LAN** → `AutoInterface` handles everything
- Hub has a **public IP** → gateways connect directly via `TCPClientInterface`
- You're okay with **I2P** for NAT traversal (slower but no Tailscale dependency)
- You're in a **field deployment** with no IP → LoRa is the only path

**Integrating Tailscale status into RNS config (when used):**

If Tailscale is present, `tailscale.py`'s `get_tailscale_status()` can dynamically populate the `reachable_on` field in the hub's discoverable interface announce. This ensures the published address stays correct through Tailscale IP changes:

```ini
[[AgroNomi Hub TCP]]
  type = TCPServerInterface
  mode = full
  discoverable = yes
  reachable_on = /usr/local/bin/get_tailscale_ip.sh
  # get_tailscale_ip.sh runs: tailscale status --json | jq -r '.TailscaleIPs[0]'
```

Alternatively, use `PipeInterface` to bridge directly through Tailscale without needing a separate TCP server:

```ini
[[Tailscale Bridge]]
  type = PipeInterface
  enabled = yes
  command = tailscale netcat <hub-tailscale-hostname> 4242
  respawn_delay = 15    # auto-reconnect on link loss
```

### Fleet Auto-Provisioning Architecture

The current system has one hub and one gateway (mimi). Scaling to multiple field gateways at different farm locations requires a **fleet auto-provisioning system** that eliminates all manual RNS configuration. Farm operators should plug in a gateway, power it on, and have it join the mesh within seconds.

#### Fleet Topology

```
                          ┌──────────────────────┐
                          │    Central Hub        │
                          │    (Mac mini)         │
                          │    Transport Node     │
                          │                      │
                          │  AutoInterface (full) │
                          │  TCPServer (full, disc)│
                          │  RNode LoRa (full)    │
                          └──────────┬───────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              │                      │                      │
     ┌────────┴─────────┐  ┌────────┴─────────┐  ┌────────┴─────────┐
     │  RNode LoRa BLE  │  │  RNode LoRa BLE  │  │  RNode LoRa BLE  │
     │    (field 1)     │  │  (field 2)       │  │  (field 3)       │
     │                  │  │                  │  │                  │
     │   │     │
     └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
              │                      │                      │
              │                      │                      │
        ┌─────┴─────┐        ┌───────┴──────┐        ┌───────┴──────┐
        │ESP32c6 BLE          ESP32c6 BLE              ESP32c6 BLE  
        │  nodes    │        │  nodes        │        │  nodes        │
        │              │        │        │        │        │
        │           │        │        │        │        │
        └───────────┘        └──────────────┘        └──────────────┘
```

Each esp node can connect via BLE rnode to reticulum network. Nodes discover the hub via:
1. **AutoInterface** — if on same LAN (zero config, instant)
2. **LoRa announces** — if on same LoRa frequency (no IP needed)
3. **Discoverable TCP** — hub publishes its TCP server; nodes auto-connect via `autoconnect_discovered_interfaces`
4. **I2P tunnel** — if behind strict NAT with no Tailscale (slow but always works)
5. **Tailscale TCP** — if on a remote network running Tailscale (fast, uses Tailscale CGNAT IP)

#### Fleet Identity — Network Identity for Trust

All fleet members share a **Network Identity** — a single RNS Identity keypair that federates the fleet. This provides:
- **Encrypted discovery**: Only fleet members can decode the hub's TCP address from discovery announces
- **Verified authorship**: Discovery announces are signed, proving they come from the fleet operator
- **Whitelisted trust**: `interface_discovery_sources` restricts auto-connect to only fleet-published interfaces

**Provisioning flow:**
1. Operator generates Network Identity **once** on the hub: `rnid -g /opt/agronomi/fleet_identity`
2. The identity file is copied to every gateway during setup (or downloaded from a secure channel)
3. All nodes set `network_identity` in their `[reticulum]` config
4. All discoverable interfaces set `discovery_encrypt = yes`
5. All nodes set `interface_discovery_sources` to the hub's transport identity hash (optional, for strict whitelisting)

If the Network Identity file doesn't exist on a node, RNS will auto-generate one at startup — but that would be a **different** identity from the fleet's, so it couldn't decrypt fleet discovery announces. The operator must ensure the **same** identity file is on every node. (The auto-generation behavior is from the `Reticulum.__init__()` source code — if `network_identity` is specified but the file doesn't exist, a new Identity is created and saved to that path. This is convenient for first-time setup but dangerous for fleet consistency.)

#### Hub Auto-Provisioning

The hub is the fleet's central Transport Node. It must:
- Listen for incoming TCP connections from nodes (discoverable)
- Listen for LoRa traffic from field nodes
- Accept LAN connections via AutoInterface
- Proxy path requests on behalf of connected gateways

**`autoconfigure_rns(role="hub")` generates:**

```ini
[reticulum]
  enable_transport = Yes
  share_instance = Yes
  network_identity = /opt/agronomi/fleet_identity
  discover_interfaces = Yes
  autoconnect_discovered_interfaces = 0
  # Hub does not auto-connect — it publishes its own interfaces for gateways to connect to

[[LAN Mesh]]
  type = AutoInterface
  enabled = yes
  group_id = agronomi
  mode = full
  # Symmetric peer discovery; hub is not a path resolver for random LAN nodes

[[AgroNomi Hub TCP]]
  type = TCPServerInterface
  mode = full
  listen_on = 0.0.0.0
  port = 4242
  discoverable = yes
  discovery_name = AgroNomi Hub
  discovery_encrypt = yes
  reachable_on = /bel/CascadeProjects/pod_peripherals/get_reachable_ip.sh
  discovery_stamp_value = 20
  announce_interval = 720

[[LoRa Radio]]
  type = RNodeInterface
  mode = full
  port = /dev/ttyUSB0
  frequency = 868000000
  bandwidth = 125000
  spreadingfactor = 11
  codingrate = 5
  txpower = 17
```

**`get_reachable_ip.sh`** — dynamically resolves the address gateways should connect to:
```bash
#!/bin/sh
# Try Tailscale first (stable CGNAT IP, works across NAT)
TS_IP=$(tailscale status --json 2>/dev/null | jq -r '.TailscaleIPs[0]')
if [ -n "$TS_IP" ] && [ "$TS_IP" != "null" ]; then
  echo "$TS_IP"
  exit 0
fi
# Fall back to public IP
PUBLIC_IP=$(curl -s --max-time 3 ip.me 2>/dev/null)
if [ -n "$PUBLIC_IP" ]; then
  echo "$PUBLIC_IP"
  exit 0
fi
# No WAN IP — discovery announce fails gracefully, LAN/LoRa still work
exit 1
```

#### Nodes Auto-Provisioning

BLE field nodes are part of the RNS mesh. They must:
- Connect to the hub by whatever path is available (LoRa discovery, TCP auto-connect, I2P)
- Announce themselves so the hub discovers their `farm.gateway_commands` destination
- They always have BLE Rnode available.

**`autoconfigure_rns(role="gateway")` generates:**

```ini
[reticulum]
  enable_transport = Yes
  share_instance = Yes
  network_identity = /opt/agronomi/fleet_identity
  discover_interfaces = Yes
  autoconnect_discovered_interfaces = 3
  # Auto-connect to up to 3 discovered hub TCP servers
  required_discovery_value = 16
  # Reject discovery announces with stamp value < 16 (spam filter)

[[LAN Mesh]]
  type = AutoInterface
  enabled = yes
  group_id = agronomi
  mode = full
  # Standard LAN discovery; gateway is not a mesh entry point

[[LoRa Radio]]
  type = RNodeInterface
  mode = full
  port = /dev/ttyUSB0
  frequency = 868000000
  bandwidth = 125000
  spreadingfactor = 11
  codingrate = 5
  txpower = 17
```

**No TCPServerInterface on field nodes** — they are clients, not servers. The node discovers the hub's TCP server via the LoRa-carried discovery announce (or via AutoInterface on LAN), then auto-connects as a TCP client.

**Gateway auto-connect flow:**
1. Gateway starts → AutoInterface scans LAN, LoRa comes up
2. **If on same LAN as hub**: AutoInterface discovers hub instantly → direct UDP data path → fastest option
3. **If LoRa only**: Gateway hears hub's announce on `farm.telemetry_readings` → discovers hub identity/path. LoRa also carries hub's discovery announce → gateway learns hub's TCP address → `autoconnect_discovered_interfaces` auto-creates a `TCPClientInterface` to the hub → TCP connection established over Tailscale or public IP
4. **If behind strict NAT, no Tailscale**: Auto-provisioning falls back to `I2PInterface` (see below)
5. RNS uses the fastest available path: AutoInterface > TCP > I2P > LoRa
6. If all IP drops, LoRa continues as the always-on fallback

#### Connectivity Detection — `detect_rns_environment()`

Before generating config, the auto-provisioner must detect what's actually available:

```python
def detect_rns_environment() -> dict:
    """Detect available RNS connectivity on this machine."""
    env = {
        "role": None,               # "hub" or "gateway" — from config or auto-detect
        "has_ipv6_ll": False,       # fe80:: addresses → AutoInterface viable
        "tailscale_ip": None,       # Tailscale CGNAT IP
        "public_ip": None,          # Public IP (from curl ip.me)
        "rnode_devices": [],        # List of RNode USB serial ports
        "lan_ip": None,             # Local LAN IP for reference
        "i2p_available": False,     # i2pd running
        "platform": None,           # linux, macos, windows
    }

    # 1. Check role — from agronomi config or first-telemetry auto-detect
    #    Hub: has farm_data.db, runs reticulum_ingest.py
    #    Gateway: has ble_forwarder.toml, Pico 2W attached

    # 2. Check IPv6 link-local addresses
    # Note: netinfo is an internal RNS utility (RNS.Interfaces.AutoInterface.netinfo)
    # not a public API — may change between RNS versions
    from RNS.Interfaces.AutoInterface import netinfo
    for iface in netinfo.interfaces():
        addrs = netinfo.ips(iface)
        if any(a.startswith("fe80::") for a in addrs):
            env["has_ipv6_ll"] = True
            break

    # 3. Check Tailscale
    try:
        from tailscale import get_tailscale_status
        ts = get_tailscale_status()
        if ts.get("connected"):
            env["tailscale_ip"] = ts["ip"]
    except Exception:
        pass

    # 4. Check for RNode USB devices
    import serial.tools.list_ports
    for port in serial.tools.list_ports.comports():
        if "RNode" in port.description or "1a86" in port.hwid:
            env["rnode_devices"].append(port.device)

    # 5. Check for i2pd
    import subprocess
    try:
        result = subprocess.run(["i2pd", "--version"], capture_output=True, timeout=3)
        env["i2p_available"] = result.returncode == 0
    except Exception:
        pass

    return env
```

#### Config Generation — `autoconfigure_rns()`

```python
def autoconfigure_rns(role: str, env: dict, configdir: str = "~/.reticulum") -> str:
    """
    Generate RNS config for the given role and detected environment.
    Returns the path to the generated config file.
    """
    from RNS.vendor.configobj import ConfigObj

    configdir = os.path.expanduser(configdir)
    os.makedirs(configdir, exist_ok=True)
    os.makedirs(f"{configdir}/storage/identities", exist_ok=True)

    config = ConfigObj()
    config.filename = f"{configdir}/config"

    # ── [reticulum] section ──
    config["reticulum"] = {}
    # enable_transport = Yes for hub and small-fleet gateways.
    # For fleets with 3+ gateways on same LoRa, consider making extra gateways
    # Instance-only (enable_transport = No) to reduce LoRa announce rebroadcasts.
    config["reticulum"]["enable_transport"] = "Yes"
    config["reticulum"]["share_instance"] = "Yes"

    # Fleet identity (must exist on all nodes)
    fleet_id_path = "/opt/agronomi/fleet_identity"
    if os.path.isfile(fleet_id_path):
        config["reticulum"]["network_identity"] = fleet_id_path
        config["reticulum"]["discover_interfaces"] = "Yes"

    if role == "hub":
        config["reticulum"]["autoconnect_discovered_interfaces"] = "0"
    else:
        config["reticulum"]["autoconnect_discovered_interfaces"] = "3"
        config["reticulum"]["required_discovery_value"] = "16"

    # ── [interfaces] section ──
    config["interfaces"] = {}

    # AutoInterface — always enable if IPv6 link-local is present
    if env.get("has_ipv6_ll"):
        iface_name = "LAN Mesh"
        config["interfaces"][iface_name] = {}
        config["interfaces"][iface_name]["type"] = "AutoInterface"
        config["interfaces"][iface_name]["enabled"] = "yes"
        config["interfaces"][iface_name]["group_id"] = "agronomi"
        config["interfaces"][iface_name]["mode"] = "full"
        # Both hub and gateway use full mode on AutoInterface.
        # AutoInterface is a symmetric peer mesh — gateway mode would proxy
        # path requests for random LAN peers, wasting LoRa airtime.

    # TCPServerInterface — hub only, if it has any WAN IP
    if role == "hub" and (env.get("tailscale_ip") or env.get("public_ip")):
        iface_name = "AgroNomi Hub TCP"
        config["interfaces"][iface_name] = {}
        config["interfaces"][iface_name]["type"] = "TCPServerInterface"
        config["interfaces"][iface_name]["mode"] = "full"
        # full, not gateway — gateways discover hub destinations via announces.
        config["interfaces"][iface_name]["listen_on"] = "0.0.0.0"
        config["interfaces"][iface_name]["port"] = "4242"
        config["interfaces"][iface_name]["discoverable"] = "yes"
        config["interfaces"][iface_name]["discovery_name"] = "AgroNomi Hub"
        config["interfaces"][iface_name]["discovery_encrypt"] = "yes"
        config["interfaces"][iface_name]["reachable_on"] = "/opt/agronomi/bin/get_reachable_ip.sh"
        config["interfaces"][iface_name]["discovery_stamp_value"] = "20"
        config["interfaces"][iface_name]["announce_interval"] = "720"
        # IFAC: per-interface authentication for TCP (WAN-facing, untrusted)
        # Prevents unauthorized connections even if discovery is disabled
        config["interfaces"][iface_name]["ifac_size"] = "3"  # 3-byte key = human-shareable passphrase

    # BackboneInterface — hub on Linux, if available (faster than TCPServerInterface)
    if role == "hub" and env.get("platform") == "linux" and (
        env.get("tailscale_ip") or env.get("public_ip")
    ):
        iface_name = "AgroNomi Hub Backbone"
        config["interfaces"][iface_name] = {}
        config["interfaces"][iface_name]["type"] = "BackboneInterface"
        config["interfaces"][iface_name]["mode"] = "full"
        # full, not gateway — gateways discover hub destinations via announces.
        config["interfaces"][iface_name]["listen_on"] = "0.0.0.0"
        config["interfaces"][iface_name]["port"] = "4242"
        config["interfaces"][iface_name]["discoverable"] = "yes"
        config["interfaces"][iface_name]["discovery_name"] = "AgroNomi Hub"
        config["interfaces"][iface_name]["discovery_encrypt"] = "yes"
        config["interfaces"][iface_name]["reachable_on"] = "/opt/agronomi/bin/get_reachable_ip.sh"
        config["interfaces"][iface_name]["discovery_stamp_value"] = "20"
        config["interfaces"][iface_name]["announce_interval"] = "720"
        # IFAC: per-interface authentication for TCP (WAN-facing, untrusted)
        config["interfaces"][iface_name]["ifac_size"] = "3"  # 3-byte key = human-shareable passphrase
        # Remove the TCPServerInterface — Backbone replaces it on Linux
        config["interfaces"].pop("AgroNomi Hub TCP", None)

    # I2PInterface — gateway fallback for strict NAT with no Tailscale
    if role == "gateway" and env.get("i2p_available") and not env.get("tailscale_ip"):
        iface_name = "I2P Tunnel"
        config["interfaces"][iface_name] = {}
        config["interfaces"][iface_name]["type"] = "I2PInterface"
        config["interfaces"][iface_name]["enabled"] = "yes"
        config["interfaces"][iface_name]["connectable"] = "no"
        config["interfaces"][iface_name]["peers"] = ""  # filled in after first LoRa discovery

    # RNodeInterface — always enable if device found
    for i, rnode_port in enumerate(env.get("rnode_devices", [])):
        iface_name = f"LoRa Radio" + (f" {i+1}" if i > 0 else "")
        config["interfaces"][iface_name] = {}
        config["interfaces"][iface_name]["type"] = "RNodeInterface"
        config["interfaces"][iface_name]["mode"] = "full"
        config["interfaces"][iface_name]["port"] = rnode_port
        config["interfaces"][iface_name]["frequency"] = "868000000"
        config["interfaces"][iface_name]["bandwidth"] = "125000"
        config["interfaces"][iface_name]["spreadingfactor"] = "11"
        config["interfaces"][iface_name]["codingrate"] = "5"
        config["interfaces"][iface_name]["txpower"] = "17"

    # ── [logging] section ──
    config["logging"] = {}
    config["logging"]["loglevel"] = "4"

    config.write()
    return config.filename
```

#### Gateway Bootstrap — The Onboarding Journey

When a new gateway is deployed at a farm location, the onboarding sequence is:

1. **Operator copies fleet identity** to the gateway: `scp /opt/agronomi/fleet_identity gateway:~/.reticulum/storage/identities/`
2. **Operator runs `agronomi-setup gateway`** (or the first boot script does it automatically):
   - `detect_rns_environment()` scans for RNode, AutoInterface, Tailscale, i2pd
   - `autoconfigure_rns(role="gateway")` generates `~/.reticulum/config`
   - `get_reachable_ip.sh` is installed at `/opt/agronomi/bin/`
   - `rnsd` is started (or `RNS.Reticulum()` is called by `ble_forwarder.py`)
3. **Gateway joins the mesh** — automatically, within seconds:
   - AutoInterface discovers hub on LAN (if same network) → instant
   - LoRa comes up → hub's announce arrives → gateway discovers `farm.telemetry_readings`
   - Hub's discovery announce arrives via LoRa → gateway auto-connects TCP to hub → fast path established
   - Gateway announces `farm.gateway_commands` with `app_data="agronomi-gateway:GW-XXXX-XX"` → hub auto-provisions `reticulum_gateways` table
4. **First telemetry arrives** → hub auto-provisions `hardware_devices` + `sensor_nodes`
5. **Commands and OTA flow** — over the fastest available path

**No manual IP configuration. No Tailscale required for LAN gateways. No port forwarding.**

#### Hub as the Path Discovery Hub

With `mode = gateway` on the hub's TCPServerInterface, the hub can **proxy path requests** for connected gateways. This is useful for multi-gateway topologies where a gateway needs to discover a path to another gateway. However, for AgroNomi's current fleet (2-5 gateways), `full` mode is sufficient — announces propagate between gateways through the hub's normal rebroadcast, and explicit path requests are rarely needed. If inter-gateway path discovery becomes necessary at scale, changing the TCPServerInterface to `gateway` mode is a single config line.

- Gateway A asks hub: "how do I reach Gateway B?"
- Hub's gateway-mode interface receives the path request → `should_search_for_unknown = True` (because `MODE_GATEWAY` is in `DISCOVER_PATHS_FOR`)
- Hub forwards the path request to all other interfaces → LoRa, TCP, AutoInterface
- If Gateway B is reachable via any of those, the hub answers Gateway A with the discovered path
- Gateway A can now communicate directly with Gateway B (no hub relay needed for data — just for path discovery)

This means gateways don't need to know about each other. The hub acts as the fleet's **path discovery service**, resolving inter-gateway connectivity on demand.

#### Transport Node Policy — Who Should Be a Transport Node?

See ["Transport Nodes vs. Instances — Who Should Route?"](#transport-nodes-vs-instances--who-should-route) above for the detailed analysis. Key point: only the hub and the best-connected gateways need `enable_transport = Yes`. As the fleet scales beyond 2-3 gateways on the same LoRa frequency, consider making additional gateways Instance-only to reduce LoRa airtime waste from announce rebroadcasts.

#### Inter-Gateway Communication

Gateways can reach each other through the hub. Two paths:

1. **Via hub path proxy**: Gateway A → path request to hub → hub discovers Gateway B → hub answers Gateway A → Gateway A connects directly to Gateway B (hub not in data path)
2. **Via LoRa direct**: If gateways are on the same LoRa frequency and in range, they discover each other directly without hub involvement

For the current AgroNomi deployment, inter-gateway communication is not a primary use case — all data flows through the hub. But the mesh topology supports it naturally.

#### Adding a New Gateway — Operator Checklist

What the operator actually does when deploying a new gateway:

| Step | Action | Automated? |
|------|--------|------------|
| 1 | Install OS + Python + RNS + AgroNomi gateway package | Partial (Ansible/scripted) |
| 2 | Copy fleet identity file to `/opt/agronomi/fleet_identity` | One-time manual or scripted |
| 3 | Connect RNode USB radio | Plug in |
| 4 | Connect Pico 2W via USB | Plug in |
| 5 | Optionally connect to LAN/WiFi or install Tailscale | Optional — LoRa works alone |
| 6 | Run `agronomi-setup gateway` or power on | ✅ Auto-detect + auto-configure |
| 7 | Gateway joins mesh within seconds | ✅ Fully automatic |
| 8 | Hub auto-registers gateway in `reticulum_gateways` table | ✅ Via announce handler |

**Steps 1-2 are the only manual steps.** Steps 3-5 are physical. Steps 6-8 are fully automatic.

#### Adding a New Hub — Operator Checklist

| Step | Action | Automated? |
|------|--------|------------|
| 1 | Install OS + Python + RNS + AgroNomi hub package | Partial (Ansible/scripted) |
| 2 | Generate or copy fleet identity to `/opt/agronomi/fleet_identity` | Manual (first hub generates, others copy) |
| 3 | Connect RNode USB radio | Plug in |
| 4 | Connect to LAN and/or install Tailscale | Optional but recommended |
| 5 | Run `agronomi-setup hub` | ✅ Auto-detect + auto-configure |
| 6 | Hub starts serving — gateways discover and auto-connect | ✅ Fully automatic |

#### Multi-Hub Deployments

For large farms with multiple hubs (e.g., different fields or regions), the architecture extends naturally:

- Each hub runs `autoconfigure_rns(role="hub")` with its own RNode and TCP server
- All hubs share the same fleet Network Identity
- Hubs connect to each other via AutoInterface (same LAN), LoRa (in range), or TCP/I2P
- If hubs are on different network segments, set the inter-hub interface to `boundary` mode to control announce propagation between fast and slow segments
- Gateways discover the nearest/best hub via announce propagation and auto-connect

```ini
# Hub B — remote site, connected to Hub A via Internet
[[Hub A Link]]
  type = BackboneInterface
  mode = boundary         # boundary on the Internet-facing side
  remote = hub-a.example.com
  target_port = 4242
```

The `boundary` mode prevents announces from the fast Internet segment from flooding into the local LoRa segment (which would waste airtime), while still allowing paths to be discovered.

#### `agronomi-setup` CLI — The Operator's Entry Point

```bash
# Initialize a new fleet (generates fleet identity + hub config)
agronomi-setup init-fleet

# Set up this machine as the hub
agronomi-setup hub

# Set up this machine as a field gateway
agronomi-setup gateway

# Re-detect environment and regenerate RNS config
agronomi-setup reconfigure

# Show current RNS status and connectivity
agronomi-setup status

# Export fleet identity for copying to a new gateway
agronomi-setup export-identity
```

`agronomi-setup` wraps `detect_rns_environment()` + `autoconfigure_rns()` + `rnsd` management + fleet identity distribution. It's the single tool operators use to deploy and maintain the fleet.

### Multi-Path Routing and Failover

RNS maintains a **single best-path per destination** model — the path table maps each destination hash to one next hop, one receiving interface, and one hop count. RNS does **not** load-balance across multiple paths or select paths by interface speed. Path selection:

1. **Lower hop count wins** — a path with fewer hops replaces an existing path with more hops
2. **Emission timebase** — equal hops → more recently emitted announce wins
3. **Unresponsive paths replaced** — failed link → path marked `UNRESPONSIVE` → equal-hop-count announce from another interface can replace it
4. **Expired paths replaced** — any valid new announce replaces an expired path entry

`Transport.prioritize_interfaces()` sorts interfaces by bitrate (highest first), but this affects the **order interfaces are processed for broadcasting**, not the path selection algorithm. The path table entry is singular — one path per destination.

**What this means for AgroNomi:** When the hub has AutoInterface + TCP + LoRa active, and a gateway is reachable via multiple interfaces, the path that gets used depends on which announce arrives first with the lowest hop count. For a directly connected gateway, the hop count is 1 regardless of interface — so the path from whichever announce is processed first wins. If that path becomes unresponsive (e.g., WiFi goes down), the gateway's next announce arriving via another interface (TCP or LoRa) will replace the unresponsive path.

**Failover scenario:** Hub's WiFi goes down → AutoInterface peer lost → path via AutoInterface becomes unresponsive → gateway's next announce arrives via LoRa or TCP → new path replaces the unresponsive one → telemetry and commands flow on the surviving interface. This is not instant — it depends on the announce interval (the hub re-announces every 30 seconds; gateways re-announce every 30 seconds).

### Link Intermittency Tolerance

RNS handles intermittent connectivity natively — critical for agricultural deployments where power, WiFi, and Internet are unreliable.

- **TCP reconnection**: `TCPClientInterface` auto-reconnects every **5 seconds** on connection loss (`RECONNECT_WAIT = 5` in `TCPInterface.py`). I2P peers reconnect every 15 seconds. Unlimited retries by default.
- **Path persistence**: Path tables are saved to disk (`destination_table`, `tunnels` files). After restart, known paths are immediately available.
- **Unresponsive detection**: If a link establishment times out (proof not received), the path is marked `UNRESPONSIVE`. Equal-hop-count announces on other interfaces can replace it.
- **AutoInterface carrier detection**: No multicast echo within 6.5s → carrier flagged lost. IPv6 address change → listener rebuilt automatically.
- **I2P tunnel resilience**: 45s user timeout, keepalive probes 10s after last write then every 9s, 5 probes, 110s read timeout, auto-reconnection. Source: `I2PInterface.py`.
- **PipeInterface respawn**: Subprocess exits → auto-respawn after configurable `respawn_delay`.

### Impact on OTA

| Transport | Firmware Transfer Time (1.4MB) | Requires Tailscale? |
|-----------|-------------------------------|-------------------|
| LoRa only (current) | ~50–90 minutes (estimated, SF11/125kHz, 2% announce cap) | ❌ No |
| AutoInterface (same LAN) | ~1–5 seconds (estimated) | ❌ No |
| TCP over Tailscale WAN | ~1–10 seconds (estimated) | ✅ Yes |
| TCP over public IP | ~5–60 seconds (estimated) | ❌ No |
| I2P tunnel | ~30–120 seconds (estimated) | ❌ No |

With auto-provisioned AutoInterface or TCP, OTA transfers become near-instant over IP. **The biggest OTA win (AutoInterface on LAN) requires zero additional infrastructure** — no Tailscale, no port forwarding, no configuration. Just plug hub and gateway into the same network and RNS discovers the fast path automatically.

LoRa remains as the always-available backup for field deployments without any IP connectivity.

### OTA Firmware Verification — From Raw Binary to Signed Releases

The current OTA system transfers raw firmware binaries via RNS Resource with a SHA-256 hash for integrity checking. This works but has a gap: the gateway has no cryptographic proof that the firmware came from the hub operator. Any RNS node that can reach the hub's `farm.commands_control` destination could inject a malicious binary.

RNS's `rngit` system (Git over Reticulum, introduced in RNS 1.2.0) solves this with **signed release manifests** (`.rsm` files). A release manifest is a self-contained, cryptographically signed document containing:
- **Release metadata**: version, description/release notes, creation timestamp, **commit hash**
- **Origin node identity and repository path**: embedded provenance for update discovery
- **Artifact list**: filenames, sizes
- **Ed25519 signatures**: one per artifact, signed by the developer's RNS Identity (public key embedded in manifest)
- **Manifest-level signature**: the entire `.rsm` is itself signed by the creator's Identity, creating a chain of trust — manifest signature proves manifest authenticity, embedded artifact signatures prove each file's integrity

Additionally, each artifact gets an **individual `.rsg` signature file** (Reticulum Signature format) containing the Ed25519 signature, the signing identity's public key, and optional metadata. These `.rsg` files allow **single-file verification** independent of the manifest, using the `rnid -V <file>` command.

**Why this matters for AgroNomi OTA:**

1. **Cryptographic verification replaces institutional trust**: Instead of trusting that the hub hasn't been compromised, the gateway verifies the firmware signature against the developer's known RNS Identity. A tampered binary fails signature verification regardless of how it was obtained.
2. **Offline verification**: The manifest and signatures can be verified without any network connection. A gateway that received firmware over LoRa can verify it before flashing, even if the LoRa link is down. Command: `rngit release <manifest>.rsm --offline`
3. **Distribution without intermediaries**: Firmware releases can traverse any RNS path — LoRa, AutoInterface, TCP, I2P, USB drive, SD card — and remain verifiable. The manifest is self-contained; it doesn't need a central server.
4. **Auditability**: Every release is attributed to a specific RNS Identity. You know exactly who signed and released each firmware version.
5. **Pinned signer verification**: `rngit release fetch <url> --signer <identity_hash>` requires that releases are signed by a specific identity. If the release wasn't signed by that identity, the fetch aborts before any files are downloaded. This is critical for AgroNomi — gateways pin the developer's known identity hash and reject any firmware not signed by it.
6. **Incremental/resume support**: `rngit release fetch` only downloads files that are missing or fail signature verification. Interrupted LoRa transfers can be resumed without re-downloading already-verified artifacts.
7. **Air-gapped signing**: `rngit release create <tag>:./dist --local` generates the `.rsm` manifest and `.rsg` signatures locally without uploading to any remote node. This allows a signing workstation that's never been on any network to produce verified releases for distribution via any medium.

**Integration path for AgroNomi:**

| Current OTA | rngit-enhanced OTA |
|-------------|-------------------|
| Hub sends firmware binary via RNS Resource | Hub publishes signed `.rsm` manifest + firmware binary via RNS Resource |
| Gateway checks SHA-256 hash | Gateway checks SHA-256 hash **and** Ed25519 signature against known developer Identity |
| Gateway flashes binary | Gateway flashes binary **only if signature verifies** |
| No attribution — any node could send firmware | Cryptographic attribution — only the developer's Identity can produce valid signatures |
| No offline verification | Manifest can be verified offline (`rngit release manifest.rsm --offline`), carried on USB, distributed over any medium |
| No identity pinning | Gateway pins developer identity hash with `--signer`, rejects firmware from unknown identities |
| No resume on interrupted transfer | `rngit release fetch` skips already-verified files, resumes interrupted downloads |

**Actual `rngit release` commands**:

| Command | Purpose |
|---------|--------|
| `rngit release create <tag>:<path>` | Create signed release from artifacts directory. Opens `$EDITOR` for release notes. |
| `rngit release create <tag>:<path> --local` | Create signed release locally without uploading (air-gapped signing) |
| `rngit release fetch <url> latest:all` | Fetch latest release, auto-verify signatures, skip valid files |
| `rngit release fetch <url> latest:all --signer <hash>` | Fetch + pin required signer identity (abort if signer doesn't match) |
| `rngit release <manifest>.rsm fetch latest:all` | Fetch update using existing manifest (verifies new manifest against old one) |
| `rngit release <manifest>.rsm --offline` | Verify all artifacts on-disk against manifest, no network needed |
| `rnid -V <file>` | Verify individual file against its `.rsg` signature |
| `rngit release list <url>` | List all releases for a repository |
| `rngit release view <url> <tag>` | View release details |
| `rngit release delete <url> <tag>` | Delete a release (requires `rel` permission) |

**Important**: Release creation includes upload (`create`), and verification is automatic during `fetch` or via the `--offline` flag.

**Signature distribution best practice**: While `.rsm` manifests include embedded `.rsg` signatures for every artifact, it's recommended to also distribute individual `.rsg` files alongside artifacts. They're lightweight and enable single-file verification with `rnid -V` without needing the full manifest — useful for gateways that only need to verify one firmware binary.

**Future `.rvp` package format**: RNS is developing an `.rvp` (Reticulum Verified Package) format that bundles releases with all artifacts, metadata, manifest, and signatures in a single archive. This is not yet available but would simplify AgroNomi OTA to a single-file transfer.

**This is a Phase 3+ enhancement**, not a Phase 1 requirement. The current SHA-256 hash verification is sufficient for the initial fleet deployment. rngit integration would be added when the fleet scales beyond trusted hardware.

### rngit Beyond OTA — Fleet Code Distribution & Collaboration

`rngit` is not just a release signing tool — it's a complete Git hosting and collaboration system over Reticulum. The following capabilities are relevant to AgroNomi's long-term fleet management:

#### Repository Hosting Over RNS

`rngit` hosts bare Git repositories accessible via `rns://DESTINATION_HASH/group/repo` URLs. The `git-remote-rns` helper (installed with RNS) makes this transparent — `git clone`, `git push`, `git pull` all work over RNS paths without any special configuration.

For AgroNomi, this means the hub can host the project's Git repository and any operator with RNS connectivity can clone/push without internet access — entirely over LoRa, AutoInterface, or TCP.

#### Mirroring & Automatic Sync

`rngit mirror <source_url> <target_url>` creates a mirror that auto-syncs on a configurable interval (default: 24 hours, set via `mirror_interval` in `rngit` config). The node checks for mirrors needing sync every 15 minutes. This enables:
- Hub mirrors AgroNomi firmware repo from GitHub over WAN, makes it available to gateways over LoRa
- Gateways in the field mirror from the hub, creating redundant code distribution paths
- Sync failures are logged but don't block future retries; sync timestamp only updates on success

Manual sync: `rngit sync rns://<hash>/group/repo`

#### Fine-Grained Permission System

`rngit` has a complete permission system with per-identity-hash access control:

| Permission | Meaning |
|-----------|---------|
| `r` (read) | Clone, fetch, view repos and work documents |
| `w` (write) | Push changes, manage work documents |
| `rw` (read/write) | Combined read and write |
| `c` (create) | Create, fork, or mirror new repos in a group |
| `s` (stats) | View repository activity statistics |
| `rel` (release) | Create and manage releases |
| `i` (interact) | Comment on and interact with work documents |
| `p` (propose) | Propose work documents without full write access |
| `adm` (admin) | Full access |

Permissions can be targeted at `all` (everyone), `none`, or a specific RNS identity hash. They're configured at three levels:
1. **Group-level**: In `rngit` config or `<group_name>.allowed` files
2. **Repository-level**: In `<repo_name>.allowed` files next to the repo
3. **Work document-level**: In `<doc_id>.allowed` files

Permissions can also be managed remotely: `rngit perms rns://<hash>/group/repo` opens an editor, and changes are transmitted over the encrypted RNS link and applied immediately. No shell access to the hosting node needed.

**Identity aliases** can be defined in `~/.rngit/config` to make permission management readable:
```ini
[aliases]
  developer = 9710b86ba12c42d1d8f30f74fe509286
  hub = d09285e660cfe27cee6d9a0beb58b7e0
```

For AgroNomi, the hub's `rngit` would configure:
- `public` group: `r:all` (any RNS peer can read)
- `firmware` group: `r:all, rel:<developer_identity_hash>` (anyone can download, only developer can create releases)
- `internal` group: `rw:<hub_identity_hash>` (only hub can push)

#### Work Documents — Cryptographically Signed Issue Tracking

`rngit work` provides issue tracking and task management over RNS with **cryptographic attribution** — every document and comment is Ed25519-signed by its author. This is more than convenience; it means:
- **Tamper-proof records**: Any modification after creation invalidates the signature
- **Author verification**: The author's identity is cryptographically verified, not just claimed
- **Offline validation**: Signatures can be verified without network access using any RNS transport node's cached keys
- **No central authority needed**: Work documents are stored as msgpack files on the `rngit` node, not in a central database

Commands: `rngit work <url> create/list/view/edit/update/complete/activate/delete/perms`

Work documents support three scopes: **active** (in-progress), **completed** (resolved), **proposed** (awaiting approval). The `propose` permission lets team members create proposals without full write access.

For AgroNomi, this could serve as:
- **Field issue tracking**: An operator in the field creates a work document on the hub's `rngit` node over LoRa, documenting a sensor malfunction. The document is cryptographically attributed to them.
- **Maintenance records**: Gateways log maintenance events as work document updates, creating an auditable, tamper-proof history.
- **Fleet coordination**: Multiple operators can interact with work documents over any RNS transport, with full permission control.

#### Nomad Network Page Node

`rngit` can serve a browseable repository interface over Nomad Network (RNS's messaging layer). Enable with:
```ini
[pages]
  serve_nomadnet = yes
```

This provides: repository browsing, release viewing/downloading, commit history, file browser, work documents, and statistics — all accessible from any Nomad Network client. Pages respect the same permission system as Git access.

For AgroNomi, this means an operator with a Nomad Network client (e.g., on a laptop connected to the hub's WiFi) can browse firmware releases, view changelogs, and trigger updates without SSH access or command-line tools.

#### `rngit` Configuration for AgroNomi Hub

A minimal hub configuration hosting firmware releases:
```ini
[rngit]
  node_name = AgroNomi Hub
  announce_interval = 360
  record_stats = yes

[repositories]
  firmware = /var/git/firmware
  internal = /var/git/internal

[access]
  firmware = r:all, rel:9710b86ba12c42d1d8f30f74fe509286
  internal = rw:9710b86ba12c42d1d8f30f74fe509286

[pages]
  serve_nomadnet = yes
  unicode_icons = yes
```

Running the node: `rngit` (foreground) or `rngit -s` (service mode with file logging).

Viewing identity info: `rngit --print-identity` outputs the Git Peer Identity, Repository Node Identity, Repositories Destination hash, and Nomad Network Destination hash.

#### Relevance to AgroNomi Implementation Phases

| Capability | Phase | Rationale |
|-----------|-------|----------|
| Signed release manifests for OTA | Phase 3+ | Current SHA-256 is sufficient for trusted hardware Phase 1 |
| `--signer` identity pinning for gateways | Phase 3+ | Requires storing developer identity hash on gateways |
| `.rsm` manifest-based OTA updates (resume, verify) | Phase 3+ | Replaces current `ota_scheduler.py` SHA-256-only path |
| Hub-hosted `rngit` for firmware distribution | Phase 3+ | Enables gateways to `rngit release fetch` over RNS |
| Work documents for field issue tracking | Phase 4+ | Nice-to-have, not critical for initial deployment |
| Nomad Network page node for operator UI | Phase 4+ | Requires Nomad Network client setup |
| Mirror sync for offline code distribution | Phase 4+ | Useful for multi-hub deployments |
| `.rvp` package format | Future | Not yet available in RNS |
| Encrypted releases | Future | API not yet implemented in RNS |

### Transport Mode: When to Enable

`enable_transport = Yes` should be set on the hub and on the best-connected gateway(s). It makes a node a **routing participant** that:

- Forwards data packets according to path tables (next-hop routing)
- Rebroadcasts announces per propagation rules (2% bandwidth cap, local traffic prioritized)
- Serves path requests — when asked "how do I reach X?", answers with cached announce or forwards the request
- Persists path tables to disk for recovery after restart
- Generates a persistent Transport Identity separate from user identities
- **Caches public keys for the entire fleet** — acts as a distributed keystore so any node can verify any other node's identity

Without transport, a node can send/receive packets and host destinations but cannot route for others or cache keys. For a small fleet (1 hub + 1-2 gateways), enabling transport on all nodes is fine. As the fleet scales beyond 3-4 gateways on the same LoRa frequency, consider making additional gateways Instance-only to reduce LoRa airtime waste from announce rebroadcasts — only the best-connected gateway(s) and the hub need to be Transport Nodes.

### Implementation Tasks — Fleet Auto-Provisioning

#### Phase 1: Single Node Auto-Config (hub + mimi)

| # | Task | Effort | Depends On |
|---|------|--------|------------|
| 1 | Implement `detect_rns_environment()` — detect IPv6 link-local, Tailscale, RNode USB, i2pd, platform | Small | — |
| 2 | Implement `autoconfigure_rns(role, env)` using `ConfigObj` — generates `~/.reticulum/config` | Medium | 1 |
| 3 | Implement `get_reachable_ip.sh` generation (Tailscale → public IP → exit 1) | Small | 2 |
| 4 | Integrate `tailscale.py`'s `get_tailscale_status()` into `detect_rns_environment()` | Small | 1 |
| 5 | Test: AutoInterface-only mesh (no Tailscale, no TCP server) — hub + mimi on same LAN | Medium | 2 |
| 6 | Test: LoRa → discovery announce → TCP auto-connect → LoRa stays as fallback | Medium | 2 |
| 7 | Test: OTA transfers complete in seconds when AutoInterface or TCP is active | Medium | 5, 6 |

#### Phase 2: Fleet Identity + Multi-Gateway

| # | Task | Effort | Depends On |
|---|------|--------|------------|
| 8 | Implement `agronomi-setup init-fleet` — generate fleet Network Identity via `rnid -g` | Small | Phase 1 |
| 9 | Implement `agronomi-setup export-identity` — copy fleet identity for distribution | Small | 8 |
| 10 | Add `network_identity`, `discover_interfaces`, `discovery_encrypt`, `required_discovery_value` to generated configs | Small | 8 |
| 11 | Implement `agronomi-setup hub` — detect + configure + start `rnsd` | Medium | 2, 8 |
| 12 | Implement `agronomi-setup gateway` — detect + configure + start `rnsd` + announce `farm.gateway_commands` | Medium | 2, 8 |
| 13 | Test: new gateway auto-joins mesh (LoRa bootstrap → TCP auto-connect → hub auto-provisions `reticulum_gateways`) | Medium | 11, 12 |
| 14 | Test: encrypted discovery — fleet identity required, non-fleet nodes cannot auto-connect | Medium | 10, 13 |
| 15 | Test: hub proxies path request between two gateways → gateways discover each other | Medium | 13 |

#### Phase 3: Hardening + Edge Cases

| # | Task | Effort | Depends On |
|---|------|--------|------------|
| 16 | Implement `agronomi-setup reconfigure` — re-detect environment, regenerate config, restart `rnsd` | Medium | 11, 12 |
| 17 | Implement `agronomi-setup status` — show RNS interfaces, path table, connected gateways | Medium | Phase 2 |
| 18 | Add `BackboneInterface` selection on Linux hubs (replaces `TCPServerInterface`) | Small | 2 |
| 19 | Add `I2PInterface` fallback for gateways behind strict NAT with no Tailscale | Medium | 2 |
| 20 | Add systemd service for `rnsd` auto-start on hub and gateway | Small | 11, 12 |
| 21 | Test: WiFi goes down → AutoInterface carrier loss → LoRa fallback → WiFi returns → AutoInterface recovers | Medium | 5 |
| 22 | Test: gateway power cycle → RNS reconnects, path table persists | Small | 13 |
| 23 | Add `ReticulumConfig` to `config/schemas.py` — app-level only (identity_path, announce_interval, command_poll_interval, OTA params) | Small | 2 |
| 24 | Generate and distribute IFAC key for hub's TCP interface — add `ifac_size` + `ifac_key` to TCPServerInterface and BackboneInterface configs | Small | Phase 2 |
| 25 | Integrate `rngit` signed release manifests into OTA pipeline — hub produces `.rsm` + `.rsg` files alongside firmware binaries | Medium | rngit installed on hub |
| 26 | Add `--signer` identity pinning to gateway OTA — gateways reject firmware not signed by developer's known RNS Identity | Small | 25 |
| 27 | Add `rngit release manifest.rsm --offline` verification to `ble_ota.py` — verify manifest + artifacts before flashing | Medium | 25 |
| 28 | Distribute `.rsg` signature files alongside firmware binaries for single-file `rnid -V` verification fallback | Small | 25 |
| 29 | Set up `rngit` node on hub — configure `[repositories]`, `[access]`, `[pages]` for firmware distribution | Medium | rngit installed on hub |
| 30 | Test: interrupted LoRa OTA → resume `rngit release fetch` skips already-verified files | Medium | 25, 27 |

## Port Checklist

| # | Task | Effort | Notes |
|---|------|--------|-------|
| 1 | Add 4 migration tables to `farm_knowledge.py` SCHEMA_SQL | Small | `hardware_devices`, `reticulum_gateways`, `actuator_commands`, `ble_link_log` |
| 2 | Refactor DB access: `sqlite3` → `DatabaseConnectionPool` | Medium | Every `get_db()` call, especially in RNS callbacks |
| 3 | Remove `_SCHEMA_DDL` from reticulum_ingest, rely on AgroNomi schema | Small | Schema is now owned by farm_knowledge.py |
| 4 | Fix `record_telemetry()` auto-provision to set `node_type` (NOT NULL) | Medium | Map `device_type`→`node_type`: `air_node→'air'`, `soil_node→'soil'`, `pump_node→'pump'`, `gh_actuator→'greenhouse'` |
| 5 | Fix `sensor_readings.unit` — `_get_unit()` must never return `''` | Small | AgroNomi schema has `unit TEXT NOT NULL` with no default. Return `'unknown'` for unmapped types. |
| 6 | Adjust `sensor_readings` INSERT for AgroNomi schema | Medium | Column `id` (not `reading_id`), include `unit` column |
| 7 | Wire `sensor_aggregator.py` to join `hardware_devices` for enriched alerts | Small | Optional enhancement |
| 8 | Add `ReticulumConfig` to `config/schemas.py` — app-level only, NOT RNS config | Small | `identity_path`, `announce_interval`, `command_poll_interval`, OTA settings |
| 9 | Test auto-provisioning with AgroNomi's existing `sensor_nodes` data | Medium | Ensure INSERT only populates known columns |
| 10 | Add `irrigate_mm` bridge between `irrigation_schedule` and `actuator_commands` | Small | Future: irrigation scheduler → command dispatcher |
| 11 | Integrate `rngit` signed release manifests into OTA pipeline | Medium | Phase 3+: hub produces `.rsm` + `.rsg`, gateways verify with `--signer` pinning |
| 12 | Set up `rngit` node on hub for firmware distribution + Nomad Network pages | Medium | Phase 3+: `[rngit]` config with `[repositories]`, `[access]`, `[pages]` |
