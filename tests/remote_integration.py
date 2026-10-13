#!/usr/bin/env python3
"""Actual Nginx TLS entry, certificate checks, ingress guards and failure recovery."""
import hashlib, http.client, http.cookies, importlib.util, json, os, pathlib, signal, socket, ssl, subprocess, time, unittest
import media_integration as media
NGINX=os.environ.get('LANVIDEO_NGINX', '/usr/sbin/nginx' if pathlib.Path('/usr/sbin/nginx').exists() else '/workspace/toolchain/sysroot/usr/sbin/nginx')
spec=importlib.util.spec_from_file_location('remote_config',pathlib.Path(__file__).resolve().parents[1]/'scripts/configure-remote.py')
config=importlib.util.module_from_spec(spec); spec.loader.exec_module(config)

def free_port():
    with socket.socket() as sock: sock.bind(('127.0.0.1',0)); return sock.getsockname()[1]

class RemoteFixture:
    def __init__(self,video,trust=True,hostname='127.0.0.1'):
        self.f=media.Fixture(video); self.process=None; self.logfile=None
        try:
            self.f.server.stop(); self.port=free_port(); self.base=f'https://127.0.0.1:{self.port}'
            self.cert=self.f.data/'tls.pem'; self.key=self.f.data/'tls.key'
            # A generated, isolated test-only trust anchor; real TLS verification stays enabled.
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(self.key),'-out',str(self.cert),'-days','1','-subj','/CN='+hostname,'-addext','subjectAltName=IP:'+hostname],check=True,capture_output=True,timeout=15)
            self.key.chmod(0o600)
            self.context=ssl.create_default_context(cafile=str(self.cert)); self.tls_hostname=hostname
            self.f.server=media.auth.Server(self.f.data,LANVIDEO_REMOTE_MODE='reverse_proxy',LANVIDEO_PUBLIC_ORIGIN=self.base,LANVIDEO_REMOTE_CA=str(self.cert) if trust else '')
            self.conf=self.f.data/'nginx.conf'
            config.configure(self.f.data,self.conf,self.base,self.cert,self.key,self.f.server.port,'127.0.0.1')
            self.start_proxy()
            result,headers=self.call('POST','/api/login',{'username':'media_administrator','password':self.f.password})
            cookie=http.cookies.SimpleCookie(headers['Set-Cookie'])['lv_session']
            self.admin={'token':cookie.value,'csrf':result['csrf_token'],'user':result['user']}; self.admin_cookie=cookie
        except BaseException:
            self.close(); raise
    def start_proxy(self):
        self.logfile=(self.f.data/'nginx.log').open('a')
        checked=subprocess.run([NGINX,'-t','-p',str(self.f.data)+'/', '-c',str(self.conf)],capture_output=True,text=True,timeout=10)
        if checked.returncode:
            # Protect key even when a parser reports a bad config line.
            key=(self.f.data/'ingress.key').read_text()
            raise AssertionError('Nginx validation failed: '+checked.stderr.replace(key,'[redacted]')[-1800:])
        self.process=subprocess.Popen([NGINX,'-p',str(self.f.data)+'/', '-c',str(self.conf)],stdout=self.logfile,stderr=self.logfile)
        for _ in range(100):
            try:
                code,_,_=self.raw('GET','/api/health')
                if code==200: return
            except OSError: pass
            if self.process.poll() is not None: raise AssertionError('Nginx startup failed: '+(self.f.data/'nginx.log').read_text()[-800:])
            time.sleep(.05)
        raise AssertionError('TLS proxy readiness timed out')
    def stop_proxy(self):
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM); self.process.wait(timeout=10)
        if self.logfile: self.logfile.close()
    def close(self):
        self.stop_proxy(); self.f.close()
    def raw(self,method,path,body=None,session=None,headers=None):
        values={}; payload=body
        if isinstance(body,dict): values['Content-Type']='application/json'; payload=json.dumps(body).encode()
        if session: values.update({'Cookie':'lv_session='+session['token'],'X-CSRF-Token':session['csrf']})
        values.update(headers or {})
        connection=http.client.HTTPSConnection(self.tls_hostname,self.port,context=self.context,timeout=10)
        # Negative hostname fixture: verified TLS names 127.0.0.2, TCP remains local.
        # The application's independent probe uses configured origin 127.0.0.1 and must fail.
        connection._create_connection=lambda *args,**kwargs: socket.create_connection(('127.0.0.1',self.port),timeout=10)
        try:
            connection.request(method,path,payload,values); response=connection.getresponse()
            return response.status,response.read(),dict(response.headers)
        finally: connection.close()
    def call(self,method,path,body=None,session=None,expected=200,headers=None):
        code,content,received=self.raw(method,path,body,session,headers); result=json.loads(content)
        if code!=expected: raise AssertionError(f'{method} {path.split("?")[0]} expected {expected}, got {code}: {result.get("error","unknown")}')
        return result,received
    def direct(self,path,headers=None,method='GET',body=None):
        return self.f.binary(method,path,payload=json.dumps(body).encode() if body is not None else None,headers=headers)
    def internal_headers(self):
        return {'X-Lanvideo-Ingress':(self.f.data/'ingress.key').read_text(),'X-Forwarded-Proto':'https','X-Forwarded-Host':self.base[8:],'X-Real-IP':'127.0.0.1','Cookie':'lv_session='+self.admin['token'],'X-CSRF-Token':self.admin['csrf'],'Content-Type':'application/json'}

class RemoteIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.temp=tempfile.TemporaryDirectory(); cls.video=pathlib.Path(cls.temp.name)/'sample.mp4'; media.generate_video(cls.video)
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()
    def fixture(self,**options):
        result=RemoteFixture(self.video,**options); self.addCleanup(result.close); return result
    def test_01_tls_cookie_hsts_and_live_range_bytes(self):
        f=self.fixture(); self.assertTrue(f.admin_cookie['secure']); self.assertTrue(f.admin_cookie['httponly'])
        code,content,headers=f.raw('GET','/media/'+str(f.f.item['id']),session=f.admin,headers={'Range':'bytes=13-5001'})
        self.assertEqual(code,206); self.assertEqual(content,f.f.bytes[13:5002]); self.assertIn('max-age=',headers['Strict-Transport-Security'])
        context=ssl.create_default_context()
        with self.assertRaises(ssl.SSLCertVerificationError):
            connection=http.client.HTTPSConnection('127.0.0.1',f.port,context=context,timeout=5)
            try: connection.request('GET','/api/health')
            finally: connection.close()
    def test_02_ingress_spoof_duplicate_invalid_range_and_upload_guard(self):
        f=self.fixture()
        for path in ['/api/media','/media/'+str(f.f.item['id']),'/share/api/info','/share/media','/']:
            self.assertEqual(f.direct(path)[0],403)
            self.assertEqual(f.direct(path,{'X-Forwarded-Proto':'https','X-Real-IP':'8.8.8.8'})[0],403)
        self.assertEqual(f.direct('/share/media',{'Range':'nonsense','X-Forwarded-Proto':'https'})[0],403)
        self.assertEqual(f.direct('/media/'+str(f.f.item['id']),{'Range':'nonsense'})[0],403)
        self.assertEqual(f.direct('/api/media/upload?filename=x.mp4',{'Content-Type':'application/octet-stream'},method='POST',body={})[0],403)
        connection=http.client.HTTPConnection('127.0.0.1',f.f.server.port,timeout=5)
        try:
            connection.putrequest('GET','/api/media')
            for key,value in f.internal_headers().items(): connection.putheader(key,value)
            connection.putheader('X-Forwarded-Proto','https'); connection.endheaders()
            result=connection.getresponse(); self.assertEqual(result.status,403); result.read()
        finally: connection.close()
    def test_03_proxy_overwrites_untrusted_headers_and_plain_http_rejected(self):
        f=self.fixture()
        result,_=f.call('GET','/api/remote',session=f.admin,headers={'X-Lanvideo-Ingress':'attacker','X-Forwarded-Proto':'http','X-Forwarded-Host':'evil.example','X-Real-IP':'invalid'})
        self.assertEqual(result['adapter'],'reverse_proxy'); self.assertFalse(result['cross_nat_verified'])
        connection=http.client.HTTPConnection('127.0.0.1',f.port,timeout=5)
        try:
            connection.request('GET','/share'); response=connection.getresponse(); self.assertEqual(response.status,400); response.read()
        finally: connection.close()
    def test_04_real_certificate_verified_entry_probe(self):
        f=self.fixture(); result,_=f.call('POST','/api/remote/test',{},f.admin)
        self.assertEqual(result['state'],'reachable'); self.assertTrue(result['reachable'])
        f.call('POST','/api/remote/test',{},f.admin,expected=429)
        result,_=f.call('GET','/api/remote',session=f.admin); self.assertEqual(result['last_test']['state'],'reachable')
        f.call('POST','/api/remote/test',{'target':'https://evil.example'},f.admin,expected=400)
    def test_05_untrusted_certificate_is_rejected_by_cpp(self):
        f=self.fixture(trust=False); result,_=f.call('POST','/api/remote/test',{},f.admin)
        self.assertEqual(result['state'],'tls_error'); self.assertFalse(result['reachable'])
    def test_06_certificate_hostname_is_rejected_by_cpp(self):
        f=self.fixture(hostname='127.0.0.2'); result,_=f.call('POST','/api/remote/test',{},f.admin)
        self.assertEqual(result['state'],'tls_error'); self.assertFalse(result['reachable'])
    def test_07_proxy_failure_closed_recovery_and_explicit_local_fallback(self):
        f=self.fixture(); f.stop_proxy()
        with self.assertRaises(OSError): f.raw('GET','/api/media',session=f.admin)
        headers=f.internal_headers(); started=time.monotonic()
        code,content,_=f.direct('/api/remote/test',headers,method='POST',body={})
        self.assertEqual(code,200); self.assertEqual(json.loads(content)['state'],'connection_error'); self.assertLess(time.monotonic()-started,6)
        self.assertEqual(f.direct('/api/media')[0],403)
        f.start_proxy(); f.call('GET','/api/media',session=f.admin)
        # Explicit admin restart chooses local-only mode; no automatic insecure redirect.
        f.stop_proxy(); f.f.server.stop(); f.f.server=media.auth.Server(f.f.data)
        self.assertEqual(f.f.call('GET','/api/media',session=f.f.admin)[0]['total'],1)
        self.assertEqual(f.f.call('GET','/api/remote',session=f.f.admin)[0]['adapter'],'local')
    def test_08_short_share_https_scope_revoke_and_origin(self):
        f=self.fixture()
        share,_=f.call('POST','/api/shares',{'media_id':f.f.item['id'],'ttl_seconds':60,'scope':'video_read'},f.admin,expected=201)
        self.assertTrue(share['url'].startswith(f.base+'/share#')); token=share['url'].split('#')[1]
        f.call('POST','/share/api/exchange',{'token':token},headers={'Origin':'https://evil.example'},expected=403)
        _,headers=f.call('POST','/share/api/exchange',{'token':token},headers={'Origin':f.base})
        cookie=http.cookies.SimpleCookie(headers['Set-Cookie'])['lv_share']; self.assertTrue(cookie['secure'])
        code,content,_=f.raw('GET','/share/media',headers={'Cookie':'lv_share='+cookie.value,'Range':'bytes=9-500'})
        self.assertEqual(code,206); self.assertEqual(content,f.f.bytes[9:501])
        self.assertEqual(f.raw('GET','/api/media',headers={'Cookie':'lv_share='+cookie.value})[0],401)
        f.call('DELETE',f'/api/shares/{share["id"]}',session=f.admin)
        self.assertEqual(f.raw('GET','/share/media',headers={'Cookie':'lv_share='+cookie.value})[0],404)
        for path in [f.f.server.logpath,f.f.data/'nginx.log']:
            text=path.read_text(); self.assertNotIn(token,text); self.assertNotIn(cookie.value,text); self.assertNotIn((f.f.data/'ingress.key').read_text(),text)
    def test_09_configuration_private_and_repeatable_without_overwrite(self):
        f=self.fixture()
        self.assertEqual(f.conf.stat().st_mode&0o777,0o600); self.assertEqual((f.f.data/'ingress.key').stat().st_mode&0o777,0o600)
        original=f.conf.read_bytes()
        with self.assertRaises(FileExistsError): config.configure(f.f.data,f.conf,f.base,f.cert,f.key,f.f.server.port,'127.0.0.1')
        self.assertEqual(original,f.conf.read_bytes())
        alternate=f.f.data/'nginx-alt.conf'
        result=subprocess.run([media.auth.sys.executable,str(pathlib.Path(config.__file__)),'--data',str(f.f.data),'--output',str(alternate),'--origin',f.base,'--cert',str(f.cert),'--key',str(f.key),'--upstream-port',str(f.f.server.port),'--listen','127.0.0.1'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0); self.assertNotIn((f.f.data/'ingress.key').read_text(),result.stdout+result.stderr)
        checked=subprocess.run([NGINX,'-t','-p',str(f.f.data)+'/', '-c',str(alternate)],capture_output=True,timeout=10)
        self.assertEqual(checked.returncode,0)
        for value in ['http://example.com','https://evil.example/path','https://x:65536','https://x:0','https://../x']:
            with self.assertRaises(ValueError): config.origin(value)
    def test_10_invalid_reverse_proxy_configuration_fails_startup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as data:
            for values in [{'LANVIDEO_REMOTE_MODE':'unknown'},{'LANVIDEO_REMOTE_MODE':'reverse_proxy'},{'LANVIDEO_REMOTE_MODE':'reverse_proxy','LANVIDEO_PUBLIC_ORIGIN':'https://example.com','LANVIDEO_BIND':'0.0.0.0'},{'LANVIDEO_PUBLIC_ORIGIN':'https://x:65536'},{'LANVIDEO_REMOTE_CA':'relative'}]:
                result=subprocess.run([media.BINARY],env=media.auth.environment(data,**values),capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,1); self.assertEqual(json.loads(result.stdout)['event'],'startup_failure')
    def test_11_actual_wrong_service_and_redirect_not_followed(self):
        import http.server, threading, urllib.parse
        for redirect in [False,True]:
            f=self.fixture(); headers=f.internal_headers(); f.stop_proxy(); observations=[]
            class ForeignEntry(http.server.BaseHTTPRequestHandler):
                def log_message(self,*args): pass
                def do_GET(self):
                    observations.append({'authorization':self.headers.get('Authorization'),'cookie':self.headers.get('Cookie')})
                    if redirect:
                        self.send_response(302); self.send_header('Location','https://127.0.0.1:1/never'); payload=b'{}'
                    else:
                        nonce=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)['nonce'][0]
                        self.send_response(200); payload=json.dumps({'nonce':nonce,'instance':'another_application'}).encode()
                    self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
            foreign=http.server.ThreadingHTTPServer(('127.0.0.1',f.port),ForeignEntry)
            tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(f.cert,f.key); foreign.socket=tls.wrap_socket(foreign.socket,server_side=True)
            thread=threading.Thread(target=foreign.serve_forever,daemon=True); thread.start()
            try:
                code,content,_=f.direct('/api/remote/test',headers,method='POST',body={})
                self.assertEqual(code,200); self.assertEqual(json.loads(content)['state'],'entry_mismatch')
                self.assertEqual(observations,[{'authorization':None,'cookie':None}])
            finally: foreign.shutdown(); foreign.server_close(); thread.join(timeout=5)
    def test_12_remote_admin_permissions_csrf_and_key_file_guard(self):
        f=self.fixture(); f.call('GET','/api/remote',expected=401)
        f.call('POST','/api/remote/test',{},session=f.admin,headers={'X-CSRF-Token':'bad'},expected=403)
        password=__import__('secrets').token_urlsafe(24)
        f.call('POST','/api/users',{'username':'remote_viewer','password':password},f.admin,expected=201)
        result,headers=f.call('POST','/api/login',{'username':'remote_viewer','password':password})
        user={'token':http.cookies.SimpleCookie(headers['Set-Cookie'])['lv_session'].value,'csrf':result['csrf_token']}
        f.call('GET','/api/remote',session=user,expected=403); f.call('GET','/api/shares',session=user,expected=403)
        f.stop_proxy(); f.f.server.stop(); (f.f.data/'ingress.key').chmod(0o644)
        result=subprocess.run([media.BINARY],env=media.auth.environment(f.f.data,LANVIDEO_REMOTE_MODE='reverse_proxy',LANVIDEO_PUBLIC_ORIGIN=f.base),capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,1); self.assertIn('private 0600',json.loads(result.stdout)['fields']['reason'])

    def test_13_trickling_tls_headers_have_total_probe_deadline(self):
        import socketserver, threading
        f=self.fixture(); headers=f.internal_headers(); f.stop_proxy()
        class SlowTLS(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.settimeout(8); self.request.recv(4096)
                    for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n':
                        self.request.sendall(bytes([byte])); time.sleep(.2)
                except OSError: pass
        class ProbeServer(socketserver.ThreadingTCPServer):
            allow_reuse_address=True
            daemon_threads=True
        server=ProbeServer(('127.0.0.1',f.port),SlowTLS)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(f.cert,f.key)
        server.socket=context.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever); thread.start()
        self.addCleanup(thread.join,5); self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        started=time.monotonic()
        code,content,_=f.direct('/api/remote/test',headers,method='POST',body={})
        self.assertEqual(code,200); self.assertEqual(json.loads(content)['state'],'connection_error')
        self.assertGreater(time.monotonic()-started,5)
        self.assertLess(time.monotonic()-started,8)

if __name__=='__main__': unittest.main(verbosity=2)
