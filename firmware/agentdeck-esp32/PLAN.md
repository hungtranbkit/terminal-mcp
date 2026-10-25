# Agents Deck implementation
User specification: ESP32-S3 candidate ES3C28P, offline demo and authenticated read-only live status, no production deployment.

1. Test status projection, scoped authentication, pagination and rate caps using stdlib unittest; implement standalone opt-in HTTPS adapter reading the real supervised-task SQLite database with mode=ro. Only explicitly provisioned target aliases are exposed.
2. Implement PlatformIO Arduino firmware with Adafruit ILI9341, native FT6336 register reads, six original procedural pixel avatars, grid/detail/filter/page controls, offline demo, TLS CA validation and bounded polling. No audio recording or SD writes.
3. Compile firmware; run API tests and static checks; document physical bring-up, contract, provisioning and limitations; commit and push feature branch without Actions.

Review focus: stale records, process-running without supervised execution, malformed/oversized JSON, auth denial before data access, request/page limits, millis wrap, TLS time validity, touch release/debounce.
Architecture choice: standalone API avoids coupling to dashboard admin authentication and exposes no command endpoint. Operator supplies safe labels rather than leaking raw targets. No deployment performed.
