"""Static assets for the web terminal feature (xterm.js), read once at
import time from terminal_mcp/vendor/xterm/ -- see that directory's own
README.md for exactly what's vendored and why (this project's CSP is
`script-src 'self'`, so these are served same-origin rather than from a
CDN; see logging_setup.py's SecurityHeadersMiddleware).

Both dashboard.py (/dashboard/assets/*) and webauth_dashboard.py
(/app/assets/*) register their own GET route for each of these -- same
tunnel-prefix-isolation reason every other route in this project is
duplicated per dashboard variant (see webauth_dashboard.py's own module
docstring) -- but they all serve these exact same bytes, read from disk
exactly once, so there is exactly one copy of xterm.js in memory and
exactly one place its content is decided.
"""
from __future__ import annotations

from pathlib import Path

_VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "xterm"

# (path segment under /assets/, on-disk filename, content-type) -- the
# only three files this feature ever serves; nothing here is derived from
# a request, so there is no path-traversal surface even in principle.
_ASSET_SPECS = (
    ("xterm.js", "xterm.js", "application/javascript; charset=utf-8"),
    ("xterm-addon-fit.js", "xterm-addon-fit.js", "application/javascript; charset=utf-8"),
    ("xterm.css", "xterm.css", "text/css; charset=utf-8"),
)

# name -> (bytes, content_type). Read once at import time -- these are
# static, versioned-by-file vendored assets, never regenerated at
# runtime, so there is nothing to gain from re-reading the file on every
# request and a real (if small) cost in doing so.
ASSETS: dict[str, tuple[bytes, str]] = {
    name: ((_VENDOR_DIR / filename).read_bytes(), content_type)
    for name, filename, content_type in _ASSET_SPECS
}
