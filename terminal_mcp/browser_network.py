"""Per-worker HTTP proxy: validate DNS answers and connect to numeric IPs.

Chromium sends all TCP web traffic here; CONNECT tunnels retain end-to-end
TLS validation. No second hostname lookup occurs between policy and connect.
The Playwright context separately checks full URLs (including redirects).
"""
from __future__ import annotations

import select
import socket
import socketserver
import threading
from dataclasses import replace
from urllib.parse import urlsplit

from .browser_safety import UrlPolicy, UrlRejected, _resolve, validate_url


def destination(host: str, port: int, policy: UrlPolicy) -> str:
    policy = replace(policy, allow_patterns=(), deny_patterns=(), resolve_dns=False)
    authority = f"[{host}]" if ":" in host else host
    validate_url(f"http://{authority}:{port}/", policy)
    addresses = _resolve(host)
    if not addresses:
        raise UrlRejected("URL_DNS_FAILED", "hostname could not be resolved")
    for ip in addresses:
        authority = f"[{ip}]" if ip.version == 6 else str(ip)
        validate_url(f"http://{authority}:{port}/", policy)
    return str(addresses[0])


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        try:
            line = self.rfile.readline(8193)
            if len(line) > 8192:
                return
            method, target, version = line.decode('ascii').strip().split(' ')
            headers = []
            size = 0
            while True:
                row = self.rfile.readline(8193)
                size += len(row)
                if size > 32768 or not row:
                    return
                if row == b'\r\n':
                    break
                if not row.lower().startswith((b'proxy-', b'connection:')):
                    headers.append(row)
            parts = urlsplit('https://' + target if method == 'CONNECT' else target)
            if parts.scheme not in ('http', 'https') or not parts.hostname:
                return
            port = parts.port or (443 if parts.scheme == 'https' else 80)
            address = destination(parts.hostname, port, self.server.policy)
            with socket.create_connection((address, port), timeout=5) as upstream:
                if method == 'CONNECT':
                    self.connection.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                else:
                    path = parts.path or '/'
                    if parts.query:
                        path += '?' + parts.query
                    upstream.sendall(f'{method} {path} {version}\r\n'.encode('ascii')
                                     + b''.join(headers) + b'Connection: close\r\n\r\n')
                # rbufsize=0 prevents request bodies being stranded in a buffer.
                while True:
                    readable, _, _ = select.select([self.connection, upstream], [], [], 5)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is self.connection else self.connection).sendall(data)
        except (OSError, ValueError, UnicodeError):
            try:
                self.connection.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass

    rbufsize = 0


class GuardedProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, policy):
        self.policy = policy
        super().__init__(('127.0.0.1', 0), _Handler)

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=1)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_address[1]}'
