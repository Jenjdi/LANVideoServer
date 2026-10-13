#!/usr/bin/env python3
"""Administrator-only actual DB snapshots and secret-free bounded audit pagination."""
import concurrent.futures, json, pathlib, tempfile, time, unittest
import media_integration as media

class MonitorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(); cls.video=pathlib.Path(cls.temp.name)/'sample.mp4'; media.generate_video(cls.video)
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()
    def setUp(self): self.f=media.Fixture(self.video); self.addCleanup(self.f.close)
    def get(self,path='/api/admin/status',expected=200,session=None):
        return self.f.call('GET',path,session=session or self.f.admin,expected=expected)[0]
    def test_01_anonymous_regular_user_and_forged_role_denied(self):
        viewer=self.f.user()
        for path in ['/api/admin/status','/api/admin/audit']:
            self.f.call('GET',path,expected=401)
            self.get(path,403,viewer)
            self.f.call('GET',path,expected=401,headers={'X-Role':'admin'})
    def test_02_snapshot_matches_real_database_and_clock(self):
        viewer=self.f.user(); state=self.get()
        self.assertEqual(state['version'],'1.0.0'); self.assertEqual(state['users']['total'],2); self.assertEqual(state['users']['enabled'],2)
        self.assertEqual(state['media']['count'],1); self.assertEqual(state['media']['catalog_bytes'],len(self.f.bytes)); self.assertEqual(state['tasks']['total'],0)
        self.assertEqual(state['sessions'],self.f.db('SELECT count(*) FROM sessions WHERE expires>?',(int(time.time()),))[0][0])
        self.assertEqual(state['audit_entries'],self.f.db('SELECT count(*) FROM audit')[0][0]); self.assertGreaterEqual(state['data_fs_free_bytes'],0)
        first=state['uptime_seconds']; time.sleep(1.05); self.assertGreater(self.get()['uptime_seconds'],first)
        self.f.call('PATCH',f'/api/users/{viewer["user"]["id"]}',{'disabled':True},self.f.admin)
        self.assertEqual(self.get()['users']['enabled'],1)
    def test_03_share_creation_and_revocation_counts(self):
        share=self.f.call('POST','/api/shares',{'media_id':self.f.item['id'],'ttl_seconds':3600},self.f.admin,expected=201)[0]
        self.assertEqual(self.get()['shares']['active'],1)
        self.f.call('POST','/share/api/exchange',{'token':share['url'].split('#')[1]})
        self.assertEqual(self.get()['shares']['play_sessions'],1)
        self.f.call('DELETE',f'/api/shares/{share["id"]}',session=self.f.admin)
        state=self.get(); self.assertEqual(state['shares']['active'],0); self.assertEqual(state['shares']['revoked'],1); self.assertEqual(state['shares']['play_sessions'],0)
    def test_04_audit_pagination_matches_storage_without_duplicates(self):
        expected=[row[0] for row in self.f.db('SELECT id FROM audit ORDER BY id DESC')]; collected=[]; before=None
        for _ in range(20):
            data=self.get('/api/admin/audit?limit=2'+('&before_id='+str(before) if before else ''))
            collected.extend(row['id'] for row in data['audit']); before=data['next_before_id']
            if before is None: break
        self.assertEqual(collected,expected); self.assertEqual(len(collected),len(set(collected)))
        self.assertEqual(self.get('/api/admin/audit?before_id=1')['audit'],[])
    def test_05_no_sensitive_values_in_status_or_audit(self):
        share=self.f.call('POST','/api/shares',{'media_id':self.f.item['id'],'ttl_seconds':3600},self.f.admin,expected=201)[0]
        result=json.dumps({'state':self.get(),'audit':self.get('/api/admin/audit')})
        for secret in [self.f.password,self.f.admin['token'],self.f.admin['csrf'],share['url'].split('#')[1]]: self.assertNotIn(secret,result)
        for key in ['password_hash','token_hash','proxy_password','resume']: self.assertNotIn(key,result)
        self.assertTrue(all(isinstance(row['detail'],dict) for row in self.get('/api/admin/audit')['audit']))
    def test_06_parameter_validation_and_bound(self):
        for query in ['limit=0','limit=101','limit=-1','limit=1;DROP+TABLE+users','before_id=-1','before_id=18446744073709551615','limit=2&limit=2','limit=2&limit=3','%6cimit=2&limit=2','before_id=1&before_id=1','other=1']:
            self.get('/api/admin/audit?'+query,400)
        self.get('/api/admin/status?anything=1',400)
        self.assertLessEqual(len(self.get('/api/admin/audit?limit=100')['audit']),100)
    def test_07_current_role_and_disable_checked_on_every_read(self):
        import secrets
        password=secrets.token_urlsafe(24)
        created=self.f.call('POST','/api/users',{'username':'other_admin','password':password,'role':'admin'},self.f.admin,expected=201)[0]['user']
        other=self.f.login('other_admin',password); self.get(session=other)
        self.f.call('PATCH',f'/api/users/{created["id"]}',{'role':'user'},self.f.admin)
        self.get(expected=401,session=other)
        regular=self.f.login('other_admin',password); self.get(expected=403,session=regular)
    def test_08_concurrent_bounded_reads_and_restart_persistence(self):
        before=self.get('/api/admin/audit')['audit']
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
            states=list(workers.map(lambda _:self.get(),range(16)))
        self.assertTrue(all(state['media']['count']==1 for state in states))
        self.f.server.stop(); self.f.server=media.auth.Server(self.f.data)
        self.assertEqual(self.get('/api/admin/audit')['audit'],before)

if __name__=='__main__': unittest.main(verbosity=2)
