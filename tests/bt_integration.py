#!/usr/bin/env python3
"""Actual local libtorrent seeder, compact HTTP tracker and forwarding proxies.
No generated progress/peer statistics: test samples are exclusively service API values.
"""
import base64
import hashlib
import http.cookies
import http.server
import json
import os
import pathlib
import secrets
import select
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import media_integration as media
HELPER=sys.argv.pop(1)

class Tracker:
    def __init__(self):
        self.seed_port=0; self.requests=[]
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(self.path)
                peers=socket.inet_aton('127.0.0.1')+struct.pack('!H',owner.seed_port) if owner.seed_port else b''
                payload=b'd8:intervali1e5:peers'+str(len(peers)).encode()+b':'+peers+b'e'
                self.send_response(200); self.send_header('Content-Type','text/plain'); self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
            def log_message(self,*args): pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/announce'
    def close(self): self.server.shutdown(); self.server.server_close(); self.thread.join()

def recv(sock,n):
    result=b''
    while len(result)<n:
        data=sock.recv(n-len(result))
        if not data: raise OSError('closed')
        result+=data
    return result

class Proxy:
    def __init__(self,kind,username='',password='',stall=False):
        self.targets=[]; self.bytes=0; self.username=username; self.password=password; self.kind=kind; self.stall=stall
        self.lock=threading.Lock(); owner=self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                downstream=self.request; downstream.settimeout(5)
                upstream=None
                try:
                    if owner.stall: time.sleep(2); return
                    if owner.kind=='socks5':
                        head=recv(downstream,2); methods=recv(downstream,head[1]); required=2 if owner.username else 0
                        if head[0]!=5 or required not in methods: downstream.sendall(b'\x05\xff'); return
                        downstream.sendall(bytes([5,required]))
                        if required==2:
                            version=recv(downstream,1); u=recv(downstream,recv(downstream,1)[0]); p=recv(downstream,recv(downstream,1)[0])
                            good=version==b'\x01' and u.decode()==owner.username and p.decode()==owner.password
                            downstream.sendall(b'\x01'+bytes([0 if good else 1]))
                            if not good: return
                        head=recv(downstream,4)
                        if head[3]==1: host=socket.inet_ntoa(recv(downstream,4))
                        elif head[3]==3: host=recv(downstream,recv(downstream,1)[0]).decode()
                        else: return
                        port=struct.unpack('!H',recv(downstream,2))[0]
                        upstream=socket.create_connection((host,port),timeout=3)
                        downstream.sendall(b'\x05\x00\x00\x01'+socket.inet_aton('127.0.0.1')+b'\x00\x00')
                    else:
                        header=b''
                        while not header.endswith(b'\r\n\r\n') and len(header)<8192: header+=recv(downstream,1)
                        lines=header.decode().split('\r\n'); method,url,_=lines[0].split(' ',2)
                        if owner.username:
                            expected='Proxy-Authorization: Basic '+base64.b64encode((owner.username+':'+owner.password).encode()).decode()
                            if expected.lower() not in [line.lower() for line in lines]: downstream.sendall(b'HTTP/1.1 407 Rejected\r\nContent-Length: 0\r\n\r\n'); return
                        if method=='CONNECT': host,port=url.rsplit(':',1); port=int(port)
                        else: parsed=urllib.parse.urlsplit(url); host=parsed.hostname; port=parsed.port or 80
                        upstream=socket.create_connection((host,port),timeout=3)
                        if method=='CONNECT': downstream.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                        else:
                            # Forward genuine libtorrent HTTP tracker request through HTTP proxy.
                            lines[0]=f'{method} {parsed.path or "/"}{"?"+parsed.query if parsed.query else ""} HTTP/1.1'
                            upstream.sendall('\r\n'.join(lines).encode())
                    with owner.lock: owner.targets.append((host,port))
                    with upstream:
                        while True:
                            ready,_,_=select.select([downstream,upstream],[],[],3)
                            if not ready: break
                            for stream in ready:
                                data=stream.recv(16384)
                                if not data: return
                                (upstream if stream is downstream else downstream).sendall(data)
                                if stream is upstream:
                                    with owner.lock: owner.bytes+=len(data)
                except (OSError,ValueError,UnicodeError): pass
                finally:
                    if upstream is not None: upstream.close()
        class Service(socketserver.ThreadingTCPServer):
            allow_reuse_address=True; daemon_threads=True
        self.server=Service(('127.0.0.1',0),Handler); self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
    def config(self,**extra): return {'type':self.kind,'host':'127.0.0.1','port':self.port,'username':self.username,'password':self.password,'timeout_ms':1500,**extra}
    def close(self): self.server.shutdown(); self.server.server_close(); self.thread.join()

class BtFixture:
    def __init__(self,*,seconds=12,width=320,height=180,padding=2*1024*1024,upload_limit=512*1024,multi=False):
        self.tmp=tempfile.TemporaryDirectory(); self.data=pathlib.Path(self.tmp.name)
        self.payload_dir=self.data/'source'; self.payload_dir.mkdir()
        self.payload=self.payload_dir/'actual-video.mp4'; media.generate_video(self.payload,seconds,width,height)
        with self.payload.open('ab') as stream: stream.write(os.urandom(padding))
        self.original=self.payload.read_bytes()
        self.tracker=Tracker(); self.torrent=self.data/'source.torrent'
        if multi: (self.payload_dir/'A-prefix.bin').write_bytes(os.urandom(37001))
        subprocess.run([HELPER,'make',str(self.payload_dir if multi else self.payload),str(self.torrent),self.tracker.url],check=True,timeout=15)
        self.seedlog=(self.data/'seed.log').open('w')
        self.seed=subprocess.Popen([HELPER,'seed',str(self.torrent),str(self.payload_dir.parent if multi else self.payload_dir),str(upload_limit)],stdout=subprocess.PIPE,stderr=self.seedlog,text=True)
        self.server=None
        try:
            ready,_,_=select.select([self.seed.stdout],[],[],15)
            if not ready: raise AssertionError('real seeder did not initialize')
            line=self.seed.stdout.readline(); result=json.loads(line)
            if not result['ready']: raise AssertionError('fixture is not seeding')
            self.tracker.seed_port=result['port']
            self.password=secrets.token_urlsafe(24)
            if media.auth.initialize(self.data,'bt_administrator',self.password).returncode: raise AssertionError('admin init failed')
            self.server=media.auth.Server(self.data); self.admin=self.login('bt_administrator',self.password)
        except BaseException:
            if self.server: self.server.stop()
            if self.seed.poll() is None: self.seed.terminate(); self.seed.wait(timeout=10)
            self.seed.stdout.close(); self.seedlog.close(); self.tracker.close(); self.tmp.cleanup(); raise
    def login(self,name,password):
        result,headers=self.call('POST','/api/login',{'username':name,'password':password})
        cookie=http.cookies.SimpleCookie(headers['Set-Cookie'])
        return {'token':cookie['lv_session'].value,'csrf':result['csrf_token'],'user':result['user']}
    def call(self,method,path,data=None,session=None,expected=200,**kwargs):
        code,result,headers=self.server.request(method,path,data,session,**kwargs)
        if code!=expected: raise AssertionError(f'{method} {path}: {code}, {result.get("error","unknown")}')
        return result,headers
    def raw(self,payload=None,session=None,header_only_length=None):
        connection=media.http.client.HTTPConnection('127.0.0.1',self.server.port,timeout=10)
        session=session or self.admin
        try:
            headers={'Cookie':'lv_session='+session['token'],'X-CSRF-Token':session['csrf'],'Content-Type':'application/x-bittorrent'}
            if header_only_length is not None:
                # Observe early size rejection without racing a body send against close.
                connection.putrequest('POST','/api/torrents')
                for key,value in headers.items(): connection.putheader(key,value)
                connection.putheader('Content-Length',str(header_only_length)); connection.endheaders()
            else: connection.request('POST','/api/torrents',body=self.torrent.read_bytes() if payload is None else payload,headers=headers)
            response=connection.getresponse(); return response.status,json.loads(response.read())
        finally: connection.close()
    def imported(self,session=None):
        code,result=self.raw(session=session)
        if code!=201: raise AssertionError(f'actual torrent import failed: {code} {result}')
        return result['task']
    def state(self,id,session=None): return self.call('GET',f'/api/tasks/{id}',session=session or self.admin)[0]['task']
    def wait(self,id,predicate,seconds=30):
        deadline=time.monotonic()+seconds; samples=[]
        while time.monotonic()<deadline:
            state=self.state(id); samples.append(state)
            if predicate(state): return state,samples
            time.sleep(.05)
        raise AssertionError('BT condition timed out: '+json.dumps(samples[-1]))
    def close(self):
        self.server.stop(); self.seed.terminate(); self.seed.wait(timeout=10); self.seed.stdout.close(); self.seedlog.close(); self.tracker.close(); self.tmp.cleanup()

class BtIntegration(unittest.TestCase):
    def setUp(self): self.f=BtFixture(); self.addCleanup(self.f.close)
    def download(self,proxy=None):
        if proxy: self.f.call('POST','/api/proxy',proxy.config(),self.f.admin)
        task=self.f.imported(); self.f.call('POST',f'/api/tasks/{task["id"]}/resume',{},self.f.admin)
        done,samples=self.f.wait(task['id'],lambda state:state['progress']==1 and state['state']=='seeding')
        self.assertTrue(any(row['peers']>=1 for row in samples)); self.assertTrue(any(row['download_bytes_per_second']>0 for row in samples))
        dest=self.f.data/'downloads'/f'task-{task["id"]}'/self.f.payload.name
        self.assertEqual(dest.read_bytes(),self.f.original)
        self.assertEqual(done['verified_bytes'],len(self.f.original)); self.assertEqual(done['verified_pieces'],done['piece_count'])
        self.assertTrue(any('info_hash=' in request for request in self.f.tracker.requests))
        return task,done,dest,samples
    def test_01_real_tracker_peer_piece_hashes_pause_restart(self):
        task=self.f.imported(); id=task['id']; self.assertTrue(task['paused'])
        self.f.call('POST',f'/api/tasks/{id}/resume',{},self.f.admin)
        partial,samples=self.f.wait(id,lambda state:0<state['progress']<1)
        self.f.call('POST',f'/api/tasks/{id}/pause',{},self.f.admin)
        state,_=self.f.wait(id,lambda state:state['paused_actual'] and state['download_bytes_per_second']==0)
        saved,_=self.f.wait(id,lambda state:state['resume_saved'])
        self.f.server.stop(); self.f.server=media.auth.Server(self.f.data)
        restored=self.f.state(id); self.assertTrue(restored['paused']); self.assertTrue(restored['resume_saved'])
        self.f.call('POST',f'/api/tasks/{id}/resume',{},self.f.admin)
        done,rest=self.f.wait(id,lambda state:state['state']=='seeding' and state['progress']==1)
        dest=self.f.data/'downloads'/f'task-{id}'/self.f.payload.name
        self.assertEqual(hashlib.sha256(dest.read_bytes()).digest(),hashlib.sha256(self.f.original).digest())
        evidence={'transport':'actual local HTTP tracker + TCP seeder','payload_bytes':len(self.f.original),'piece_count':done['piece_count'],'partial_verified_bytes':partial['verified_bytes'],'max_observed_peers':max(x['peers'] for x in samples+rest),'max_observed_download_bytes_per_second':max(x['download_bytes_per_second'] for x in samples+rest),'sha256':hashlib.sha256(self.f.original).hexdigest(),'restart_completed':True}
        path=pathlib.Path(media.auth.BINARY).resolve().parent/'Testing'/'artifacts'; path.mkdir(parents=True,exist_ok=True); (path/'bt-evidence.json').write_text(json.dumps(evidence,indent=2))
    def test_02_socks5_credentials_real_tracker_and_payload(self):
        p=Proxy('socks5',secrets.token_hex(8),secrets.token_hex(12)); self.addCleanup(p.close)
        self.download(p)
        self.assertIn(('127.0.0.1',self.f.tracker.seed_port),p.targets)
        self.assertIn(('127.0.0.1',self.f.tracker.server.server_port),p.targets)
        self.assertGreater(p.bytes,len(self.f.original))
        text=self.f.server.logpath.read_text()
        self.assertNotIn(p.username,text); self.assertNotIn(p.password,text)
        with media.sqlite3.connect(self.f.data/'lanvideo.db') as db:
            saved=db.execute("SELECT value FROM settings WHERE key='bt_proxy'").fetchone()[0]
            self.assertNotIn(p.password,saved)
        self.f.server.stop(); self.f.server=media.auth.Server(self.f.data)
        summary=self.f.call('GET','/api/bt/config',session=self.f.admin)[0]['proxy']
        self.assertTrue(summary['credentials_configured']); self.assertNotIn('password',summary)
    def test_03_http_proxy_real_tracker_and_connect_payload(self):
        p=Proxy('http',secrets.token_hex(8),secrets.token_hex(12)); self.addCleanup(p.close)
        self.download(p)
        self.assertIn(('127.0.0.1',self.f.tracker.seed_port),p.targets)
        self.assertIn(('127.0.0.1',self.f.tracker.server.server_port),p.targets)
        self.assertGreater(p.bytes,len(self.f.original))
    def test_04_recheck_detects_corrupt_piece_and_recovers(self):
        task,done,dest,_=self.download(); id=task['id']
        self.f.call('POST',f'/api/tasks/{id}/pause',{},self.f.admin)
        self.f.wait(id,lambda state:state['paused_actual'])
        with dest.open('r+b') as stream: stream.seek(100); stream.write(b'corruption')
        # Freeze the real remote seeder while observing rehash results, so a 64 KiB
        # repair cannot finish between two status polls. No service metrics are injected.
        self.f.seed.send_signal(signal.SIGSTOP)
        try:
            self.f.call('POST',f'/api/tasks/{id}/recheck',{},self.f.admin)
            self.f.call('POST',f'/api/tasks/{id}/resume',{},self.f.admin)
            dropped,_=self.f.wait(id,lambda state:state['state']=='downloading' and state['verified_pieces']==done['piece_count']-1)
            self.assertLess(dropped['verified_pieces'],done['piece_count'])
        finally: self.f.seed.send_signal(signal.SIGCONT)
        _,samples=self.f.wait(id,lambda state:state['state']=='seeding' and state['progress']==1)
        self.assertEqual(dest.read_bytes(),self.f.original)
    def test_05_owner_permissions_pause_mutations_csrf(self):
        password=secrets.token_urlsafe(24)
        self.f.call('POST','/api/users',{'username':'bt_viewer','password':password},self.f.admin,expected=201)
        viewer=self.f.login('bt_viewer',password); task=self.f.imported(viewer)
        password2=secrets.token_urlsafe(24)
        self.f.call('POST','/api/users',{'username':'other_viewer','password':password2},self.f.admin,expected=201)
        other=self.f.login('other_viewer',password2)
        self.assertEqual(self.f.call('GET','/api/tasks',session=other)[0]['tasks'],[])
        for method,suffix in [('GET',''),('POST','/resume'),('DELETE','')]: self.f.call(method,f'/api/tasks/{task["id"]}'+suffix,{} if method=='POST' else None,other,expected=404)
        self.f.call('POST',f'/api/tasks/{task["id"]}/resume',{},viewer,csrf=False,expected=403)
        self.f.call('GET','/api/bt/config',session=viewer,expected=403)
        self.f.call('POST','/api/proxy',{'type':'none'},viewer,expected=403)
        self.f.call('DELETE',f'/api/tasks/{task["id"]}',session=viewer)
    def test_06_invalid_metadata_traversal_and_limits(self):
        self.assertEqual(self.f.raw(b'not bencode')[0],400)
        original=self.f.torrent.read_bytes()
        self.assertEqual(self.f.raw(original+b'garbage')[0],400)
        needle=str(len(self.f.payload.name)).encode()+b':'+self.f.payload.name.encode()
        for unsafe in [b'../escape.mp4',b'/tmp/escape.mp4',b'bad\\name.mp4']:
            changed=original.replace(needle,str(len(unsafe)).encode()+b':'+unsafe)
            self.assertEqual(self.f.raw(changed)[0],400)
        self.assertEqual(self.f.raw(header_only_length=1024*1024+1)[0],413)
    def test_07_proxy_test_authentication_timeout_and_fail_closed(self):
        p=Proxy('socks5','proxyuser',secrets.token_hex(12)); self.addCleanup(p.close)
        self.f.call('POST','/api/proxy',p.config(),self.f.admin)
        target={'host':'127.0.0.1','port':self.f.tracker.seed_port}
        result=self.f.call('POST','/api/proxy/test',target,self.f.admin)[0]; self.assertTrue(result['connected'])
        self.f.call('POST','/api/proxy',p.config(password='incorrect-secret'),self.f.admin)
        self.f.call('POST','/api/proxy/test',target,self.f.admin,expected=502)
        stall=Proxy('socks5',stall=True); self.addCleanup(stall.close)
        self.f.call('POST','/api/proxy',stall.config(timeout_ms=200),self.f.admin)
        start=time.monotonic(); self.f.call('POST','/api/proxy/test',target,self.f.admin,expected=408); self.assertLess(time.monotonic()-start,1)
        with socket.socket() as unavailable: unavailable.bind(('127.0.0.1',0)); badport=unavailable.getsockname()[1]
        self.f.call('POST','/api/proxy',{'type':'socks5','host':'127.0.0.1','port':badport,'timeout_ms':200},self.f.admin)
        task=self.f.imported(); self.f.call('POST',f'/api/tasks/{task["id"]}/resume',{},self.f.admin)
        time.sleep(1.5); self.assertEqual(self.f.state(task['id'])['verified_bytes'],0)
        self.f.call('POST','/api/proxy',{'type':'none'},self.f.admin,expected=409)
    def test_08_http_connect_auth_and_timeout_errors(self):
        proxy=Proxy('http',secrets.token_hex(8),secrets.token_hex(12)); self.addCleanup(proxy.close)
        target={'host':'127.0.0.1','port':self.f.tracker.seed_port}
        self.f.call('POST','/api/proxy',proxy.config(),self.f.admin)
        self.assertTrue(self.f.call('POST','/api/proxy/test',target,self.f.admin)[0]['connected'])
        self.f.call('POST','/api/proxy',proxy.config(password='wrong-password'),self.f.admin)
        self.f.call('POST','/api/proxy/test',target,self.f.admin,expected=502)
        for data in [{'type':'socks5','host':'bad\r\nheader','port':80}, {'type':'http','host':'127.0.0.1','port':65536}, {'type':'none','timeout_ms':0}]:
            self.f.call('POST','/api/proxy',data,self.f.admin,expected=400)
    def test_09_download_root_and_symlink_restore_rejection(self):
        outside=self.f.data/'new-downloads'; outside.mkdir()
        self.f.call('POST','/api/bt/root',{'path':str(outside)},self.f.admin)
        task=self.f.imported(); self.f.call('POST',f'/api/tasks/{task["id"]}/resume',{},self.f.admin)
        self.f.wait(task['id'],lambda state:state['state']=='seeding')
        self.assertEqual((outside/f'task-{task["id"]}'/self.f.payload.name).read_bytes(),self.f.original)
        self.f.call('POST',f'/api/tasks/{task["id"]}/pause',{},self.f.admin)
        self.f.server.stop()
        dest=outside/f'task-{task["id"]}'/self.f.payload.name; dest.unlink(); dest.symlink_to(self.f.payload)
        self.f.server=media.auth.Server(self.f.data)
        self.assertEqual(self.f.state(task['id'])['state'],'unavailable')
        self.assertEqual(self.f.payload.read_bytes(),self.f.original)
        self.f.call('POST','/api/media/root',{'path':str(outside/f'task-{task["id"]}')},self.f.admin,expected=409)
        self.f.call('POST','/api/media/root',{'path':str(outside)},self.f.admin)
        self.f.call('POST','/api/media/scan',{},self.f.admin)
        self.assertEqual(self.f.call('GET','/api/media',session=self.f.admin)[0]['total'],0)
    def test_10_repeated_start_stop_with_ephemeral_bt_listener(self):
        # Regression for a startup failure observed after adding an independent BT listener.
        for _ in range(12):
            self.f.server.stop(); self.f.server=media.auth.Server(self.f.data)
            response=self.f.call('GET','/api/health')[0]
            self.assertEqual(response['status'],'ok')
            self.assertEqual(self.f.call('GET','/api/me',session=self.f.admin)[0]['user']['id'],self.f.admin['user']['id'])

if __name__=='__main__': unittest.main(verbosity=2)
