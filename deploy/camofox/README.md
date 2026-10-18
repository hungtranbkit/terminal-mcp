# Camofox worker on m910 (pilot runbook)

Upstream: https://github.com/jo-inc/camofox-browser, **v1.18.1**, commit
`39c82094013480b373df6600d44c7f036f58356e`, MIT license (Copyright 2025 Jo, Inc).
Browser binary: Camoufox `152.0.4-beta.30` (pinned by upstream
`lib/camoufox-download.js`). The downloaded `camoufox-bin` sha256 recorded on
2026-10-19 is `955c053a91494bcdd4ac4b9df03f25fdb99d985a0feb8f814b39289a2b54cdaa`.
Upstream does not verify a checksum itself, so compare against this hash on reinstall.

Runtime deps (`npm ci --omit=dev`, 173 packages): `camoufox-js@0.11.5`,
`playwright-core`, `express@5`, `better-sqlite3@13.0.1` (bundled prebuild; used by
camoufox-js WebGL sampling), `prom-client`, `swagger-jsdoc`. Node >= 22 (m910 runs 24.22).

## Why systemd --user, not Docker

The m910 service account is not in the `docker` group and has no sudo, so the
Docker socket is unusable without an owner change. The unit below gives the same
controls: cgroup memory/CPU/task limits, restart-on-failure, and loopback bind.

## Install / upgrade

```bash
mkdir -p ~/opt/camofox && cd ~/opt/camofox
git clone --branch v1.18.1 --depth 1 https://github.com/jo-inc/camofox-browser.git v1.18.1
cd v1.18.1 && test "$(git rev-parse HEAD)" = 39c82094013480b373df6600d44c7f036f58356e
npm ci --omit=dev --ignore-scripts --no-audit --no-fund
node lib/camoufox-download.js              # browser into ~/.cache/camoufox
sha256sum ~/.cache/camoufox/camoufox-bin   # compare with the hash above
# Disable bundled plugins we do not use (youtube transcript, persistence, vnc):
python3 - <<'PY'
import json; p='camofox.config.json'; d=json.load(open(p))
for k in ('youtube','persistence','vnc'): d['plugins'][k]['enabled']=False
json.dump(d,open(p,'w'),indent=2)
PY
ln -sfn ~/opt/camofox/v1.18.1 ~/opt/camofox/current
```

Secret (never committed, never echoed):

```bash
umask 077; mkdir -p ~/.config/camofox ~/.local/state/camofox
python3 -c "import secrets;print(secrets.token_urlsafe(32))" > ~/.config/camofox/access.key
printf 'CAMOFOX_ACCESS_KEY=%s\n' "$(cat ~/.config/camofox/access.key)" > ~/.config/camofox/camofox.env
chmod 600 ~/.config/camofox/access.key ~/.config/camofox/camofox.env
```

Unit:

```bash
cp deploy/camofox/camofox-browser.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start camofox-browser
# Optional, only after the owner approves running it at boot:
# systemctl --user enable camofox-browser
```

## Settings enforced by the unit

`CAMOFOX_BIND_HOST=127.0.0.1`, port 9377, `NODE_ENV=production`,
`CAMOFOX_CRASH_REPORT_ENABLED=false` (upstream defaults to sending crash reports),
`SENTRY_DSN=` empty, `PROMETHEUS_ENABLED=false`, `ENABLE_VNC=0`, no proxy,
`CAMOFOX_API_KEY`/`CAMOFOX_ADMIN_KEY` unset, `MAX_SESSIONS=2`,
`MAX_TABS_PER_SESSION=2`, `MAX_TABS_GLOBAL=4`, `MAX_CONCURRENT_PER_USER=1`,
navigate 20 s, handler 30 s, tab idle 3 min, session idle 10 min, browser idle stop
2 min, browser RSS restart at 900 MB. Cgroup limits: `MemoryHigh=1100M`,
`MemoryMax=1400M`, `MemorySwapMax=256M`, `CPUQuota=150%`, `TasksMax=512`.

## Verify

```bash
ss -ltn | grep 9377                        # must show 127.0.0.1:9377 only
curl -s http://127.0.0.1:9377/health
python3 -I deploy/camofox/pilot_e2e.py --out-dir /tmp/camofox-evidence
```

## Rollback / uninstall

```bash
systemctl --user disable --now camofox-browser
rm ~/.config/systemd/user/camofox-browser.service && systemctl --user daemon-reload
# optional cleanup:
rm -rf ~/opt/camofox ~/.cache/camoufox ~/.local/state/camofox ~/.config/camofox
```

Terminal MCP and PropertyIQ keep working when the worker is stopped: both
integrations are off by default and report `CAMOFOX_UNAVAILABLE` /
`worker_unavailable` when enabled against a stopped worker.

## Memory note (2026-10-19)

m910 had about 1.7–3 GB available RAM and swap was already about 99% used before the
install. The worker idles at about 150 MB, peaked at 779 MB during the pilot, and
stops its browser after 2 idle minutes. It is started but **not enabled at boot**.
Enable it only once the host has more headroom or the owner accepts the cost.
