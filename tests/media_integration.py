#!/usr/bin/env python3
"""Real video fixture, range byte comparisons, upload and resource ACL tests."""
import concurrent.futures
import hashlib
import http.client
import http.cookies
import json
import os
import pathlib
import secrets
import signal
import shutil
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
import auth_integration as auth
BINARY=auth.BINARY

def generate_video(path,seconds=12,width=320,height=180):
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f'testsrc2=size={width}x{height}:rate=25','-t',str(seconds),'-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-movflags','+faststart','-y',str(path)],check=True,timeout=30,capture_output=True)

class Fixture:
    def __init__(self,video,**overrides):
        self.temporary=tempfile.TemporaryDirectory(); self.data=pathlib.Path(self.temporary.name)
        self.root=self.data/'library'; self.root.mkdir()
        self.original=self.root/'sample.mp4'; shutil.copyfile(video,self.original)
        self.bytes=self.original.read_bytes(); self.password=secrets.token_urlsafe(24)
        if auth.initialize(self.data,'media_administrator',self.password).returncode!=0: raise AssertionError('media fixture initialization failed')
        self.server=auth.Server(self.data,**overrides)
        try:
            self.admin=self.login('media_administrator',self.password)
            self.call('POST','/api/media/root',{'path':str(self.root)},self.admin)
            self.call('POST','/api/media/scan',{},self.admin)
            self.item=self.call('GET','/api/media',session=self.admin)[0]['media'][0]
        except BaseException:
            self.server.stop(); self.temporary.cleanup(); raise
    def close(self): self.server.stop(); self.temporary.cleanup()
    def call(self,method,path,data=None,session=None,expected=200,**kwargs):
        code,result,headers=self.server.request(method,path,data,session,**kwargs)
        if code!=expected: raise AssertionError(f'{method} {path.split("?")[0]} expected {expected}, got {code}: {result.get("error","unknown")[:120]}')
        return result,headers
    def login(self,name,password):
        result,headers=self.call('POST','/api/login',{'username':name,'password':password})
        cookie=http.cookies.SimpleCookie(headers['Set-Cookie'])
        return {'token':cookie['lv_session'].value,'csrf':result['csrf_token'],'user':result['user']}
    def user(self,name='viewer'):
        password=secrets.token_urlsafe(24)
        result,_=self.call('POST','/api/users',{'username':name,'password':password},self.admin,expected=201)
        return self.login(name,password)
    def grant(self,user_ids): return self.call('POST',f'/api/media/{self.item["id"]}/grants',{'user_ids':user_ids},self.admin)
    def binary(self,method,path,payload=None,session=None,headers=None,header_only=False):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.port,timeout=10)
        values={}
        if session:
            values={'Cookie':'lv_session='+session['token'],'X-CSRF-Token':session['csrf']}
        values.update(headers or {})
        try:
            if header_only:
                connection.putrequest(method,path)
                for key,value in values.items(): connection.putheader(key,str(value))
                connection.endheaders()
            else: connection.request(method,path,body=payload,headers=values)
            response=connection.getresponse()
            return response.status,response.read(),dict(response.headers)
        finally: connection.close()
    def upload(self,name,video=None,session=None,headers=None):
        return self.binary('POST','/api/media/upload?filename='+urllib.parse.quote(name,safe=''),payload=self.bytes if video is None else video,session=self.admin if session is None else session,headers={'Content-Type':'application/octet-stream',**(headers or {})})
    def db(self,sql,values=()):
        with sqlite3.connect(self.data/'lanvideo.db') as db: return db.execute(sql,values).fetchall()

class MediaIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video_dir=tempfile.TemporaryDirectory(); cls.video=pathlib.Path(cls.video_dir.name)/'fixture.mp4'; generate_video(cls.video)
    @classmethod
    def tearDownClass(cls): cls.video_dir.cleanup()
    def setUp(self): self.f=Fixture(self.video); self.addCleanup(self.f.close)
    def raw(self,headers=None,session=None,method='GET',id=None):
        return self.f.binary(method,f'/media/{id or self.f.item["id"]}',session=self.f.admin if session is None else session,headers=headers)
    def test_01_scan_real_metadata_search_and_filter(self):
        item=self.f.item
        self.assertEqual(item['size'],len(self.f.bytes)); self.assertEqual(item['metadata_state'],'ok')
        self.assertAlmostEqual(item['duration'],12,places=1)
        self.assertEqual(self.f.call('GET','/api/media?q=sample&format=mp4',session=self.f.admin)[0]['total'],1)
        self.assertEqual(self.f.call('GET','/api/media?q=absent',session=self.f.admin)[0]['total'],0)
        self.assertEqual(self.f.call('GET','/api/media?format=webm',session=self.f.admin)[0]['total'],0)
        self.assertTrue({'root','relative','device','inode','modified'}.isdisjoint(item))
    def test_02_full_file_is_identical_and_decodable(self):
        status,content,headers=self.raw()
        self.assertEqual(status,200); self.assertEqual(content,self.f.bytes)
        self.assertEqual(int(headers['Content-Length']),len(content)); self.assertEqual(headers['Accept-Ranges'],'bytes')
        saved=self.f.data/'received.mp4'; saved.write_bytes(content)
        result=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(saved),'-threads','2','-f','null','-'],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0); self.assertEqual(result.stderr,b'')
    def test_03_range_exact_open_suffix_clamped_and_head(self):
        length=len(self.f.bytes)
        cases=[('bytes=0-63',0,64),('bytes=64-127',64,64),('bytes=100-',100,length-100),('bytes=-64',length-64,64),('bytes=-999999999',0,length),(f'bytes={length-32}-{length+300}',length-32,32)]
        for header,start,count in cases:
            with self.subTest(range=header):
                status,content,headers=self.raw({'Range':header})
                self.assertEqual(status,206); self.assertEqual(content,self.f.bytes[start:start+count])
                self.assertEqual(headers['Content-Range'],f'bytes {start}-{start+count-1}/{length}')
                self.assertEqual(int(headers['Content-Length']),count)
        status,content,headers=self.raw({'Range':'bytes=0-20'},method='HEAD')
        self.assertEqual(status,200); self.assertEqual(content,b''); self.assertEqual(int(headers['Content-Length']),length)
    def test_04_invalid_ranges_have_416_and_size(self):
        for header in [f'bytes={len(self.f.bytes)}-','bytes=10-5','bytes=-0','bytes=0-1,4-5','garbage','bytes=999999999999999999999999999-']:
            with self.subTest(range=header):
                status,content,headers=self.raw({'Range':header})
                self.assertEqual(status,416); self.assertEqual(headers['Content-Range'],f'bytes */{len(self.f.bytes)}')
                self.assertIn('error',json.loads(content))
    def test_05_if_range_falls_back_to_full_response(self):
        _,_,headers=self.raw(method='HEAD')
        self.assertEqual(self.raw({'Range':'bytes=0-20','If-Range':'stale'})[0],200)
        status,content,_=self.raw({'Range':'bytes=0-20','If-Range':headers['Last-Modified']})
        self.assertEqual(status,206); self.assertEqual(content,self.f.bytes[:21])
    def test_06_acl_hides_media_and_blocks_direct_streams(self):
        user=self.f.user()
        self.assertEqual(self.f.call('GET','/api/media',session=user)[0]['total'],0)
        self.assertEqual(self.raw(session=user)[0],403)
        self.assertEqual(self.raw({'Range':'invalid'},session=user)[0],403)
        self.assertEqual(self.raw(session={})[0],401)
        self.f.call('GET',f'/api/media/{self.f.item["id"]}',session=user,expected=403)
        self.f.grant([user['user']['id']])
        self.assertEqual(self.f.call('GET','/api/media',session=user)[0]['total'],1)
        self.assertEqual(self.raw(session=user)[1],self.f.bytes)
        self.f.grant([]); self.assertEqual(self.raw(session=user)[0],403)
    def test_07_media_management_is_admin_only_and_requires_csrf(self):
        user=self.f.user()
        for method,path,data in [('GET','/api/media/config',None),('POST','/api/media/root',{'path':str(self.f.root)}),('POST','/api/media/scan',{}),('GET',f'/api/media/{self.f.item["id"]}/grants',None),('POST',f'/api/media/{self.f.item["id"]}/grants',{'user_ids':[]})]:
            self.f.call(method,path,data,user,expected=403)
        self.f.call('POST','/api/media/scan',{},self.f.admin,csrf=False,expected=403)
        self.assertEqual(self.f.upload('denied.mp4',session=user)[0],403)
        self.assertFalse((self.f.root/'denied.mp4').exists())
    def test_08_upload_unicode_file_and_stream_bytes(self):
        status,content,_=self.f.upload('本地上传 演示.mp4')
        self.assertEqual(status,201); item=json.loads(content)['media']
        self.assertEqual(item['name'],'本地上传 演示.mp4'); self.assertEqual(item['metadata_state'],'ok')
        self.assertEqual((self.f.root/item['name']).read_bytes(),self.f.bytes)
        self.assertEqual(self.raw(id=item['id'])[1],self.f.bytes)
        self.assertEqual(list(self.f.root.glob('.upload-*')),[])
        self.assertEqual(self.f.db("SELECT count(*) FROM audit WHERE event='media_uploaded'")[0][0],1)
    def test_09_upload_larger_than_old_8_mib_limit(self):
        extra=9*1024*1024-len(self.f.bytes)
        content=self.f.bytes+struct.pack('>I',extra)+b'free'+b'\0'*(extra-8)
        status,body,_=self.f.upload('large.mp4',content)
        self.assertEqual(status,201); item=json.loads(body)['media']
        self.assertEqual(item['size'],len(content)); self.assertEqual(item['metadata_state'],'ok')
        self.assertEqual(hashlib.sha256((self.f.root/'large.mp4').read_bytes()).digest(),hashlib.sha256(content).digest())
    def test_10_duplicate_upload_does_not_overwrite(self):
        before=self.f.original.stat()
        self.assertEqual(self.f.upload('sample.mp4')[0],409)
        self.assertEqual(self.f.original.stat().st_ino,before.st_ino)
        self.assertEqual(self.f.original.read_bytes(),self.f.bytes)
        self.assertEqual(list(self.f.root.glob('.upload-*')),[])
    def test_11_upload_path_and_format_attacks(self):
        for name,code in [('../escape.mp4',400),('/tmp/escape.mp4',400),('folder\\escape.mp4',400),('.hidden.mp4',400),('drive:video.mp4',400),('bad\nname.mp4',400),('bad\0name.mp4',400),('script.html',415),('a'*201+'.mp4',400)]:
            with self.subTest(name=name): self.assertEqual(self.f.upload(name)[0],code)
        self.assertEqual(self.f.upload('fake.mp4',b'this is not a video')[0],415)
        self.assertEqual(self.f.upload('empty.mp4',b'')[0],400)
        self.assertEqual(list(self.f.root.glob('.upload-*')),[])
        self.assertEqual(self.f.call('GET','/api/media',session=self.f.admin)[0]['total'],1)
    def test_12_upload_header_limits_reject_before_body(self):
        path='/api/media/upload?filename=huge.mp4'
        headers={'Content-Type':'application/octet-stream','Content-Length':str(513*1024*1024)}
        status,_,_=self.f.binary('POST',path,session=self.f.admin,headers=headers,header_only=True)
        self.assertEqual(status,413)
        status,_,_=self.f.binary('POST',path,session=self.f.admin,headers={'Content-Type':'application/octet-stream','Transfer-Encoding':'chunked'},header_only=True)
        self.assertEqual(status,411)
        status,_,_=self.f.binary('POST',path,session=self.f.admin,headers={'Content-Type':'text/plain','Content-Length':'200'},header_only=True)
        self.assertEqual(status,415)
        status,_,_=self.f.binary('POST',path,headers={'Content-Type':'application/octet-stream','Content-Length':'200'},header_only=True)
        self.assertEqual(status,401)
        status,_,_=self.f.binary('POST',path,session=self.f.admin,headers={'Content-Type':'application/octet-stream','Content-Length':'200','X-CSRF-Token':'wrong'},header_only=True)
        self.assertEqual(status,403)
        self.assertFalse((self.f.root/'huge.mp4').exists())
    def test_13_interrupted_upload_is_cleaned_and_server_survives(self):
        sock=socket.create_connection(('127.0.0.1',self.f.server.port),timeout=3)
        header=f'POST /api/media/upload?filename=interrupted.mp4 HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: lv_session={self.f.admin["token"]}\r\nX-CSRF-Token: {self.f.admin["csrf"]}\r\nContent-Type: application/octet-stream\r\nContent-Length: 500000\r\n\r\n'
        sock.sendall(header.encode()+self.f.bytes[:4096])
        for _ in range(100):
            if list(self.f.root.glob('.upload-*')): break
            time.sleep(.01)
        else: sock.close(); self.fail('upload never entered staging state')
        sock.shutdown(socket.SHUT_WR); sock.close()
        for _ in range(100):
            if not list(self.f.root.glob('.upload-*')):
                time.sleep(.02)
                code,_,_=self.f.server.request('POST','/api/media/scan',{},self.f.admin)
                if code==200: break
            time.sleep(.02)
        else: self.fail('interrupted upload remained active')
        self.assertFalse((self.f.root/'interrupted.mp4').exists())
        self.assertEqual(list(self.f.root.glob('.upload-*')),[])
        self.assertEqual(self.f.call('GET','/api/health')[0]['status'],'ok')
    def test_14_symlink_files_directories_and_replacement_are_rejected(self):
        outside=self.f.data/'outside.mp4'; outside.write_bytes(self.f.bytes)
        (self.f.root/'outside.mp4').symlink_to(outside)
        outside_dir=self.f.data/'outside_dir'; outside_dir.mkdir(); shutil.copyfile(self.video,outside_dir/'secret.mp4')
        (self.f.root/'linked_dir').symlink_to(outside_dir,target_is_directory=True)
        result,_=self.f.call('POST','/api/media/scan',{},self.f.admin)
        self.assertEqual(result['indexed'],1); self.assertGreaterEqual(result['skipped'],2)
        self.f.original.unlink(); self.f.original.symlink_to(outside)
        self.assertEqual(self.raw()[0],403)
        self.assertEqual(self.f.upload('outside.mp4')[0],409)
        self.assertEqual(outside.read_bytes(),self.f.bytes)
    def test_15_missing_and_changed_files_require_rescan(self):
        user=self.f.user(); self.f.grant([user['user']['id']])
        self.f.original.write_bytes(self.f.bytes+b'changed')
        self.assertEqual(self.raw(session=user)[0],409)
        self.f.call('POST','/api/media/scan',{},self.f.admin)
        self.assertEqual(self.f.call('GET','/api/media',session=user)[0]['total'],0)
        new=self.f.call('GET','/api/media',session=self.f.admin)[0]['media'][0]
        self.assertNotEqual(new['id'],self.f.item['id'])
        self.assertEqual(self.raw()[0],404)
        self.f.original.unlink(); self.assertEqual(self.raw(id=new['id'])[0],404)
        removed,_=self.f.call('POST','/api/media/scan',{},self.f.admin)
        self.assertEqual(removed['removed'],1); self.assertEqual(removed['indexed'],0)
    def test_16_tampered_db_paths_cannot_escape_root(self):
        outside=self.f.data/'outside.mp4'; outside.write_bytes(self.f.bytes)
        self.f.db('UPDATE media SET relative=? WHERE id=?',('../outside.mp4',self.f.item['id']))
        self.assertEqual(self.raw()[0],403)
        self.f.db('UPDATE media SET relative=? WHERE id=?',(str(outside),self.f.item['id']))
        self.assertEqual(self.raw()[0],403)
    def test_17_history_is_private_persistent_and_revoked(self):
        one=self.f.user('viewer_one'); two=self.f.user('viewer_two'); self.f.grant([one['user']['id'],two['user']['id']])
        self.f.call('POST','/api/history',{'media_id':self.f.item['id'],'position':3.25},one)
        self.assertEqual(self.f.call('GET','/api/history',session=one)[0]['history'][0]['position'],3.25)
        self.assertEqual(self.f.call('GET','/api/history',session=two)[0]['history'],[])
        self.f.server.stop(); self.f.server=auth.Server(self.f.data)
        self.assertEqual(self.f.call('GET','/api/media',session=one)[0]['media'][0]['position'],3.25)
        self.f.grant([two['user']['id']]); self.assertEqual(self.f.call('GET','/api/history',session=one)[0]['history'],[])
        self.f.call('POST','/api/history',{'media_id':self.f.item['id'],'position':1},one,expected=403)
    def test_18_invalid_history_and_grant_inputs(self):
        user=self.f.user()
        self.f.call('GET','/api/history?user_id=1',session=user,expected=400)
        self.f.call('POST','/api/history',{'media_id':self.f.item['id'],'position':1},user,expected=403)
        self.f.grant([user['user']['id']])
        for position in [-1,13.5,'3',True,None,3000000]:
            self.f.call('POST','/api/history',{'media_id':self.f.item['id'],'position':position},user,expected=400)
        self.f.call('POST','/api/history',{'media_id':self.f.item['id'],'position':1,'user_id':1},user,expected=400)
        for ids in [[user['user']['id'],user['user']['id']],[999999],[1],['2'],[True],[2**64-1]]:
            self.f.call('POST',f'/api/media/{self.f.item["id"]}/grants',{'user_ids':ids},self.f.admin,expected=400)
        self.assertEqual(self.f.call('GET',f'/api/media/{self.f.item["id"]}/grants',session=self.f.admin)[0]['user_ids'],[user['user']['id']])
    def test_19_root_change_clears_index_and_acl_without_deleting_files(self):
        user=self.f.user(); self.f.grant([user['user']['id']])
        self.f.call('POST','/api/history',{'media_id':self.f.item['id'],'position':1},user)
        other=self.f.data/'other_root'; other.mkdir(); shutil.copyfile(self.video,other/'other.mp4')
        self.f.call('POST','/api/media/root',{'path':str(other)},self.f.admin)
        self.assertTrue(self.f.original.exists()); self.assertEqual(self.raw()[0],404)
        self.assertEqual(self.f.db('SELECT count(*) FROM history')[0][0],0)
        self.f.call('POST','/api/media/scan',{},self.f.admin)
        self.assertEqual(self.f.call('GET','/api/media',session=user)[0]['total'],0)
    def test_20_zero_file_and_no_ffprobe_fallback(self):
        self.f.server.stop(); self.f.server=auth.Server(self.f.data,LANVIDEO_FFPROBE='')
        empty=self.f.root/'zero.mp4'; empty.touch()
        self.f.call('POST','/api/media/scan',{},self.f.admin)
        zero=self.f.call('GET','/api/media?q=zero',session=self.f.admin)[0]['media'][0]
        self.assertIsNone(zero['duration']); self.assertEqual(zero['metadata_state'],'unavailable')
        self.assertEqual(self.raw(id=zero['id'])[0],200)
        self.assertEqual(self.raw({'Range':'bytes=0-'},id=zero['id'])[0],416)
        code,body,_=self.f.upload('without_probe.mp4'); self.assertEqual(code,201)
        self.assertIsNone(json.loads(body)['media']['duration'])
    def test_21_concurrent_ranges_and_static_asset_scope(self):
        def read(index): return self.raw({'Range':f'bytes={index*16}-{index*16+15}'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(read,range(32)))
        for index,(status,content,_) in enumerate(results): self.assertEqual(status,206); self.assertEqual(content,self.f.bytes[index*16:index*16+16])
        status,content,headers=self.f.binary('GET','/')
        self.assertEqual(status,200); self.assertIn(b'<video',content); self.assertIn("script-src 'self'",headers['Content-Security-Policy'])
        for path in ['/src/auth.cpp','/.env','/web/../src/auth.cpp','/media/../outside.mp4']:
            self.assertEqual(self.f.binary('GET',path)[0],404)
    def test_22_disconnect_during_stream_keeps_service_healthy(self):
        sock=socket.create_connection(('127.0.0.1',self.f.server.port),timeout=3)
        sock.sendall(f'GET /media/{self.f.item["id"]} HTTP/1.1\r\nHost: local\r\nCookie: lv_session={self.f.admin["token"]}\r\n\r\n'.encode())
        sock.recv(256); sock.close()
        self.assertEqual(self.f.call('GET','/api/health')[0]['status'],'ok')
    def test_23_parent_symlink_switch_cannot_redirect_reads_or_uploads(self):
        parent=self.f.root/'parent'; nested=parent/'clips'; nested.mkdir(parents=True)
        shutil.copyfile(self.video,nested/'protected.mp4')
        self.f.call('POST','/api/media/root',{'path':str(nested)},self.f.admin)
        self.f.call('POST','/api/media/scan',{},self.f.admin)
        item=self.f.call('GET','/api/media',session=self.f.admin)[0]['media'][0]
        outside=self.f.data/'replacement'; (outside/'clips').mkdir(parents=True); shutil.copyfile(self.video,outside/'clips'/'protected.mp4')
        parent.rename(self.f.root/'old_parent'); parent.symlink_to(outside,target_is_directory=True)
        self.assertEqual(self.raw(id=item['id'])[0],403)
        self.assertEqual(self.f.upload('redirected.mp4')[0],403)
        self.assertFalse((outside/'clips'/'redirected.mp4').exists())
    def test_24_upload_never_inherits_stale_file_grants(self):
        user=self.f.user(); self.f.grant([user['user']['id']]); self.f.original.unlink()
        status,body,_=self.f.upload('sample.mp4')
        self.assertEqual(status,201); self.assertNotEqual(json.loads(body)['media']['id'],self.f.item['id'])
        self.assertEqual(self.f.call('GET','/api/media',session=user)[0]['total'],0)
        self.assertEqual(self.raw(session=user)[0],404)
    def test_25_disconnect_during_real_probe_prevents_publication(self):
        self.f.server.stop()
        wrapper=self.f.data/'delayed_probe.py'
        wrapper.write_text('#!/usr/bin/python3\nimport os,sys,time\ntime.sleep(1)\nos.execv("/usr/bin/ffprobe",["/usr/bin/ffprobe"]+sys.argv[1:])\n')
        wrapper.chmod(0o700)
        self.f.server=auth.Server(self.f.data,LANVIDEO_FFPROBE=str(wrapper))
        sock=socket.create_connection(('127.0.0.1',self.f.server.port),timeout=3)
        header=f'POST /api/media/upload?filename=probe_cancelled.mp4 HTTP/1.1\r\nHost: local\r\nCookie: lv_session={self.f.admin["token"]}\r\nX-CSRF-Token: {self.f.admin["csrf"]}\r\nContent-Type: application/octet-stream\r\nContent-Length: {len(self.f.bytes)}\r\n\r\n'
        sock.sendall(header.encode()+self.f.bytes)
        for _ in range(100):
            staged=list(self.f.root.glob('.upload-*'))
            if staged and staged[0].stat().st_size==len(self.f.bytes): break
            time.sleep(.01)
        else: sock.close(); self.fail('upload did not reach complete staging')
        sock.close()
        for _ in range(200):
            if not list(self.f.root.glob('.upload-*')): break
            time.sleep(.02)
        else: self.fail('disconnected probe left staging data')
        self.assertFalse((self.f.root/'probe_cancelled.mp4').exists())
        self.assertEqual(self.f.db("SELECT count(*) FROM media WHERE name='probe_cancelled.mp4'")[0][0],0)
    def test_26_shutdown_during_partial_upload_is_bounded_and_clean(self):
        sock=socket.create_connection(('127.0.0.1',self.f.server.port),timeout=3)
        self.addCleanup(sock.close)
        header=f'POST /api/media/upload?filename=shutdown.mp4 HTTP/1.1\r\nHost: local\r\nCookie: lv_session={self.f.admin["token"]}\r\nX-CSRF-Token: {self.f.admin["csrf"]}\r\nContent-Type: application/octet-stream\r\nContent-Length: 500000\r\n\r\n'
        sock.sendall(header.encode()+self.f.bytes[:4096])
        for _ in range(100):
            if list(self.f.root.glob('.upload-*')): break
            time.sleep(.01)
        else: self.fail('upload did not start')
        self.f.server.process.send_signal(signal.SIGTERM)
        self.f.server.process.wait(timeout=15)
        self.assertEqual(self.f.server.process.returncode,0)
        self.assertFalse((self.f.root/'shutdown.mp4').exists())
        self.assertEqual(list(self.f.root.glob('.upload-*')),[])

if __name__=='__main__': unittest.main(verbosity=2)
