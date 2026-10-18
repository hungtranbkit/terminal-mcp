#!/usr/bin/env python3
"""Raw-REST E2E pilot for the loopback Camofox worker (deploy/camofox/README.md).

Reads the access key from CAMOFOX_ACCESS_KEY_FILE (default
~/.config/camofox/access.key) and never prints it. Exits non-zero on the
first failed check. Writes a screenshot to --out-dir when given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def call(base: str, method: str, path: str, key: str | None, body: dict | None = None,
         timeout: float = 45.0) -> tuple[int, bytes, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("CAMOFOX_URL", "http://127.0.0.1:9377"))
    parser.add_argument("--url", default="https://example.com/")
    # example.com (2026) shows "Example Domain" only in <title>; the
    # accessibility snapshot carries the body text, the tab list the title.
    parser.add_argument("--expect", default="This domain is for use in documentation")
    parser.add_argument("--expect-title", default="Example Domain")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()
    key_file = Path(os.environ.get("CAMOFOX_ACCESS_KEY_FILE",
                                   "~/.config/camofox/access.key")).expanduser()
    key = key_file.read_text().strip()
    user = f"pilot-{int(time.time())}"
    results: list[dict] = []

    def check(name: str, ok: bool, **detail: object) -> None:
        results.append({"check": name, "ok": ok, **detail})
        print(json.dumps(results[-1]))
        if not ok:
            raise SystemExit(1)

    status, body, _ = call(args.base, "GET", "/health", None)
    health = json.loads(body or b"{}")
    check("health", status == 200 and health.get("ok") is True, status=status,
          engine=health.get("engine"), memory=health.get("memory"))

    status, _, _ = call(args.base, "GET", f"/tabs?userId={user}", None)
    check("auth_missing_key_rejected", status in (401, 403), status=status)
    status, _, _ = call(args.base, "GET", f"/tabs?userId={user}", "wrong-" + key[:4])
    check("auth_wrong_key_rejected", status in (401, 403), status=status)
    status, _, _ = call(args.base, "POST", "/stop", key, {})
    check("admin_stop_disabled", status == 403, status=status)

    started = time.monotonic()
    status, body, _ = call(args.base, "POST", "/tabs", key,
                           {"userId": user, "sessionKey": "pilot", "url": args.url})
    tab = json.loads(body or b"{}")
    tab_id = tab.get("tabId") or tab.get("id")
    check("create_tab", status == 200 and bool(tab_id), status=status,
          ms=int((time.monotonic() - started) * 1000))
    try:
        status, body, _ = call(args.base, "GET", f"/tabs/{tab_id}/snapshot?userId={user}", key)
        snap = json.loads(body or b"{}")
        text = json.dumps(snap)
        check("snapshot_contains_expected", status == 200 and args.expect in text,
              status=status, url=snap.get("url"), chars=len(text))
        status, body, ctype = call(args.base, "GET", f"/tabs/{tab_id}/screenshot?userId={user}", key)
        is_png = status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n"
        if is_png and args.out_dir:
            out = Path(args.out_dir).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            (out / "camofox-example.png").write_bytes(body)
        check("screenshot_png", is_png, status=status, bytes=len(body), content_type=ctype)
        status, body, _ = call(args.base, "GET", f"/tabs?userId={user}", key)
        listed = json.loads(body or b"{}").get("tabs", [])
        titles = [t.get("title") for t in listed]
        check("list_tabs_title", status == 200 and args.expect_title in titles,
              status=status, titles=titles)
    finally:
        status, _, _ = call(args.base, "DELETE", f"/tabs/{tab_id}?userId={user}", key)
        check("close_tab", status in (200, 204), status=status)
        call(args.base, "DELETE", f"/sessions/{user}", key)
    status, body, _ = call(args.base, "GET", "/health", None)
    health = json.loads(body or b"{}")
    check("health_after", status == 200 and health.get("activeTabs", 0) == 0,
          status=status, active_tabs=health.get("activeTabs"), memory=health.get("memory"))
    print(json.dumps({"summary": "PASS", "checks": len(results)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
