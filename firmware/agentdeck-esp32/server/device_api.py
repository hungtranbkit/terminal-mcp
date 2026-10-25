"""Opt-in, scoped, read-only HTTPS bridge to Terminal MCP supervised tasks."""
import argparse
import hmac
import json
import re
import sqlite3
import ssl
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, parse_qs


def status(row, now):
    if row is None:
        return 'unknown'
    row = dict(row)
    if row.get('observe_failures', 0) or row.get('unreachable_since') or row.get('last_turn_state') == 'UNKNOWN':
        return 'unknown'
    age = now - row['updated_at']
    if age < -30 or age > 90:
        return 'stale'
    return {'RUNNING':'working','EXECUTION_ACCEPTED':'pending','DISPATCHING':'pending',
            'WAITING_INPUT':'waiting','STUCK_COMPOSER':'waiting','DISPATCH_UNCERTAIN':'unknown',
            'DONE':'done','BLOCKED':'blocked','FAILED':'failed','CANCELLED':'cancelled'}.get(row['state'],'unknown')


def project(alias, labels, row, now):
    return dict(id=alias, machine=labels['machine'], session=labels['session'],
                status=status(row, now), age_s=max(0,int(now-row['updated_at'])) if row else None)


def page_number(value):
    if not re.fullmatch(r'[0-9]{1,4}',value) or int(value)>1000:
        raise ValueError('page')
    return int(value)


class Gate:
    def __init__(self, config, clock=time.monotonic):
        self.config, self.clock, self.last = config, clock, float('-inf')
    def authorize(self, header):
        return hmac.compare_digest(header.encode('utf-8'), ('Bearer '+self.config['token']).encode('utf-8'))
    def allow(self):
        now=self.clock()
        if now-self.last < 5: return False
        self.last=now
        return True


def snapshot(config, page, working, now=None):
    now=time.time() if now is None else now
    items=[]
    # URI mode=ro never creates/migrates a database or persists prompts.
    uri=Path(config['database']).resolve().as_uri()+'?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=1) as db:
        db.row_factory=sqlite3.Row
        for alias, labels in config['targets'].items():
            row=db.execute('SELECT state, updated_at, observe_failures, unreachable_since, last_turn_state FROM direct_tasks WHERE target=? ORDER BY created_at DESC LIMIT 1',
                           (labels['target'],)).fetchone()
            items.append(project(alias,labels,row,now))
    running=sum(x['status']=='working' for x in items)
    total=len(items)
    if working: items=[x for x in items if x['status']=='working']
    start=page*6
    return dict(version=1, running=running, total=total, page=page,
                next_page=page+1 if start+6<len(items) else None, items=items[start:start+6])


def validate(config):
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', config['token']) or config['token'].startswith('REPLACE_') or len(config['targets'])>120:
        raise ValueError('Use a random device-only token >=32 characters; maximum 120 targets')
    for alias, labels in config['targets'].items():
        for value in (alias, labels['machine'],labels['session']):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9 _.-]{1,24}',value):
                raise ValueError('Display aliases must be safe ASCII labels, 1..24 characters')
        if not isinstance(labels['target'],str) or not labels['target']:
            raise ValueError('Missing exact scoped target')


def handler(config):
    gate=Gate(config)
    class Handler(BaseHTTPRequestHandler):
        timeout=3
        def log_message(self,*args): pass  # Never log Authorization or targets.
        def reply(self,code,payload):
            raw=json.dumps(payload,separators=(',',':')).encode()
            self.send_response(code)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            if code==429: self.send_header('Retry-After','5')
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if not gate.authorize(self.headers.get('Authorization','')):
                return self.reply(401,{'error':'unauthorized'})
            if not gate.allow(): return self.reply(429,{'error':'rate_limit'})
            parts=urlsplit(self.path)
            if parts.path!='/device/v1/status': return self.reply(404,{'error':'not_found'})
            try:
                query=parse_qs(parts.query,strict_parsing=True)
                if set(query)-{'page','filter'} or any(len(v)!=1 for v in query.values()): raise ValueError()
                page=page_number(query.get('page',['0'])[0])
                filt=query.get('filter',['all'])[0]
                if filt not in ('all','working'): raise ValueError()
            except ValueError:
                return self.reply(400,{'error':'invalid_query'})
            try: payload=snapshot(config,page,filt=='working')
            except (sqlite3.Error,OSError): return self.reply(503,{'error':'source_unavailable'})
            self.reply(200,payload)
    return Handler


class TLSHTTPServer(HTTPServer):
    def __init__(self, address, request_handler, context):
        self.context = context
        super().__init__(address, request_handler)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(3)  # Includes the pre-HTTP TLS handshake.
        try:
            return self.context.wrap_socket(sock, server_side=True), address
        except Exception:
            sock.close()
            raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    config=json.loads(Path(args.config).read_text())
    validate(config)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config['certificate'],config['private_key'])
    server=TLSHTTPServer((config.get('bind','127.0.0.1'),config.get('port',8443)),handler(config),context)
    server.serve_forever()

if __name__=='__main__': main()
