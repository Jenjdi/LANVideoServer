#!/usr/bin/env python3
"""Regression tests for request ambiguity, proxy negotiation and file identity."""
import hashlib
import http.client
import json
import os
import pathlib
import secrets
import socket
import subprocess
import tempfile
import threading
import socketserver
import time
import unittest
import media_integration as media


class SecurityRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.video = pathlib.Path(cls.temp.name) / 'sample.mp4'
        media.generate_video(cls.video, seconds=1)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.f = media.Fixture(self.video)
        self.addCleanup(self.f.close)

    def test_cross_origin_login_rejected_on_local_http(self):
        self.f.call('POST', '/api/login', {'username':'media_administrator', 'password':self.f.password},
                    headers={'Origin':'http://untrusted.example'}, expected=403)
        self.f.call('POST', '/api/login', {'username':'media_administrator', 'password':self.f.password},
                    headers={'Origin':self.f.server.base})

    def test_cross_origin_share_exchange_rejected(self):
        share = self.f.call('POST', '/api/shares', {'media_id':self.f.item['id'], 'ttl_seconds':60}, self.f.admin, expected=201)[0]
        self.f.call('POST', '/share/api/exchange', {'token':share['url'].split('#')[1]},
                    headers={'Origin':'http://untrusted.example'}, expected=403)

    def test_duplicate_origin_headers_rejected(self):
        connection=http.client.HTTPConnection('127.0.0.1',self.f.server.port,timeout=5)
        self.addCleanup(connection.close)
        payload=json.dumps({'username':'media_administrator','password':self.f.password}).encode()
        connection.putrequest('POST','/api/login')
        connection.putheader('Content-Type','application/json'); connection.putheader('Content-Length',len(payload))
        connection.putheader('Origin',self.f.server.base); connection.putheader('Origin','http://untrusted.example')
        connection.endheaders(payload)
        response=connection.getresponse(); self.assertEqual(response.status,403); response.read()

    def test_duplicate_json_keys_rejected(self):
        payload = '{"username":"other","username":"media_administrator","password":' + json.dumps(self.f.password) + '}'
        code, _, _ = self.f.binary('POST', '/api/login', payload=payload, headers={'Content-Type':'application/json'})
        self.assertEqual(code, 400)

    def test_nested_duplicate_keys_rejected(self):
        payload = '{"username":"media_administrator","password":' + json.dumps(self.f.password) + ',"extra":{"a":1,"a":2}}'
        code, _, _ = self.f.binary('POST', '/api/login', payload=payload, headers={'Content-Type':'application/json'})
        self.assertEqual(code, 400)

    def manifest(self):
        return self.f.call('POST', '/api/uploads', {'filename':'upload.mp4', 'size':len(self.f.bytes),
                          'hashes':[hashlib.sha256(self.f.bytes).hexdigest()]}, self.f.admin, expected=201)[0]

    def replace_directory(self, upload):
        self.f.root.rename(self.f.data / 'original-library')
        self.f.root.mkdir()
        victim = self.f.root / ('.resume-' + upload['id'])
        victim.write_bytes(b'unrelated file')
        return victim

    def test_discard_never_removes_file_in_replaced_directory(self):
        upload = self.manifest()
        victim = self.replace_directory(upload)
        self.f.call('DELETE', '/api/uploads/' + upload['id'], session=self.f.admin, expected=409)
        self.assertEqual(victim.read_bytes(), b'unrelated file')

    def test_expiry_never_removes_file_in_replaced_directory(self):
        upload = self.manifest()
        victim = self.replace_directory(upload)
        self.f.db('UPDATE uploads SET expires=0 WHERE id=?', (upload['id'],))
        self.f.server.stop()
        self.f.server = media.auth.Server(self.f.data)
        self.assertTrue(victim.exists(), 'expiry removed an unrelated replacement file')
        self.assertEqual(victim.read_bytes(), b'unrelated file')

    def test_discard_never_removes_replaced_staging_file(self):
        upload=self.manifest(); victim=self.f.root / ('.resume-'+upload['id'])
        victim.rename(self.f.root / '.original-staging'); victim.write_bytes(b'unrelated file')
        self.f.call('DELETE','/api/uploads/'+upload['id'],session=self.f.admin,expected=409)
        self.assertEqual(victim.read_bytes(),b'unrelated file')

    def test_socks5_rejects_unadvertised_authentication_method(self):
        class Downgrade(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.settimeout(2)
                    def read(length):
                        result = b''
                        while len(result)<length:
                            part = self.request.recv(length-len(result))
                            if not part: raise EOFError()
                            result += part
                        return result
                    greeting = read(2); read(greeting[1])
                    self.request.sendall(b'\x05\x00')
                    read(10)
                    self.request.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50')
                except (OSError, EOFError): pass
        server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Downgrade)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever); thread.start()
        self.addCleanup(thread.join, 5); self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.f.call('POST', '/api/proxy', {'type':'socks5','host':'127.0.0.1','port':server.server_address[1],
                    'username':'test_user','password':secrets.token_urlsafe(16)}, self.f.admin)
        self.f.call('POST', '/api/proxy/test', {'host':'127.0.0.1','port':80}, self.f.admin, expected=502)

    def test_proxy_address_rejects_embedded_null(self):
        self.f.call('POST', '/api/proxy', {'type':'socks5','host':'127.0.0.1\0ignored','port':1080},
                    self.f.admin, expected=400)

    def test_startup_rejects_symlink_database_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as temp:
            data = pathlib.Path(temp) / 'data'; data.mkdir()
            target = pathlib.Path(temp) / 'outside.db'
            target.write_bytes((self.f.data / 'lanvideo.db').read_bytes())
            before = hashlib.sha256(target.read_bytes()).digest()
            (data / 'lanvideo.db').symlink_to(target)
            result = subprocess.run([media.BINARY], env=media.auth.environment(data), capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(hashlib.sha256(target.read_bytes()).digest(), before)

    def test_startup_rejects_symlink_data_without_chmod(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp); target = root / 'outside'; target.mkdir(); target.chmod(0o755)
            alias = root / 'alias'; alias.symlink_to(target)
            before = target.stat().st_mode
            result = subprocess.run([media.BINARY], env=media.auth.environment(alias), capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(target.stat().st_mode, before)

    def test_slow_headers_cannot_occupy_all_workers_indefinitely(self):
        self.check_slow_connections(b'GET /api/health HTTP/1.1\r\nHost: ')

    def test_slow_body_cannot_occupy_all_workers_indefinitely(self):
        self.check_slow_connections(b'POST /api/login HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{')

    def check_slow_connections(self, request):
        clients = []
        stop = threading.Event()
        def trickle():
            while not stop.wait(.2):
                for client in clients:
                    try: client.sendall(b'a')
                    except OSError: pass
        try:
            for _ in range(8):
                client = socket.create_connection(('127.0.0.1',self.f.server.port), timeout=2)
                clients.append(client)
                client.sendall(request)
            thread = threading.Thread(target=trickle); thread.start()
            time.sleep(11)
            started = time.monotonic()
            self.f.call('GET', '/api/health')
            self.assertLess(time.monotonic()-started, 2)
        finally:
            stop.set()
            for client in clients: client.close()
            if 'thread' in locals(): thread.join(2)

    def test_shutdown_interrupts_incomplete_requests(self):
        clients = []
        try:
            for _ in range(8):
                client = socket.create_connection(('127.0.0.1',self.f.server.port), timeout=2)
                clients.append(client); client.sendall(b'GET /api/health HTTP/1.1\r\nHost: ')
            time.sleep(.2)
            started = time.monotonic()
            self.f.server.stop()
            self.assertLess(time.monotonic()-started, 2)
        finally:
            for client in clients: client.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
