#!/usr/bin/env python3
"""Resumable upload: real filesystem, checksums, restart and permission boundaries."""
import hashlib
import json
import pathlib
import socket
import tempfile
import unittest
import media_integration as media

class UploadIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(); cls.video=pathlib.Path(cls.tmp.name)/'fixture.mp4'; media.generate_video(cls.video)
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    def setUp(self):
        self.f=media.Fixture(self.video); self.addCleanup(self.f.close)
        self.bytes=self.f.bytes+b'\0'*(3*1024*1024)
        self.chunks=[self.bytes[i:i+1048576] for i in range(0,len(self.bytes),1048576)]
        self.hashes=[hashlib.sha256(c).hexdigest() for c in self.chunks]
        self.row=self.f.call('POST','/api/uploads',{'filename':'resume.mp4','size':len(self.bytes),'hashes':self.hashes},self.f.admin,expected=201)[0]
        self.path='/api/uploads/'+self.row['id']
    def chunk(self,index,content=None,expected=200,session=None):
        status,data,_=self.f.binary('PUT',self.path+'/chunk',self.chunks[index] if content is None else content,self.f.admin if session is None else session,{'Content-Type':'application/octet-stream','Upload-Offset':str(index*1048576)})
        self.assertEqual(status,expected,data[:120]); return json.loads(data)
    def test_01_restart_retry_complete_and_exact_bytes(self):
        self.assertEqual(self.chunk(0)['offset'],1048576)
        self.assertEqual(self.chunk(0)['offset'],1048576)
        self.f.server.stop(); self.f.server=media.auth.Server(self.f.data)
        self.assertEqual(self.f.call('GET',self.path,session=self.f.admin)[0]['offset'],1048576)
        for i in range(1,len(self.chunks)): self.chunk(i)
        value=self.f.call('POST',self.path+'/complete',{},self.f.admin,expected=201)[0]
        self.assertEqual(value['sha256'],hashlib.sha256(self.bytes).hexdigest())
        self.assertEqual((self.f.root/'resume.mp4').read_bytes(),self.bytes)
        second=self.f.call('POST',self.path+'/complete',{},self.f.admin,expected=201)[0]
        self.assertEqual(second,value)
        self.assertEqual(self.f.binary('GET',f'/media/{value["media"]["id"]}',session=self.f.admin)[1],self.bytes)
    def test_02_checksum_offset_and_unfinished_publication(self):
        bad=bytearray(self.chunks[0]); bad[50]^=1
        self.chunk(0,bytes(bad),422); self.chunk(1,expected=409)
        self.f.call('POST',self.path+'/complete',{},self.f.admin,expected=409)
        self.assertFalse((self.f.root/'resume.mp4').exists())
        self.assertEqual(self.f.call('GET',self.path,session=self.f.admin)[0]['offset'],0)
    def test_03_permissions_csrf_and_owner(self):
        viewer=self.f.user(); self.chunk(0,session=viewer,expected=403)
        self.f.call('GET',self.path,session=viewer,expected=403)
        self.f.call('POST','/api/uploads',{'filename':'x.mp4','size':1,'hashes':['0'*64]},viewer,expected=403)
        self.f.call('PATCH',f'/api/users/{viewer["user"]["id"]}',{'role':'admin'},self.f.admin)
        # Reuse normal creation with a second known-password administrator.
        password=media.secrets.token_urlsafe(24)
        self.f.call('POST','/api/users',{'username':'other_admin','password':password,'role':'admin'},self.f.admin,expected=201)
        other=self.f.login('other_admin',password)
        self.f.call('GET',self.path,session=other,expected=404)
        self.f.call('DELETE',self.path,session=other,expected=404)
        self.f.call('POST',self.path+'/complete',{},self.f.admin,csrf=False,expected=403)
    def test_04_unacknowledged_bytes_and_disconnect(self):
        self.chunk(0)
        stage=self.f.root/('.resume-'+self.row['id'])
        with stage.open('ab') as stream: stream.write(b'unacknowledged')
        self.f.call('GET',self.path,session=self.f.admin)
        self.assertEqual(stage.stat().st_size,1048576)
        connection=socket.create_connection(('127.0.0.1',self.f.server.port))
        headers=f'PUT {self.path}/chunk HTTP/1.1\r\nHost: localhost\r\nCookie: lv_session={self.f.admin["token"]}\r\nX-CSRF-Token: {self.f.admin["csrf"]}\r\nContent-Type: application/octet-stream\r\nContent-Length: 1048576\r\nUpload-Offset: 1048576\r\n\r\n'.encode()
        connection.sendall(headers+self.chunks[1][:4000]); connection.close()
        self.assertEqual(self.f.call('GET',self.path,session=self.f.admin)[0]['offset'],1048576)
        self.chunk(1)
    def test_05_root_change_discard_and_expiry(self):
        other=self.f.data/'other'; other.mkdir()
        self.f.call('POST','/api/media/root',{'path':str(other)},self.f.admin)
        self.f.call('GET',self.path,session=self.f.admin,expected=409)
        self.f.call('DELETE',self.path,session=self.f.admin)
        self.assertFalse((self.f.root/('.resume-'+self.row['id'])).exists())
        self.f.call('GET',self.path,session=self.f.admin,expected=404)
    def test_06_corrupt_staging_and_symlinks(self):
        for i in range(len(self.chunks)): self.chunk(i)
        stage=self.f.root/('.resume-'+self.row['id'])
        with stage.open('r+b') as stream: stream.seek(100); stream.write(b'bad')
        self.f.call('POST',self.path+'/complete',{},self.f.admin,expected=422)
        self.assertFalse((self.f.root/'resume.mp4').exists())
        stage.unlink(); stage.symlink_to(self.f.root/'sample.mp4')
        self.f.call('GET',self.path,session=self.f.admin,expected=409)
    def test_07_manifest_validation_and_expiration(self):
        for data,status in [({'filename':'../evil.mp4','size':1,'hashes':['0'*64]},400),({'filename':'x.mp4','size':1,'hashes':[]},400),({'filename':'x.mp4','size':1,'hashes':['z'*64]},400)]:
            self.f.call('POST','/api/uploads',data,self.f.admin,expected=status)
        self.f.db('UPDATE uploads SET expires=0 WHERE id=?',(self.row['id'],))
        self.f.call('GET',self.path,session=self.f.admin,expected=410)
        self.f.server.stop(); self.f.server=media.auth.Server(self.f.data)
        self.assertFalse((self.f.root/('.resume-'+self.row['id'])).exists())

if __name__=='__main__': unittest.main(verbosity=2)
