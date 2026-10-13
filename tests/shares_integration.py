#!/usr/bin/env python3
"""Real scoped share sessions, media bytes, path/role revocation and wall-clock expiry."""
import http.cookies, http.client, json, pathlib, secrets, socket, time, unittest
import media_integration as media

class SharesIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.temp=tempfile.TemporaryDirectory(); cls.video=pathlib.Path(cls.temp.name)/'sample.mp4'; media.generate_video(cls.video)
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()
    def setUp(self): self.f=media.Fixture(self.video); self.addCleanup(self.f.close)
    def create(self,ttl=3600,session=None,**overrides):
        value={'media_id':self.f.item['id'],'ttl_seconds':ttl,'scope':'video_read',**overrides}
        return self.f.call('POST','/api/shares',value,session or self.f.admin,expected=201)[0]
    def exchange(self,share,expected=200):
        result,headers=self.f.call('POST','/share/api/exchange',{'token':share['url'].split('#')[1]},expected=expected)
        if expected!=200: return None
        cookie=http.cookies.SimpleCookie(headers['Set-Cookie'])['lv_share']
        self.assertEqual(cookie['path'],'/share/'); self.assertTrue(cookie['httponly']); self.assertEqual(cookie['samesite'],'Strict')
        return cookie.value
    def read(self,cookie=None,headers=None,method='GET',path='/share/media'):
        return self.f.binary(method,path,headers={**({'Cookie':'lv_share='+cookie} if cookie else {}),**(headers or {})})
    def test_01_admin_only_and_single_video_scope(self):
        self.f.call('POST','/api/shares',{'media_id':self.f.item['id'],'ttl_seconds':3600},expected=401)
        user=self.f.user(); self.f.grant([user['user']['id']])
        self.f.call('POST','/api/shares',{'media_id':self.f.item['id'],'ttl_seconds':3600},user,expected=403)
        cookie=self.exchange(self.create())
        code,value,_=self.read(cookie,path='/api/media'); self.assertEqual(code,401)
        self.assertEqual(self.read(cookie,path='/media/'+str(self.f.item['id']))[0],401)
        self.assertEqual(self.read(cookie,path='/api/users')[0],401)
        self.assertEqual(self.read(cookie,path='/share/media?media_id=999')[0],400)
        code,value,_=self.read(cookie,path='/share/api/info'); self.assertEqual(code,200)
        self.assertEqual(json.loads(value)['name'],'sample.mp4'); self.assertNotIn('root',json.loads(value))
    def test_02_actual_full_range_suffix_head_invalid_and_restart(self):
        share=self.create(); cookie=self.exchange(share)
        code,value,headers=self.read(cookie); self.assertEqual(code,200); self.assertEqual(value,self.f.bytes)
        for selected,expected in [('bytes=101-70001',self.f.bytes[101:70002]),('bytes=-500',self.f.bytes[-500:]),('bytes=100-',self.f.bytes[100:])]:
            code,value,headers=self.read(cookie,{'Range':selected}); self.assertEqual(code,206); self.assertEqual(value,expected)
        self.assertEqual(self.read(cookie,{'Range':'bytes=1-2'},method='HEAD')[0],200)
        code,_,headers=self.read(cookie,{'Range':'bytes=999999999-'}); self.assertEqual(code,416); self.assertEqual(headers['Content-Range'],f'bytes */{len(self.f.bytes)}')
        self.assertEqual(self.read(cookie,{'Range':'garbage'})[0],416)
        self.assertEqual(self.read(headers={'Range':'garbage'})[0],401)
        self.f.server.stop(); self.f.server=media.auth.Server(self.f.data)
        self.assertEqual(self.read(cookie,{'Range':'bytes=1-100'})[1],self.f.bytes[1:101])
    def test_03_revoke_and_reexchange_fail_and_no_secret_storage(self):
        share=self.create(); raw=share['url'].split('#')[1]; cookie=self.exchange(share)
        self.assertNotIn(raw,self.f.server.logpath.read_text())
        self.assertNotIn(cookie,self.f.server.logpath.read_text())
        self.assertNotEqual(self.f.db('SELECT token_hash FROM shares')[0][0],raw)
        self.assertNotEqual(self.f.db('SELECT token_hash FROM share_sessions')[0][0],cookie)
        self.assertNotIn(raw,str(self.f.db('SELECT detail FROM audit')))
        listed=self.f.call('GET','/api/shares',session=self.f.admin)[0]
        self.assertNotIn(raw,json.dumps(listed)); self.assertNotIn('token_hash',json.dumps(listed))
        self.f.call('DELETE',f'/api/shares/{share["id"]}',session=self.f.admin)
        self.assertEqual(self.read(cookie)[0],404); self.exchange(share,404)
        self.assertEqual(self.f.db('SELECT count(*) FROM share_sessions')[0][0],0)
    def test_04_creator_disable_demotion_deletion_invalidates_shares(self):
        password=secrets.token_urlsafe(24)
        user=self.f.call('POST','/api/users',{'username':'second_admin','password':password,'role':'admin'},self.f.admin,expected=201)[0]['user']
        issuer=self.f.login('second_admin',password)
        share=self.create(session=issuer); cookie=self.exchange(share)
        self.f.call('PATCH',f'/api/users/{user["id"]}',{'disabled':True},self.f.admin)
        self.assertEqual(self.read(cookie)[0],404); self.exchange(share,404)
        self.f.call('PATCH',f'/api/users/{user["id"]}',{'disabled':False},self.f.admin)
        self.assertEqual(self.read(cookie)[0],404); self.exchange(share,404)
        self.f.call('PATCH',f'/api/users/{user["id"]}',{'disabled':False,'role':'user'},self.f.admin)
        self.assertEqual(self.read(cookie)[0],404)
        self.f.call('DELETE',f'/api/users/{user["id"]}',session=self.f.admin)
        self.assertEqual(self.read(cookie)[0],404); self.assertEqual(self.f.db('SELECT count(*) FROM shares')[0][0],0)
    def test_05_file_replacement_symlink_rescan_and_root_change(self):
        share=self.create(); cookie=self.exchange(share)
        self.f.original.rename(self.f.root/'old.mp4'); self.f.original.symlink_to(self.video)
        self.assertIn(self.read(cookie)[0],(403,404)); self.f.original.unlink(); self.f.original.write_bytes(self.f.bytes)
        self.assertEqual(self.read(cookie)[0],409)
        self.f.call('POST','/api/media/scan',{},self.f.admin)
        self.assertEqual(self.read(cookie)[0],404); self.exchange(share,404)
        self.f.item=self.f.call('GET','/api/media',session=self.f.admin)[0]['media'][0]
        cookie=self.exchange(self.create()); other=self.f.data/'other'; other.mkdir()
        self.f.call('POST','/api/media/root',{'path':str(other)},self.f.admin)
        self.assertEqual(self.read(cookie)[0],404)
    def test_06_input_csrf_origin_and_cookie_validation(self):
        self.f.call('POST','/api/shares',{'media_id':self.f.item['id'],'ttl_seconds':3600},self.f.admin,expected=403,csrf=False)
        for values in [{'ttl_seconds':29},{'ttl_seconds':604801},{'media_id':-1},{'scope':'all'},{'other':'x'},{'media_id':2**64-1},{'ttl_seconds':'60'}]:
            self.f.call('POST','/api/shares',{'media_id':self.f.item['id'],'ttl_seconds':3600,**values},self.f.admin,expected=400)
        for token in ['x','../'+secrets.token_hex(32),secrets.token_hex(32)]: self.f.call('POST','/share/api/exchange',{'token':token},expected=404)
        cookie=self.exchange(self.create())
        self.assertEqual(self.read(cookie,{'Authorization':'Bearer '+cookie})[0],400)
        self.assertEqual(self.read(cookie,{'Cookie':'lv_share='+cookie+'; lv_share='+cookie})[0],400)
        self.f.call('POST','/share/api/exchange',{'token':'x','scope':'admin'},expected=400)
    def test_07_real_wall_clock_expiry(self):
        share=self.create(ttl=30); cookie=self.exchange(share)
        self.assertEqual(self.read(cookie,{'Range':'bytes=0-30'})[0],206)
        # Real elapsed expiry, not a mocked clock or database injection.
        remaining=share['expires']-time.time()+.15
        if remaining>0: time.sleep(remaining)
        self.assertEqual(self.read(cookie)[0],404); self.exchange(share,404)
    def test_08_session_limit_and_missing_file(self):
        share=self.create()
        for _ in range(64): self.exchange(share)
        self.exchange(share,429)
        cookie=self.exchange(self.create()); self.f.original.unlink()
        self.assertEqual(self.read(cookie)[0],404)
    def test_09_live_stream_revoke_stops_bounded_writes(self):
        with self.f.original.open('ab') as target: target.write(bytes(32*1024*1024))
        self.f.call('POST','/api/media/scan',{},self.f.admin)
        self.f.item=self.f.call('GET','/api/media',session=self.f.admin)[0]['media'][0]
        share=self.create(); cookie=self.exchange(share)
        sock=socket.socket(); sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,16384); sock.settimeout(15)
        self.addCleanup(sock.close); sock.connect(('127.0.0.1',self.f.server.port))
        sock.sendall(('GET /share/media HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nCookie: lv_share='+cookie+'\r\n\r\n').encode())
        first=sock.recv(4096); self.assertIn(b'200 OK',first)
        self.f.call('DELETE',f'/api/shares/{share["id"]}',session=self.f.admin)
        received=len(first)
        while True:
            data=sock.recv(65536)
            if not data: break
            received+=len(data)
        self.assertLess(received,self.f.item['size']); self.assertGreater(received,0)
        self.assertEqual(self.f.call('GET','/api/health')[0]['status'],'ok')
    def test_10_password_reset_revokes_delegated_links_permanently(self):
        share=self.create(); cookie=self.exchange(share)
        self.f.call('POST',f'/api/users/{self.f.admin["user"]["id"]}/password',{'password':secrets.token_urlsafe(24)},self.f.admin)
        self.assertEqual(self.read(cookie)[0],404); self.exchange(share,404)

if __name__=='__main__': unittest.main(verbosity=2)
