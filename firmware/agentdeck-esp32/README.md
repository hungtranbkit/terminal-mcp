# Agents Deck ESP32 prototype

Real Arduino firmware for a standalone 240×320 portrait status display, plus an opt-in HTTPS bridge to Terminal MCP's actual supervised-task SQLite store. Boots into a fully offline six-avatar demo; LIVE requires local provisioning. No production service is modified or deployed. No hardware was attached during development: successful compilation does **not** verify panel polarity, touch orientation, flash capacity, battery behavior or physical operation.

## Candidate hardware profile

Target is **ESP32-S3 ES3C28P**, commonly sold as DIYMORE/DIY NOW/Hosyond 2.8-inch capacitive display with microphone/speaker, USB-C, SD and battery connector. This is **not ESP32-2432S028R CYD**, not resistive touch, and not an XPT2046 driver. A photo alone cannot establish the exact PCB revision. Check PCB silkscreen, module marking and vendor schematic before upload. Candidate memory configuration: 16 MB flash, 8 MB OPI PSRAM; firmware does not depend on PSRAM allocations.

| Function | GPIO |
|---|---|
| ILI9341V SPI CS / DC | 10 / 46 |
| SPI SCK / MOSI / MISO | 12 / 11 / 13 |
| LCD reset | shared main reset; driver uses -1 |
| Backlight, active high | 45 |
| FT6336G SDA / SCL | 16 / 15 |
| Touch reset / interrupt | 18 / 17 |
| Speaker amp enable, held low | 1 |

Reference pin definitions: [ngttai BSP header](https://github.com/ngttai/esp32_s3_es3c28p/blob/main/include/bsp/esp32_s3_es3c28p.h). Independent [Leshoraa Hosyond example](https://github.com/Leshoraa/2.8_ESP32-S3Display) uses the same display family. The BSP documents shared LCD reset, audio, battery and SD support. This implementation uses only display and touch; it does not record audio, drive the codec, write SD, control charging, or estimate battery percentage. Keep vendor battery/charging wiring intact. Hardware profile remains **candidate**, not physical compatibility certification.

## Build and flash

Install Python and PlatformIO Core (or PlatformIO IDE). From this directory:

```sh
pio run -e es3c28p
# Only after physically verifying the candidate board:
pio device list
pio run -e es3c28p -t upload --upload-port /dev/ttyACM0
```

The default build uses `include/config.example.h` with no credentials and starts DEMO. Platform, driver and JSON versions are pinned in `platformio.ini`; Arduino core is supplied by espressif32 6.9.0. USB CDC is enabled. If USB upload cannot connect, use the vendor BOOT+RESET procedure and select the newly enumerated port. Do not flash an existing production device without backing up and verifying its exact board. This task did not flash a device.

For LIVE, copy `include/config.example.h` to **ignored** `include/config.local.h`. Set WiFi, HTTPS endpoint, random device-only token and the issuing CA PEM. A multiline CA can be defined as a C++ raw string, e.g. `#define DECK_ROOT_CA R"PEM(-----BEGIN CERTIFICATE----- ... )PEM"` with the complete real certificate and actual newlines. The server certificate must have a SAN matching the endpoint hostname/IP. Never set an insecure TLS mode or put an admin bearer into firmware. Leave `DECK_BOOT_DEMO true` for safe offline bring-up, or set false for provisioned live boot. Config changes require rebuild/upload.

Secrets are compiled into the local binary: protect the build directory and physical device; rotate this device credential on loss. The prototype does not enable secure boot/flash encryption. Never commit or distribute provisioned binaries. No credentials, HTTP bodies or raw target IDs are logged by the client.

## Screen and controls

Portrait header: supervised **working** count labelled running / total allowlisted sessions, and DEMO/LIVE/WiFi RSSI/offline/auth/error/stale indicator. Six original symmetric pixel creatures occupy a 2-column × 3-row grid, each with operator-safe machine/session label and task status. Tap a card for detail and source age; tap detail body to return. Bottom left toggles DEMO/LIVE; middle switches ALL/WORK; right advances six-item pages, returning to page one at the end. Source refresh is 20 seconds, including navigation; the client never bursts requests on taps. A page/filter change clears the previous view until its own response arrives. In-flight old responses are discarded after navigation.

Backlight dims after 30 seconds; first touch wakes without acting. Demo disables WiFi. Live enables WiFi modem sleep, idle loop yields, and network work runs in a separate FreeRTOS task with connection/read/deadline limits. This is an always-on LCD prototype, not measured ultra-low-power hardware; battery runtime/current must be measured. No deep sleep because it must remain visible and touch responsive.

Touch uses FT6336G address 0x38, one contact, reset pulse, bounded I2C and release detection. If coordinates are mirrored/swapped, adjust the three `DECK_TOUCH_*` macros in local config after checking a physical board. Do not substitute CYD pins. Polling touch avoids relying on an unverified interrupt polarity.

## Run the real read-only Device API

`server/device_api.py` uses Python stdlib only and reads the real local database with SQLite URI `mode=ro`; it never creates/migrates the task store. It is intentionally a separate, opt-in service, with no changes to Terminal MCP's admin API or production process. It works only for supervised targets present in this database. A process merely running is **not** evidence of task execution. Unsupervised sessions are unknown; this bridge does not enumerate tmux or infer progress from output.

1. Copy `server/config.example.json` to ignored `server/config.local.json`; restrict file permissions to the service account (e.g. `chmod 600`). Set the actual database path (`XDG_DATA_HOME/terminal-mcp/direct_tasks.db` or `~/.local/share/terminal-mcp/direct_tasks.db`). The service needs read access to SQLite DB/WAL/SHM and its directory; use an account with minimal permissions. No admin API credential is needed.
2. Generate a separate random token: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. Replace the placeholder. One configuration represents **one device scope**. For multiple devices, use independent service instances/ports and credentials. Revocation is configuration replacement plus restart.
3. In `targets`, map stable public aliases to exact supervisor `target` values and safe operator-chosen machine/session labels (ASCII 1–24 characters). Maximum 120 entries. The device cannot widen this scope. Do not use private paths, prompts or sensitive session names as display labels.
4. Supply a server certificate/key issued by a trusted private or public CA; put the issuing CA into firmware. For local bring-up, create a local CA and a SAN-bearing server certificate using your normal PKI tooling. Bind to a private/VPN address reachable by the device; the example binds loopback until explicitly configured. Never publish an unauthenticated session endpoint. TLS is mandatory in this server. Do not place this behind a proxy that bypasses its bearer checks.
5. Start explicitly: `python3 server/device_api.py --config server/config.local.json`. No system service is installed automatically. It defaults to port 8443. Test with `curl --cacert /path/ca.pem -H "Authorization: Bearer $DECK_TOKEN" 'https://your-host:8443/device/v1/status?page=0&filter=all'` using a local environment variable (avoid shell history containing a literal token).

NTP must work on the device before TLS is attempted; bad clock yields a visible waiting state. CA chain and hostname verification are provided by `WiFiClientSecure.setCACert`. No redirects are followed and no HTTP fallback exists. A provisioning CA may be private; a bare self-signed leaf must be installed as its trust anchor with a matching SAN. Rebuild provisioned firmware when trust changes. Authentication failures remain visible; no fallback to demo is disguised as live.

## REST contract v1

`GET /device/v1/status?page=0&filter=all` with `Authorization: Bearer <device token>`. Only `all` and `working`; page is 0–1000. Fixed maximum six items, at most 120 scoped aliases. Unsupported methods are rejected by the HTTP server; unknown query keys/duplicates/invalid pages return 400. No session IDs or target selectors accepted from callers.

```json
{"version":1,"running":1,"total":2,"page":0,"next_page":null,"items":[
  {"id":"agent-1","machine":"Desk","session":"Builder","status":"working","age_s":4},
  {"id":"agent-2","machine":"Lab","session":"Reviewer","status":"unknown","age_s":null}
]}
```

Counts apply across the entire allowlist before filtering. `next_page` is null at end; empty pages are valid. Safe labels are the entire identity surface. No task prompts, reason strings, terminal tails, paths, command text, credentials or admin metadata are returned. Response is JSON with Content-Length and `Cache-Control: no-store`; firmware rejects bodies above 4096 bytes and malformed item data, preserving the previous good snapshot.

Task mapping: RUNNING → working; DISPATCHING/EXECUTION_ACCEPTED → pending; WAITING_INPUT/STUCK_COMPOSER → waiting; DONE/BLOCKED/FAILED/CANCELLED → corresponding lowercase state; DISPATCH_UNCERTAIN/unrecognized/no row → unknown. Newest task per exact scoped target is selected. Nonzero observation failures, an unreachable timestamp, or UNKNOWN turn observation override the task state to unknown, even if the supervisor keeps RUNNING during its outage grace period. Observations older than 90 seconds or more than 30 seconds in the future → stale. This conservative policy also makes old completed rows stale; these are current-status tiles, not history. `age_s` is task row update age, not a guarantee of machine heartbeat. No separate machine-offline inference is made from task age. Transport failure shows OFFLINE/error, and the retained client snapshot becomes STALE after 90 seconds since last success. A future adapter can add actual authenticated heartbeat evidence; never derive working from a PID.

Valid tokens are limited to **one request per 5 seconds per service** (including bad paths/query); excess → 429 with Retry-After 5. Firmware polls once per 20 seconds. Missing/wrong token → 401 before database access; source failure → 503 with a constant message. No exception/SQL/path disclosure. Single-threaded service, request socket timeout 3 seconds; deploy on a private interface with normal connection limits. API bearer tokens have no permission on admin endpoints because this service is entirely separate. Endpoint does not offer writes, command execution or public session access.

## Verification and hardware acceptance

```sh
python3 -m unittest discover -s tests -v
pio run -e es3c28p
```

Before relying on the physical prototype: verify flash/PSRAM model and pins, check portrait colours and all six cards, touch each corner and each button, dim/wake, unplug network to observe error/stale, test bad token and wrong CA rejection, page more than six scoped targets, and compare RUNNING vs EXECUTION_ACCEPTED vs WAITING_INPUT with the supervisor. Measure current at bright/dim/demo/live and battery runtime. No production flash/deploy is part of this change. See `VERIFICATION.md` for actual run evidence.
