#!/usr/bin/env python3
"""Real Chromium native video playback, seek/resume and browser file upload.

Runs solely against this script's generated local fixture. Chromium's sandbox
helper is unavailable in this cloud container; sandbox is disabled only here.
No production browser/deployment flags are changed. Metrics are measured.
"""
import json
import datetime
import hashlib
import pathlib
import secrets
import tempfile
import time
import unittest
from playwright.sync_api import sync_playwright
import media_integration as media

class BrowserIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory(); cls.video=pathlib.Path(cls.directory.name)/'fixture.mp4'; media.generate_video(cls.video)
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,chromium_sandbox=False,args=['--autoplay-policy=no-user-gesture-required'])
        cls.artifacts=pathlib.Path(media.BINARY).resolve().parent/'Testing'/'artifacts'; cls.artifacts.mkdir(parents=True,exist_ok=True)
        cls.evidence={'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'fixture_sha256':hashlib.sha256(cls.video.read_bytes()).hexdigest(),'browser':cls.browser.version,'sandbox':False,'sandbox_scope':'generated local test fixture only','tests':[]}
    @classmethod
    def tearDownClass(cls):
        (cls.artifacts/'browser-evidence.json').write_text(json.dumps(cls.evidence,ensure_ascii=False,indent=2))
        cls.browser.close(); cls.playwright.stop(); cls.directory.cleanup()
    def setUp(self):
        self.f=media.Fixture(self.video); self.addCleanup(self.f.close)
        self.context=self.browser.new_context(viewport={'width':1280,'height':900}); self.addCleanup(self.context.close)
        self.external=[]
        def local_only(route):
            if route.request.url.startswith(self.f.server.base+'/'): route.continue_()
            else: self.external.append('unexpected external request'); route.abort()
        self.context.route('**/*',local_only)
        self.page=self.context.new_page(); self.errors=[]; self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.ranges=[]
        def response_received(response):
            if response.url.startswith(self.f.server.base+'/media/'):
                self.ranges.append({'status':response.status,'range':response.request.header_value('range'),'content_range':response.headers.get('content-range')})
        self.page.on('response',response_received)
        self.page.goto(self.f.server.base,wait_until='networkidle')
    def login(self,username,password):
        self.page.locator('#username').fill(username); self.page.locator('#password').fill(password)
        self.page.locator('#login-form button').click(); self.page.locator('#workspace').wait_for(state='visible')
        self.page.wait_for_function("() => document.getElementById('total').textContent.includes('个可访问视频')")
    def card(self,name): return self.page.locator('.card').filter(has=self.page.get_by_role('heading',name=name,exact=True))
    def assert_clean(self): self.assertEqual(self.errors,[]); self.assertEqual(self.external,[])
    def test_01_local_upload_authorize_native_play_seek_resume(self):
        password=secrets.token_urlsafe(24)
        viewer=self.f.call('POST','/api/users',{'username':'browser_viewer','password':password},self.f.admin,expected=201)[0]['user']
        self.login('media_administrator',self.f.password)
        self.page.locator('#admin-tab').click(); self.page.locator('#administration').wait_for(state='visible')
        self.page.locator('#upload-file').set_input_files(self.video)
        self.page.locator('#upload-button').click()
        self.page.wait_for_function("() => document.getElementById('upload-status').textContent.includes('上传成功')")
        self.assertEqual((self.f.root/'fixture.mp4').read_bytes(),self.f.bytes)
        self.assertEqual(self.page.locator('#upload-progress').get_attribute('value'),'100')
        self.page.screenshot(path=str(self.artifacts/'admin-upload.png'),full_page=True)
        self.page.locator('#media-tab').click(); self.card('fixture.mp4').get_by_role('button',name='授权',exact=True).click()
        self.page.locator('#grant-panel').wait_for(state='visible')
        self.page.locator('#grant-users label').filter(has_text='browser_viewer').get_by_role('checkbox').check()
        self.page.locator('#grants-form button[type=submit]').click()
        self.page.wait_for_function("() => document.getElementById('message').textContent.includes('授权已保存')")
        self.page.locator('#logout').click(); self.page.locator('#login-section').wait_for(state='visible')
        self.login('browser_viewer',password)
        self.assertEqual(self.page.locator('.card').count(),1)
        self.assertTrue(self.page.locator('#admin-tab').is_hidden())
        self.assertEqual(self.page.evaluate("async () => (await fetch('/api/media/config')).status"),403)
        self.assertEqual(self.page.evaluate(f"async () => (await fetch('/media/{self.f.item['id']}',{{headers:{{Range:'bytes=0-20'}}}})).status"),403)
        started=time.monotonic(); self.card('fixture.mp4').get_by_role('button',name='播放',exact=True).click()
        self.page.wait_for_function("() => document.getElementById('player').readyState>=1")
        metadata_seconds=time.monotonic()-started
        dimensions=self.page.locator('#player').evaluate('(v)=>({width:v.videoWidth,height:v.videoHeight,duration:v.duration})')
        self.assertEqual(dimensions['width'],320); self.assertEqual(dimensions['height'],180); self.assertAlmostEqual(dimensions['duration'],12,places=1)
        self.page.evaluate("() => { window.playEvents={waiting:0,stalled:0,playing:0};const v=document.getElementById('player');for(const e of Object.keys(window.playEvents))v.addEventListener(e,()=>window.playEvents[e]++); }")
        play_start=time.monotonic(); self.page.locator('#player').evaluate('(v)=>v.play()')
        self.page.wait_for_function("() => document.getElementById('player').currentTime>1")
        playing_seconds=time.monotonic()-play_start
        frames=self.page.locator('#player').evaluate('(v)=>v.getVideoPlaybackQuality().totalVideoFrames'); self.assertGreater(frames,10)
        self.page.locator('#player').evaluate('(v)=>{v.currentTime=6}')
        self.page.wait_for_function("() => !document.getElementById('player').seeking && document.getElementById('player').currentTime>6.25")
        paused_position=self.page.locator('#player').evaluate('(v)=>{v.pause();return v.currentTime}')
        for _ in range(100):
            rows=self.f.db('SELECT position FROM history WHERE user_id=?',(viewer['id'],))
            if rows and abs(rows[0][0]-paused_position)<.05: break
            self.page.wait_for_timeout(30)
        else: self.fail('browser playback position was not persisted')
        saved=rows[0][0]; self.assertLess(saved,8)
        play_events=self.page.evaluate('window.playEvents')
        self.page.reload(wait_until='networkidle'); self.page.locator('#workspace').wait_for(state='visible')
        self.card('fixture.mp4').get_by_role('button',name='播放',exact=True).click()
        self.page.wait_for_function("() => document.getElementById('player').readyState>=1 && document.getElementById('player').currentTime>5.5")
        resumed=self.page.locator('#player').evaluate('(v)=>v.currentTime'); self.assertAlmostEqual(resumed,saved,delta=.3)
        self.assertTrue(any(entry['status']==206 and entry['range'] for entry in self.ranges))
        self.assertEqual(self.page.evaluate('Object.keys(localStorage).length'),0)
        self.page.screenshot(path=str(self.artifacts/'viewer-playback.png'),full_page=True)
        self.assert_clean()
        self.evidence['tests'].append({'name':'upload_grant_native_play_seek_resume','passed':True,'loaded_metadata_seconds':round(metadata_seconds,4),'time_to_currentTime_over_one_second':round(playing_seconds,4),'decoded_frames':frames,'seek_target_seconds':6,'persisted_position_seconds':saved,'resumed_position_seconds':resumed,'observed_events':play_events,'media_responses':self.ranges,'video':dimensions})
    def test_05_admin_monitor_actual_refresh_audit_text_and_user_boundary(self):
        password=secrets.token_urlsafe(24)
        self.f.call('POST','/api/users',{'username':'monitor_viewer','password':password},self.f.admin,expected=201)
        other=self.f.data/'<audit-fixture>'; other.mkdir()
        self.f.call('POST','/api/media/root',{'path':str(other)},self.f.admin)
        # Test-only DB fixture input exercises literal audit rendering; no product bypass API.
        self.f.db('INSERT INTO audit(timestamp,actor,event,detail) VALUES(?,?,?,?)',(int(time.time()),self.f.admin['user']['id'],'test_fixture_markup',json.dumps({'note':'<img src=x onerror=alert(1)> <audit-fixture>'})))
        self.login('media_administrator',self.f.password); self.page.locator('#admin-tab').click()
        self.page.locator('#monitor-panel').wait_for(state='visible')
        self.assertIn('2/2 个启用',self.page.locator('#monitor-summary').inner_text())
        self.assertIn('<audit-fixture>',self.page.locator('#audit-list').inner_text())
        self.assertEqual(self.page.locator('#audit-list img').count(),0)
        self.page.locator('#refresh-monitor').click()
        self.page.wait_for_function("() => !document.getElementById('refresh-monitor').disabled")
        self.assertIn('快照时间',self.page.locator('#monitor-status').inner_text())
        self.page.screenshot(path=str(self.artifacts/'admin-monitor.png'),full_page=True)
        self.page.locator('#logout').click(); self.page.locator('#login-section').wait_for(state='visible')
        self.login('monitor_viewer',password)
        self.assertEqual(self.page.evaluate("async () => (await fetch('/api/admin/status')).status"),403)
        self.assertEqual(self.page.locator('#monitor-summary').inner_text(),''); self.assertTrue(self.page.locator('#administration').is_hidden())
        self.assert_clean(); self.evidence['tests'].append({'name':'admin_monitor_refresh_audit_plain_text_and_role_boundary','passed':True})
    def test_02_upload_errors_cancel_and_filename_is_plain_text(self):
        self.login('media_administrator',self.f.password); self.page.locator('#admin-tab').click()
        self.page.locator('#administration').wait_for(state='visible')
        self.page.locator('#upload-file').set_input_files({'name':'sample.mp4','mimeType':'video/mp4','buffer':self.f.bytes})
        self.page.locator('#upload-button').click()
        self.page.wait_for_function("() => document.getElementById('upload-status').textContent.includes('destination exists')")
        self.assertEqual(self.f.original.read_bytes(),self.f.bytes)
        self.page.locator('#upload-file').set_input_files({'name':'fake.mp4','mimeType':'video/mp4','buffer':b'not video data'})
        self.page.locator('#upload-button').click()
        self.page.wait_for_function("() => document.getElementById('upload-status').textContent.includes('video container')")
        malicious='<img src=x onerror=window.uploadXss=1>.mp4'
        self.page.locator('#upload-file').set_input_files({'name':malicious,'mimeType':'video/mp4','buffer':self.f.bytes})
        self.page.locator('#upload-button').click(); self.page.wait_for_function("() => document.getElementById('upload-status').textContent.includes('上传成功')")
        self.page.locator('#media-tab').click(); self.card(malicious).wait_for(state='visible')
        self.assertEqual(self.page.locator('.card img').count(),0); self.assertTrue(self.page.evaluate('window.uploadXss===undefined'))
        self.page.locator('#admin-tab').click(); self.page.locator('#administration').wait_for(state='visible')
        cdp=self.context.new_cdp_session(self.page); cdp.send('Network.enable')
        cdp.send('Network.emulateNetworkConditions',{'offline':False,'latency':200,'downloadThroughput':1024*1024,'uploadThroughput':16*1024})
        self.page.locator('#upload-file').set_input_files({'name':'cancelled.mp4','mimeType':'video/mp4','buffer':self.f.bytes})
        self.page.locator('#upload-button').click(); self.page.locator('#cancel-upload').wait_for(state='visible'); self.page.locator('#cancel-upload').click()
        self.page.wait_for_function("() => document.getElementById('upload-status').textContent.includes('已取消传输')")
        self.assertIn('不会撤销已完成的保存',self.page.locator('#upload-status').inner_text())
        cdp.send('Network.emulateNetworkConditions',{'offline':False,'latency':0,'downloadThroughput':-1,'uploadThroughput':-1})
        self.page.wait_for_timeout(200)
        published=(self.f.root/'cancelled.mp4').exists()
        if published:
            # Aborting after server commit cannot roll back an HTTP side effect.
            # The UI must report uncertainty; any published file must be complete.
            self.assertEqual((self.f.root/'cancelled.mp4').read_bytes(),self.f.bytes)
        self.assertEqual(list(self.f.root.glob('.upload-*')),[])
        self.assert_clean(); self.evidence['tests'].append({'name':'duplicate_invalid_cancel_xss','passed':True,'late_abort_file_already_published':published})
    def test_03_browser_password_change_and_logout(self):
        self.login('media_administrator',self.f.password)
        self.page.locator('#password-tab').click(); replacement=secrets.token_urlsafe(24)
        self.page.locator('#old-password').fill(self.f.password); self.page.locator('#replacement-password').fill(replacement); self.page.locator('#confirm-password').fill(replacement)
        self.page.locator('#password-form button').click(); self.page.locator('#login-section').wait_for(state='visible')
        self.assertEqual(self.page.evaluate("async()=> (await fetch('/api/me')).status"),401)
        self.login('media_administrator',replacement)
        self.page.locator('#logout').click(); self.page.locator('#login-section').wait_for(state='visible')
        self.assertEqual(self.page.evaluate("async()=> (await fetch('/api/me')).status"),401)
        self.assert_clean(); self.evidence['tests'].append({'name':'browser_password_and_logout','passed':True})

    def test_04_resumable_upload_real_disconnect_reload_and_hash(self):
        self.login('media_administrator',self.f.password); self.page.locator('#admin-tab').click()
        self.page.locator('#administration').wait_for(state='visible'); self.page.locator('#resumable-upload').check()
        payload=self.f.bytes+b'\0'*(3*1024*1024); file={'name':'resumed-browser.mp4','mimeType':'video/mp4','buffer':payload}
        offsets=[]
        def interrupt_second(route):
            at=int(route.request.headers['upload-offset']); offsets.append(at)
            if at>=1048576: route.abort('connectionfailed')
            else: route.continue_()
        self.context.route('**/api/uploads/*/chunk',interrupt_second)
        self.page.locator('#upload-file').set_input_files(file); self.page.locator('#upload-button').click()
        self.page.wait_for_function("() => document.getElementById('upload-status').textContent.includes('分块连接中断')")
        confirmed=self.f.db('SELECT offset FROM uploads WHERE name=?',('resumed-browser.mp4',))[0][0]
        self.assertEqual(confirmed,1048576); self.assertFalse((self.f.root/'resumed-browser.mp4').exists())
        self.context.unroute('**/api/uploads/*/chunk',interrupt_second)
        continued=[]
        def observe(route): continued.append(int(route.request.headers['upload-offset'])); route.continue_()
        self.context.route('**/api/uploads/*/chunk',observe)
        self.page.reload(wait_until='networkidle'); self.page.locator('#workspace').wait_for(state='visible')
        self.page.locator('#admin-tab').click(); self.page.locator('#administration').wait_for(state='visible')
        self.page.locator('#resumable-upload').check(); self.page.locator('#upload-file').set_input_files(file); self.page.locator('#upload-button').click()
        self.page.wait_for_function("() => document.getElementById('upload-status').textContent.includes('上传成功')")
        self.assertEqual(continued[0],1048576); self.assertEqual((self.f.root/'resumed-browser.mp4').read_bytes(),payload)
        self.assertEqual(self.page.evaluate('Object.keys(localStorage).length'),0)
        self.assert_clean(); self.evidence['tests'].append({'name':'resumable_browser_disconnect_reload','passed':True,'confirmed_before_reload_bytes':confirmed,'first_continued_offset':continued[0],'sha256':hashlib.sha256(payload).hexdigest()})

if __name__=='__main__': unittest.main(verbosity=2)
