import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location('device_api', Path(__file__).parents[1]/'server/device_api.py')
m = importlib.util.module_from_spec(spec)
if spec.loader and Path(spec.origin).exists(): spec.loader.exec_module(m)

class DeviceTests(unittest.TestCase):
    def test_supervised_state(self):
        self.assertEqual(m.status({'state':'RUNNING','updated_at':100}, 110), 'working')
        self.assertEqual(m.status({'state':'EXECUTION_ACCEPTED','updated_at':100},110),'pending')
        self.assertEqual(m.status({'state':'WAITING_INPUT','updated_at':100},110),'waiting')
        self.assertEqual(m.status({'state':'RUNNING','updated_at':100},200),'stale')
        self.assertEqual(m.status(None,110),'unknown')
    def test_auth_scope_and_rate(self):
        gate=m.Gate({'token':'x'*32,'targets':{}}, clock=lambda:10)
        self.assertFalse(gate.authorize('Bearer admin'))
        self.assertTrue(gate.authorize('Bearer '+'x'*32))
        self.assertTrue(gate.allow())
        self.assertFalse(gate.allow())
    def test_page_validation(self):
        self.assertEqual(m.page_number('0'),0)
        for value in ('-1','1001','abc','1.2'):
            with self.assertRaises(ValueError): m.page_number(value)
    def test_no_sensitive_fields(self):
        row={'state':'DONE','updated_at':100,'prompt':'SECRET','reason':'SECRET'}
        result=m.project('safe',{'machine':'Desk','session':'Builder'},row,110)
        self.assertNotIn('SECRET',str(result))
        self.assertEqual(set(result),{'id','machine','session','status','age_s'})


class DatabaseTests(unittest.TestCase):
    def test_real_database_paging_and_filter(self):
        import tempfile, sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tasks.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE direct_tasks(target TEXT,state TEXT,updated_at REAL,created_at REAL,prompt TEXT,observe_failures INTEGER DEFAULT 0,unreachable_since REAL,last_turn_state TEXT)')
                db.executemany('INSERT INTO direct_tasks(target,state,updated_at,created_at,prompt) VALUES(?,?,?,?,?)',
                    [(f'private-{i}','RUNNING' if i%2==0 else 'DONE',100,100,'NEVER EXPOSE') for i in range(8)])
                db.execute('INSERT INTO direct_tasks(target,state,updated_at,created_at,prompt) VALUES(?,?,?,?,?)',('unscoped','RUNNING',100,100,'SECRET'))
            cfg={'database':str(path),'targets':{str(i):{'target':f'private-{i}','machine':'Desk','session':f'Agent {i}'} for i in range(8)}}
            first=m.snapshot(cfg,0,False,110)
            self.assertEqual((len(first['items']),first['next_page'],first['running'],first['total']),(6,1,4,8))
            self.assertEqual(len(m.snapshot(cfg,1,False,110)['items']),2)
            self.assertEqual(len(m.snapshot(cfg,0,True,110)['items']),4)
            self.assertNotIn('private',str(first)); self.assertNotIn('NEVER EXPOSE',str(first))
            self.assertEqual(m.snapshot(cfg,1000,False,110)['items'],[])
    def test_missing_database_not_created(self):
        import tempfile,sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'missing.db'
            with self.assertRaises(sqlite3.OperationalError):
                m.snapshot({'database':str(path),'targets':{}},0,False)
            self.assertFalse(path.exists())
    def test_validation(self):
        for cfg in ({'token':'short','targets':{}}, {'token':'x'*32,'targets':{'id':{'machine':'bad\nlabel','session':'Agent','target':'t'}}}):
            with self.assertRaises(ValueError): m.validate(cfg)

class HTTPTests(unittest.TestCase):
    def test_http_auth_rate_and_no_disclosure(self):
        import http.client,threading
        from http.server import HTTPServer
        server=HTTPServer(('127.0.0.1',0),m.handler({'token':'x'*32,'database':'/missing/private.db','targets':{}}))
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        def get(path,token=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            conn.request('GET',path,headers={'Authorization':token} if token else {})
            response=conn.getresponse(); result=(response.status,response.read()); conn.close(); return result
        try:
            self.assertEqual(get('/device/v1/status')[0],401)
            self.assertEqual(get('/device/v1/status','Bearer admin')[0],401)
            code,body=get('/device/v1/status','Bearer '+'x'*32)
            self.assertEqual(code,503); self.assertNotIn(b'private',body)
            self.assertEqual(get('/device/v1/status','Bearer '+'x'*32)[0],429)
        finally: server.shutdown(); server.server_close(); thread.join()

class ProvisioningTests(unittest.TestCase):
    def test_example_token_is_not_accepted(self):
        import json
        cfg=json.loads((Path(__file__).parents[1]/'server/config.example.json').read_text())
        with self.assertRaises(ValueError): m.validate(cfg)
    def test_non_ascii_auth_is_denied(self):
        self.assertFalse(m.Gate({'token':'x'*32}).authorize('Bearer \u2603'))

class ObservationTests(unittest.TestCase):
    def test_unobservable_running_is_unknown(self):
        for extra in ({'observe_failures':1},{'unreachable_since':105},{'last_turn_state':'UNKNOWN'}):
            row={'state':'RUNNING','updated_at':110,**extra}
            self.assertEqual(m.status(row,110),'unknown')

class TLSTests(unittest.TestCase):
    def test_verified_https_and_stalled_handshake(self):
        import http.client, socket, ssl, subprocess, tempfile, threading, time
        with tempfile.TemporaryDirectory() as tmp:
            cert,key=Path(tmp)/'server.pem',Path(tmp)/'server.key'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                            '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost',
                            '-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert,key)
            server=m.TLSHTTPServer(('127.0.0.1',0),m.handler({'token':'x'*32,'database':'/missing','targets':{}}),context)
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            stalled=socket.create_connection(('127.0.0.1',server.server_port),timeout=2)
            try:
                started=time.monotonic()
                conn=http.client.HTTPSConnection('localhost',server.server_port,timeout=6,context=ssl.create_default_context(cafile=str(cert)))
                conn.request('GET','/device/v1/status')
                response=conn.getresponse(); self.assertEqual(response.status,401); response.read(); conn.close()
                self.assertLess(time.monotonic()-started,6)
                bad=http.client.HTTPSConnection('localhost',server.server_port,timeout=3,context=ssl.create_default_context())
                try:
                    with self.assertRaises(ssl.SSLCertVerificationError): bad.request('GET','/device/v1/status')
                finally: bad.close()
            finally:
                stalled.close(); server.shutdown(); server.server_close(); thread.join()

if __name__=='__main__': unittest.main()
