#!/usr/bin/env python3
"""CLI processes, HTTP requests, SQLite persistence and authorization boundaries.

Passwords/tokens are generated at runtime, passed through stdin/HTTP, never printed.
Database writes in expiry tests are test-fixture injection, not production bypass APIs.
"""
import concurrent.futures
import hashlib
import http.cookies
import json
import os
import pathlib
import pty
import secrets
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import urllib.error
import urllib.request

BINARY = sys.argv.pop(1)

def environment(data, **overrides):
    return {**os.environ, 'LANVIDEO_BIND':'127.0.0.1', 'LANVIDEO_DATA':str(data),
            'LANVIDEO_LOG_LEVEL':'info', 'LANVIDEO_PUBLIC_ORIGIN':'',
            'LANVIDEO_SESSION_TTL':'28800', **overrides}

def initialize(data, username, password):
    return subprocess.run([BINARY,'--init-admin-stdin'],
                          input=json.dumps({'username':username,'password':password}),
                          env=environment(data),capture_output=True,text=True,timeout=10)

class Server:
    def __init__(self, data, **overrides):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); self.port=sock.getsockname()[1]
        self.base=f'http://127.0.0.1:{self.port}'
        self.data=pathlib.Path(data)
        self.logpath=self.data/'auth-test.log'
        self.logfile=self.logpath.open('a')
        self.process=subprocess.Popen([BINARY],env=environment(data,LANVIDEO_PORT=str(self.port),**overrides),stdout=self.logfile,stderr=self.logfile)
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(self.base+'/api/health',timeout=1).close(); break
                except OSError:
                    if self.process.poll() is not None: raise AssertionError('server failed during startup')
                    time.sleep(.05)
            else: raise AssertionError('server startup timeout')
        except BaseException:
            try: self.stop()
            except AssertionError: pass
            # Report the controlled startup reason rather than hiding it in stop().
            for line in self.logpath.read_text().splitlines()[::-1]:
                try: entry=json.loads(line)
                except ValueError: continue
                if entry.get('event')=='startup_failure': raise AssertionError('server startup: '+entry['fields']['reason'])
            raise
    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try: self.process.wait(timeout=10)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(); raise
        self.logfile.close()
        if self.process.returncode != 0: raise AssertionError('service did not exit cleanly')
    def request(self, method, path, data=None, session=None, bearer=False, csrf=True, headers=None):
        request_headers={}
        payload=None
        if data is not None:
            payload=json.dumps(data).encode(); request_headers['Content-Type']='application/json'
        if session:
            if bearer: request_headers['Authorization']='Bearer '+session['token']
            else:
                request_headers['Cookie']='lv_session='+session['token']
                if csrf and method not in ('GET','HEAD'):
                    request_headers['X-CSRF-Token']=session['csrf']
        request_headers.update(headers or {})
        request=urllib.request.Request(self.base+path,data=payload,headers=request_headers,method=method)
        try: response=urllib.request.urlopen(request,timeout=10)
        except urllib.error.HTTPError as exc: response=exc
        with response:
            return response.code,json.load(response),response.headers

class AuthIntegration(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data=pathlib.Path(self.directory.name)
        self.password=secrets.token_urlsafe(24)
        self.sensitive=[self.password]
        self.cli=initialize(self.data,'administrator',self.password)
        self.assertEqual(self.cli.returncode,0,'initial admin CLI failed')
        self.server=Server(self.data)
        self.addCleanup(self.server.stop)
        self.admin=self.login('administrator',self.password)
    def db(self,sql,values=()):
        with sqlite3.connect(self.data/'lanvideo.db') as database:
            return database.execute(sql,values).fetchall()
    def call(self,method,path,data=None,session=None,expected=200,**kwargs):
        status,response,headers=self.server.request(method,path,data,session,**kwargs)
        self.assertEqual(status,expected,f'{method} {path}: unexpected status')
        return response,headers
    def login(self,name,password):
        self.sensitive.append(password)
        response,headers=self.call('POST','/api/login',{'username':name,'password':password})
        cookie=http.cookies.SimpleCookie(headers['Set-Cookie'])
        token=cookie['lv_session'].value
        self.sensitive.extend([token,response['csrf_token']])
        return {'token':token,'csrf':response['csrf_token'],'user':response['user'],'headers':headers,'expires':response['expires']}
    def create(self,name='normal_user',role='user',password=None):
        password=password or secrets.token_urlsafe(24); self.sensitive.append(password)
        response,_=self.call('POST','/api/users',{'username':name,'password':password,'role':role},self.admin,expected=201)
        return response['user'],password
    def test_01_initialization_is_once_and_not_remote(self):
        response,_=self.call('GET','/api/bootstrap/status')
        self.assertTrue(response['initialized'])
        self.call('POST','/api/bootstrap',{'username':'intruder','password':secrets.token_urlsafe(24)},expected=405)
        second=initialize(self.data,'replacement',secrets.token_urlsafe(24))
        self.assertEqual(second.returncode,1)
        self.assertIn('already been initialized',second.stdout)
        self.assertEqual(self.db('SELECT count(*) FROM users')[0][0],1)
    def test_02_login_cookie_and_secret_storage(self):
        header=self.admin['headers']['Set-Cookie']
        self.assertIn('HttpOnly',header); self.assertIn('SameSite=Strict',header); self.assertIn('Path=/',header)
        self.assertNotIn('Secure',header) # Local HTTP mode only.
        stored=self.db('SELECT password_hash FROM users')[0][0]
        self.assertTrue(stored.startswith('$argon2id$v=19$m=65536,t=2,p=1$'))
        self.assertTrue(self.password not in stored)
        hashes=self.db('SELECT token_hash FROM sessions')
        self.assertEqual(hashes[0][0],hashlib.sha256(self.admin['token'].encode()).hexdigest())
        self.assertTrue(all(value[0]!=self.admin['token'] for value in hashes))
        response,_=self.call('GET','/api/me',session=self.admin)
        self.assertEqual(response['user']['role'],'admin')
        self.assertTrue(response['csrf_token']==self.admin['csrf'])
    def test_03_bad_credentials_share_one_error(self):
        bad=secrets.token_urlsafe(24)
        a,_=self.call('POST','/api/login',{'username':'administrator','password':bad},expected=401)
        b,_=self.call('POST','/api/login',{'username':'unknown_user','password':bad},expected=401)
        self.assertEqual(a,b)
        self.assertEqual(self.db('SELECT count(*) FROM sessions')[0][0],1)
    def test_04_anonymous_requests_are_denied(self):
        routes=[('GET','/api/me'),('GET','/api/users'),('POST','/api/users'),('PATCH','/api/users/1'),('DELETE','/api/users/1'),('POST','/api/users/1/password'),('POST','/api/password'),('POST','/api/logout')]
        for method,path in routes:
            with self.subTest(method=method,path=path): self.call(method,path,{},expected=401)
        self.call('GET','/api/me',headers={'Authorization':'Bearer '+'f'*64},expected=401)
    def test_05_regular_user_cannot_manage_any_user(self):
        user,password=self.create(); session=self.login(user['username'],password)
        for method,path,data in [('GET','/api/users',None),('POST','/api/users',{'username':'new_admin','password':password,'role':'admin'}),('PATCH',f'/api/users/{user["id"]}',{'role':'admin'}),('DELETE',f'/api/users/{user["id"]}',None),('POST','/api/users/1/password',{'password':password})]:
            with self.subTest(method=method,path=path): self.call(method,path,data,session,expected=403)
        self.assertEqual(self.db('SELECT role FROM users WHERE id=?',(user['id'],))[0][0],'user')
    def test_06_csrf_and_ambiguous_authentication(self):
        self.call('POST','/api/users',{'username':'blocked_user','password':self.password},self.admin,csrf=False,expected=403)
        self.call('POST','/api/logout',session=self.admin,headers={'X-CSRF-Token':'invalid'},expected=403)
        self.call('GET','/api/me',session=self.admin,headers={'Authorization':'Bearer '+self.admin['token']},expected=400)
        self.call('GET','/api/me',headers={'Cookie':'lv_session='+self.admin['token']+'; lv_session='+self.admin['token']},expected=400)
        response,_=self.call('GET','/api/me',session=self.admin,bearer=True)
        self.assertEqual(response['user']['id'],1)
        self.call('POST','/api/logout',session=self.admin,bearer=True,csrf=False)
        self.call('GET','/api/me',session=self.admin,expected=401)
    def test_07_create_disable_enable_promote_demote_delete(self):
        user,password=self.create(); session=self.login(user['username'],password)
        self.call('POST','/api/users',{'username':user['username'],'password':password},self.admin,expected=409)
        listing,_=self.call('GET','/api/users',session=self.admin)
        self.assertEqual(len(listing['users']),2)
        self.assertTrue(all('password_hash' not in row for row in listing['users']))
        route=f'/api/users/{user["id"]}'
        self.call('PATCH',route,{'disabled':True},self.admin)
        self.call('GET','/api/me',session=session,expected=401)
        self.call('POST','/api/login',{'username':user['username'],'password':password},expected=401)
        self.call('PATCH',route,{'disabled':False},self.admin)
        self.call('GET','/api/me',session=session,expected=401)
        session=self.login(user['username'],password)
        self.call('PATCH',route,{'role':'admin'},self.admin)
        self.call('GET','/api/me',session=session,expected=401)
        promoted=self.login(user['username'],password)
        self.call('GET','/api/users',session=promoted)
        self.call('PATCH',route,{'role':'user'},self.admin)
        self.call('GET','/api/users',session=promoted,expected=401)
        ordinary=self.login(user['username'],password)
        self.call('GET','/api/users',session=ordinary,expected=403)
        self.call('DELETE',route,session=self.admin)
        self.call('GET','/api/me',session=ordinary,expected=401)
        self.assertEqual(self.db('SELECT count(*) FROM sessions WHERE user_id=?',(user['id'],))[0][0],0)
    def test_08_password_change_requires_old_password_and_revokes_all(self):
        user,password=self.create(); one=self.login(user['username'],password); two=self.login(user['username'],password)
        new=secrets.token_urlsafe(24); self.sensitive.append(new)
        self.call('POST','/api/password',{'old_password':'wrong_password','new_password':new},one,expected=403)
        self.call('POST','/api/password',{'old_password':password,'new_password':'short'},one,expected=400)
        self.call('GET','/api/me',session=one)
        result,headers=self.call('POST','/api/password',{'old_password':password,'new_password':new},one)
        self.assertTrue(result['login_required']); self.assertIn('Max-Age=0',headers['Set-Cookie'])
        for session in [one,two]: self.call('GET','/api/me',session=session,expected=401)
        self.call('POST','/api/login',{'username':user['username'],'password':password},expected=401)
        self.login(user['username'],new)
    def test_09_admin_reset_revokes_sessions(self):
        user,password=self.create(); one=self.login(user['username'],password); two=self.login(user['username'],password)
        new=secrets.token_urlsafe(24); self.sensitive.append(new)
        self.call('POST',f'/api/users/{user["id"]}/password',{'password':new},self.admin)
        for session in [one,two]: self.call('GET','/api/me',session=session,expected=401)
        self.call('POST','/api/login',{'username':user['username'],'password':password},expected=401)
        self.login(user['username'],new)
    def test_10_last_administrator_is_protected(self):
        self.call('PATCH','/api/users/1',{'role':'user'},self.admin,expected=409)
        self.call('PATCH','/api/users/1',{'disabled':True},self.admin,expected=409)
        self.call('DELETE','/api/users/1',session=self.admin,expected=409)
        user,password=self.create('second_admin','admin'); second=self.login(user['username'],password)
        self.call('DELETE','/api/users/1',session=second)
        self.call('GET','/api/me',session=self.admin,expected=401)
        self.call('DELETE',f'/api/users/{user["id"]}',session=second,expected=409)
        self.assertEqual(initialize(self.data,'cannot_reinitialize',self.password).returncode,1)
    def test_11_validation_and_injection_attempts(self):
        inputs=[{'username':"x'; DROP TABLE users;--",'password':self.password}, {'username':'valid_name','password':'short'}, {'username':'valid_name','password':self.password,'role':'root'}, {'username':'valid_name','password':self.password,'disabled':False}, {'username':12,'password':self.password}, {'username':'x'*65,'password':self.password}, {'username':'valid_name','password':self.password+'\0'}]
        for value in inputs: self.call('POST','/api/users',value,self.admin,expected=400)
        self.call('PATCH','/api/users/1',{'disabled':1},self.admin,expected=400)
        self.call('PATCH','/api/users/1',{'password':self.password},self.admin,expected=400)
        self.call('PATCH','/api/users/1',{},self.admin,expected=400)
        self.call('DELETE','/api/users/'+'9'*30,session=self.admin,expected=400)
        self.call('DELETE','/api/users/999999',session=self.admin,expected=404)
        self.call('PATCH','/api/users/999999',{'role':'user'},self.admin,expected=404)
        self.call('POST','/api/users/999999/password',{'password':self.password},self.admin,expected=404)
        self.assertEqual(self.db('SELECT count(*) FROM users')[0][0],1)
    def test_12_logout_only_revokes_current_session(self):
        other=self.login('administrator',self.password)
        _,headers=self.call('POST','/api/logout',session=self.admin)
        self.assertIn('Max-Age=0',headers['Set-Cookie'])
        self.call('GET','/api/me',session=self.admin,expected=401)
        self.call('GET','/api/me',session=other)
    def test_13_sessions_survive_restart_and_expired_sessions_fail(self):
        self.server.stop(); self.server=Server(self.data); self.addCleanup(self.server.stop)
        self.call('GET','/api/me',session=self.admin)
        self.db('UPDATE sessions SET expires=?',(int(time.time())-1,))
        self.call('GET','/api/me',session=self.admin,expected=401)
        self.login('administrator',self.password)
        self.assertEqual(self.db('SELECT count(*) FROM sessions')[0][0],1)
    def test_14_salts_audit_and_log_redaction(self):
        first,password=self.create('same_password_one'); self.create('same_password_two',password=password)
        session=self.login(first['username'],password)
        self.call('POST','/api/logout',session=session)
        hashes=self.db('SELECT password_hash FROM users WHERE username LIKE ?',('same_password_%',))
        self.assertTrue(hashes[0][0]!=hashes[1][0])
        events=self.db('SELECT event,detail FROM audit ORDER BY id')
        self.assertTrue({'admin_initialized','user_created','login_succeeded','logout'}.issubset({row[0] for row in events}))
        evidence=self.server.logpath.read_text()+self.cli.stdout+json.dumps(events)
        for secret in self.sensitive:
            self.assertTrue(secret not in evidence,'sensitive value appeared in audit or process logs')
        for line in self.server.logpath.read_text().splitlines(): self.assertIn('event',json.loads(line))
    def test_15_login_rate_limit(self):
        for _ in range(19):
            self.call('POST','/api/login',{'username':'administrator','password':'incorrect_password'},expected=401)
        self.call('POST','/api/login',{'username':'administrator','password':self.password},expected=429)
        self.call('GET','/api/me',session=self.admin) # Existing sessions are unaffected.
    def test_16_session_count_is_bounded(self):
        user,password=self.create()
        sessions=[self.login(user['username'],password) for _ in range(12)]
        self.assertEqual(self.db('SELECT count(*) FROM sessions WHERE user_id=?',(user['id'],))[0][0],10)
        self.call('GET','/api/me',session=sessions[0],expected=401)
        self.call('GET','/api/me',session=sessions[-1])
    def test_17_initialization_race_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as data:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda name:initialize(data,name,secrets.token_urlsafe(24)),['race_admin_one','race_admin_two']))
            self.assertEqual(sorted(result.returncode for result in results),[0,1])
            with sqlite3.connect(pathlib.Path(data)/'lanvideo.db') as database:
                self.assertEqual(database.execute('SELECT count(*) FROM users').fetchone()[0],1)
                self.assertEqual(database.execute("SELECT value FROM settings WHERE key='auth_initialized'").fetchone()[0],'1')
                self.assertEqual(database.execute("SELECT count(*) FROM audit WHERE event='admin_initialized'").fetchone()[0],1)
    def test_18_concurrent_admin_changes_keep_one_enabled(self):
        user,password=self.create('concurrent_admin','admin'); second=self.login(user['username'],password)
        def disable(session): return self.server.request('PATCH',f'/api/users/{session["user"]["id"]}',{'disabled':True},session)[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(disable,[self.admin,second]))
        self.assertEqual(sorted(statuses),[200,409])
        self.assertEqual(self.db("SELECT count(*) FROM users WHERE role='admin' AND disabled=0")[0][0],1)
    def test_19_https_cookie_and_origin_policy(self):
        self.server.stop()
        self.server=Server(self.data,LANVIDEO_PUBLIC_ORIGIN='https://video.example.test',LANVIDEO_SESSION_TTL='60'); self.addCleanup(self.server.stop)
        secure=self.login('administrator',self.password)
        self.assertIn('; Secure',secure['headers']['Set-Cookie'])
        self.assertIn('Max-Age=60',secure['headers']['Set-Cookie'])
        self.call('POST','/api/login',{'username':'administrator','password':self.password},headers={'Origin':'https://other.example.test'},expected=403)
        self.call('POST','/api/logout',session=secure,headers={'Origin':'https://other.example.test'},expected=403)
        self.call('POST','/api/logout',session=secure,headers={'Origin':'https://video.example.test'})
    def test_20_auth_request_limits_and_content_type(self):
        self.call('POST','/api/login',{'username':'administrator','password':self.password},headers={'Content-Type':'text/plain'},expected=415)
        self.call('POST','/api/login',{'username':'administrator','password':'x'*5000},expected=413)
        self.call('POST','/api/login',{'username':'administrator','password':123},expected=400)
        self.call('POST','/api/login',{'username':'administrator','password':self.password,'role':'admin'},expected=400)
        request=urllib.request.Request(self.server.base+'/api/login',data=b'{invalid',headers={'Content-Type':'application/json'},method='POST')
        with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request,timeout=5)
        self.assertEqual(error.exception.code,400)
        error.exception.close()
        self.call('POST','/api/login',{'username':'administrator','password':self.password},expected=415,headers={'Content-Encoding':'gzip'})
    def test_21_interactive_password_input_is_hidden(self):
        with tempfile.TemporaryDirectory() as data:
            master,slave=pty.openpty(); self.addCleanup(os.close,master); self.addCleanup(os.close,slave)
            process=subprocess.Popen([BINARY,'--init-admin'],env=environment(data),stdin=slave,stdout=slave,stderr=slave,close_fds=True)
            output=b''
            password=secrets.token_urlsafe(24)
            def wait_for(text):
                nonlocal output
                until=time.monotonic()+5
                while text.encode() not in output:
                    self.assertTrue(time.monotonic()<until,'interactive prompt timeout')
                    if select.select([master],[],[],.1)[0]: output+=os.read(master,4096)
            try:
                wait_for('Administrator username:'); os.write(master,b'interactive_admin\n')
                wait_for('Password (12+ bytes):'); self.assertFalse(termios.tcgetattr(slave)[3]&termios.ECHO); os.write(master,(password+'\n').encode())
                wait_for('Confirm password:'); self.assertFalse(termios.tcgetattr(slave)[3]&termios.ECHO); os.write(master,(password+'\n').encode())
                process.wait(timeout=5)
                while select.select([master],[],[],.1)[0]: output+=os.read(master,4096)
                self.assertEqual(process.returncode,0)
                self.assertTrue(password.encode() not in output,'interactive password was echoed')
                self.assertTrue(termios.tcgetattr(slave)[3]&termios.ECHO)
            finally:
                if process.poll() is None: process.kill(); process.wait()
    def test_22_uninitialized_service_and_invalid_bootstrap(self):
        with tempfile.TemporaryDirectory() as data:
            invalid=initialize(data,'bad name','short')
            self.assertEqual(invalid.returncode,1)
            server=Server(data)
            try:
                self.assertEqual(server.request('GET','/api/bootstrap/status')[1],{'initialized':False})
                self.assertEqual(server.request('POST','/api/login',{'username':'administrator','password':self.password})[0],409)
                self.assertEqual(server.request('POST','/api/bootstrap',{})[0],405)
                self.assertEqual(initialize(data,'new_admin',secrets.token_urlsafe(24)).returncode,0)
                self.assertEqual(server.request('GET','/api/bootstrap/status')[1],{'initialized':True})
            finally: server.stop()
    def test_23_interactive_client_uses_cookie_csrf_and_logout(self):
        master,slave=pty.openpty(); self.addCleanup(os.close,master); self.addCleanup(os.close,slave)
        client=pathlib.Path(__file__).resolve().parent.parent/'scripts/auth-client.py'
        process=subprocess.Popen([sys.executable,str(client),'--url',self.server.base,'users'],stdin=slave,stdout=slave,stderr=slave,close_fds=True)
        output=b''
        def wait_for(text):
            nonlocal output
            until=time.monotonic()+5
            while text.encode() not in output:
                self.assertTrue(time.monotonic()<until,'client prompt timeout')
                if select.select([master],[],[],.1)[0]: output+=os.read(master,4096)
        try:
            wait_for('Login username:'); os.write(master,b'administrator\n')
            wait_for('Login password:'); os.write(master,(self.password+'\n').encode())
            process.wait(timeout=5)
            while select.select([master],[],[],.1)[0]: output+=os.read(master,4096)
            self.assertEqual(process.returncode,0)
            self.assertIn(b'"users"',output); self.assertIn(b'"role": "admin"',output)
            self.assertTrue(self.password.encode() not in output,'client echoed a password')
            self.assertEqual(self.db('SELECT count(*) FROM sessions')[0][0],1) # Only fixture session remains.
            self.assertTrue(termios.tcgetattr(slave)[3]&termios.ECHO)
        finally:
            if process.poll() is None: process.kill(); process.wait()

if __name__=='__main__': unittest.main(verbosity=2)
