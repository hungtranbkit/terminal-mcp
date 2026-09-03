# Vendored: xterm.js

Unmodified UMD/browser builds, vendored locally (not loaded from a CDN)
because this project's Content-Security-Policy is `script-src 'self'` --
see `terminal_mcp/logging_setup.py`'s `SecurityHeadersMiddleware`. Served
same-origin by `terminal_mcp/webterm_assets.py` under
`/dashboard/assets/*` and `/app/assets/*`.

- `xterm.js` -- xterm@5.3.0, `lib/xterm.js` (MIT License, https://github.com/xtermjs/xterm.js)
- `xterm.css` -- xterm@5.3.0, `css/xterm.css`
- `xterm-addon-fit.js` -- xterm-addon-fit@0.8.0, `lib/xterm-addon-fit.js` (MIT License)

To upgrade: download the same three files from a newer pinned version and
replace them here -- these are the project's only copies, nothing else
references a CDN URL for xterm.
