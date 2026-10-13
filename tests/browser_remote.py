#!/usr/bin/env python3
"""Native video and sharing management through a real HTTPS Nginx entry.

Browser test explicitly trusts only its generated leaf public key. It does not
disable certificate errors globally; C++/Python CA and hostname checks have
separate positive and negative tests. No production browser settings change.
"""
import base64, datetime, hashlib, json, pathlib, subprocess, tempfile, time, unittest
from playwright.sync_api import sync_playwright
import remote_integration as remote

class BrowserRemote(unittest.TestCase):
    def test_https_create_play_seek_revoke(self):
        with tempfile.TemporaryDirectory() as folder:
            source=pathlib.Path(folder)/'sample.mp4'; remote.media.generate_video(source)
            f=remote.RemoteFixture(source); self.addCleanup(f.close)
            pub=subprocess.run(['openssl','x509','-in',str(f.cert),'-pubkey','-noout'],capture_output=True,check=True).stdout
            der=subprocess.run(['openssl','pkey','-pubin','-outform','DER'],input=pub,capture_output=True,check=True).stdout
            pin=base64.b64encode(hashlib.sha256(der).digest()).decode()
            evidence={'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'fixture_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'scope':'local real Nginx TLS fixture; no public/NAT claim','browser_test_trust':'only generated leaf SPKI; no blanket ignoreHTTPSErrors','sandbox':False,'sandbox_scope':'generated local test fixture only'}
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,chromium_sandbox=False,args=['--ignore-certificate-errors-spki-list='+pin,'--autoplay-policy=no-user-gesture-required'])
                try:
                    admin=browser.new_context(viewport={'width':1280,'height':900}); viewer=browser.new_context(viewport={'width':1280,'height':900})
                    errors=[]; external=[]; ranges=[]
                    def allow_only(route):
                        if route.request.url.startswith(f.base+'/'): route.continue_()
                        else: external.append('unexpected external request'); route.abort()
                    for context in [admin,viewer]: context.route('**/*',allow_only)
                    page=admin.new_page(); page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f.base,wait_until='networkidle'); page.locator('#username').fill('media_administrator'); page.locator('#password').fill(f.f.password)
                    page.locator('#login-form button').click(); page.locator('#workspace').wait_for(state='visible')
                    page.wait_for_function("() => document.getElementById('total').textContent.includes('个可访问视频')")
                    page.locator('.card').get_by_role('button',name='分享',exact=True).click(); page.locator('#remote-administration').wait_for(state='visible')
                    page.locator('#test-remote').click(); page.wait_for_function("() => document.getElementById('remote-test-result').textContent.includes('校验通过')")
                    page.locator('#share-ttl').select_option('3600'); page.locator('#create-share-form button').click()
                    page.wait_for_function("() => document.getElementById('share-url').value.includes('/share#')")
                    link=page.locator('#share-url').input_value(); self.assertTrue(link.startswith(f.base+'/share#'))
                    shared=viewer.new_page(); shared.on('pageerror',lambda e:errors.append(str(e)))
                    def response(r):
                        if r.url==f.base+'/share/media': ranges.append({'status':r.status,'range':r.request.header_value('range'),'content_range':r.headers.get('content-range')})
                    shared.on('response',response)
                    started=time.monotonic(); shared.goto(link,wait_until='networkidle'); shared.locator('#share-panel').wait_for(state='visible')
                    shared.wait_for_function("() => document.getElementById('shared-player').readyState>=1")
                    metadata=time.monotonic()-started; self.assertEqual(shared.url,f.base+'/share')
                    self.assertEqual(shared.locator('#share-name').inner_text(),'sample.mp4')
                    self.assertEqual(shared.evaluate("async () => (await fetch('/api/media')).status"),401)
                    self.assertEqual(shared.evaluate("async () => (await fetch('/api/users')).status"),401)
                    cookies=viewer.cookies(); session=next(item for item in cookies if item['name']=='lv_share')
                    self.assertTrue(session['secure']); self.assertTrue(session['httpOnly']); self.assertEqual(session['path'],'/share/')
                    self.assertFalse(any(item['name']=='lv_session' for item in cookies))
                    play_start=time.monotonic(); shared.locator('#shared-player').evaluate('(v)=>v.play()')
                    shared.wait_for_function("() => document.getElementById('shared-player').currentTime>1")
                    playing=time.monotonic()-play_start; frames=shared.locator('#shared-player').evaluate('(v)=>v.getVideoPlaybackQuality().totalVideoFrames'); self.assertGreater(frames,10)
                    seek=time.monotonic(); shared.locator('#shared-player').evaluate('(v)=>{v.currentTime=8}')
                    shared.wait_for_function("() => !document.getElementById('shared-player').seeking && document.getElementById('shared-player').currentTime>8.25")
                    seek_seconds=time.monotonic()-seek; shared.locator('#shared-player').evaluate('(v)=>v.pause()')
                    dimensions=shared.locator('#shared-player').evaluate('(v)=>({width:v.videoWidth,height:v.videoHeight,duration:v.duration})')
                    self.assertEqual(dimensions['width'],320); self.assertAlmostEqual(dimensions['duration'],12,places=1)
                    artifacts=pathlib.Path(remote.media.BINARY).resolve().parent/'Testing'/'artifacts'; artifacts.mkdir(parents=True,exist_ok=True)
                    shared.screenshot(path=str(artifacts/'remote-share-player.png'),full_page=True)
                    page.locator('#share-list').get_by_role('button',name='撤销',exact=True).click()
                    shared.wait_for_function("() => document.getElementById('share-status').textContent.includes('撤销')")
                    self.assertTrue(shared.locator('#share-panel').is_hidden())
                    self.assertEqual(shared.evaluate("async () => (await fetch('/share/media')).status"),404)
                    shared.reload(wait_until='networkidle'); self.assertTrue(shared.locator('#share-panel').is_hidden())
                    self.assertTrue(any(item['status']==206 for item in ranges)); self.assertEqual(errors,[]); self.assertEqual(external,[])
                    self.assertEqual(shared.evaluate('Object.keys(localStorage).length'),0)
                    evidence.update({'browser':browser.version,'passed':True,'loaded_metadata_seconds':round(metadata,4),'time_to_currentTime_over_one_second':round(playing,4),'decoded_frames':frames,'seek_target_seconds':8,'seek_completed_seconds':round(seek_seconds,4),'video':dimensions,'media_responses':ranges,'page_errors':errors,'external_requests':external,'post_revoke_media_status':404,'fragment_removed':True})
                    (artifacts/'remote-browser-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
                    viewer.close(); admin.close()
                finally: browser.close()

if __name__=='__main__': unittest.main(verbosity=2)
