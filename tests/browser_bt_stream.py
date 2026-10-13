#!/usr/bin/env python3
"""Native video decoding while an actual unpadded video torrent is incomplete."""
import datetime
import hashlib
import json
import pathlib
import time
import unittest
from playwright.sync_api import sync_playwright
import bt_integration as bt

class BrowserStreaming(unittest.TestCase):
    def test_incomplete_video_wait_play_seek_and_persist(self):
        # Keep playback-before-completion demonstrable even if initial peer queues
        # delay the first requested piece. More genuine video, no padding or fake progress.
        f=bt.BtFixture(seconds=90,width=640,height=360,padding=0,upload_limit=128*1024);self.addCleanup(f.close)
        self.assertGreater(len(f.original),45*128*1024+1024*1024)
        task=f.imported();id=task['id']
        p=sync_playwright().start();self.addCleanup(p.stop)
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,chromium_sandbox=False,args=['--autoplay-policy=no-user-gesture-required']);self.addCleanup(browser.close)
        context=browser.new_context(viewport={'width':1280,'height':900});self.addCleanup(context.close)
        external=[]
        def local(route):
            if route.request.url.startswith(f.server.base+'/'):route.continue_()
            else:external.append('external request');route.abort()
        context.route('**/*',local);page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        responses=[]
        failed_requests=[]
        page.on('requestfailed',lambda request:failed_requests.append({'path':request.url.split(f.server.base)[-1],'error':request.failure}))
        def response(res):
            if res.url.startswith(f.server.base+'/bt/'):responses.append({'status':res.status,'range':res.request.header_value('range'),'content_range':res.headers.get('content-range'),'observed_seconds':time.monotonic()})
        def diagnostic():
            artifact=pathlib.Path(bt.media.auth.BINARY).resolve().parent/'Testing'/'artifacts';artifact.mkdir(parents=True,exist_ok=True)
            info={'responses':responses,'failed_requests':failed_requests,'page_errors':errors,'player':page.locator('#player').evaluate('(v)=>({src:v.getAttribute("src"),time:v.currentTime,paused:v.paused,ready:v.readyState,network:v.networkState,error:v.error?{code:v.error.code,message:v.error.message}:null})'),'ui_error':page.locator('#player-error').inner_text(),'events':page.evaluate('window.btEvents || []'),'task':f.state(id)}
            (artifact/'bt-stream-debug.json').write_text(json.dumps(info,indent=2))
        page.on('response',response);page.goto(f.server.base)
        self.addCleanup(diagnostic)
        page.locator('#username').fill('bt_administrator');page.locator('#password').fill(f.password);page.locator('#login-form button').click();page.locator('#workspace').wait_for(state='visible')
        page.evaluate("() => { window.btEvents=[]; for(const name of ['loadedmetadata','waiting','stalled','playing','seeking','seeked','error']) document.getElementById('player').addEventListener(name,()=>window.btEvents.push({event:name,time:performance.now(),position:document.getElementById('player').currentTime})); }")
        page.locator('#tasks-tab').click();page.locator('#task-list button').filter(has_text='视频文件').click();page.locator('#bt-file-list button').click()
        page.wait_for_function("() => document.getElementById('player-error').textContent.includes('正在等待')",timeout=12000)
        until=time.monotonic()+8
        while not any(res['status']==503 for res in responses) and time.monotonic()<until:page.wait_for_timeout(50)
        self.assertTrue(any(res['status']==503 for res in responses),json.dumps({'responses':responses,'failures':failed_requests,'src':page.locator('#player').get_attribute('src'),'media_error':page.locator('#player').evaluate('(v)=>v.error ? {code:v.error.code,message:v.error.message} : null')}));self.assertEqual(f.state(id)['verified_bytes'],0)
        page.locator('#tasks-tab').click();page.locator('#task-list').get_by_role('button',name='恢复',exact=True).click();began=time.monotonic()
        page.locator('#media-tab').click();page.locator('#retry-bt-playback').click()
        page.wait_for_function("() => document.getElementById('player').currentTime>1 && document.getElementById('player').readyState>=2",timeout=45000)
        start_elapsed=time.monotonic()-began;start_state=f.state(id)
        self.assertLess(start_state['progress'],1,'video must still be downloading when native playback starts')
        frames=page.locator('#player').evaluate('(video)=>video.getVideoPlaybackQuality().totalVideoFrames');self.assertGreater(frames,15)
        seek_began=time.monotonic();page.locator('#player').evaluate('(video)=>{video.currentTime=35;}')
        page.wait_for_function("() => document.getElementById('player').currentTime>=35 && document.getElementById('player').readyState>=2 && !document.getElementById('player').seeking",timeout=60000)
        seek_elapsed=time.monotonic()-seek_began
        page.wait_for_function("() => document.getElementById('player').getVideoPlaybackQuality().totalVideoFrames>"+str(frames+5),timeout=20000)
        page.locator('#player').evaluate('(video)=>video.pause()');position=page.locator('#player').evaluate('(video)=>video.currentTime')
        deadline=time.monotonic()+10;saved=0
        while time.monotonic()<deadline:
            saved=f.call('GET',f'/api/tasks/{id}/files',session=f.admin)[0]['files'][0]['position']
            if saved>=35:break
            page.wait_for_timeout(100)
        self.assertGreaterEqual(saved,35);self.assertAlmostEqual(saved,position,delta=1)
        events=page.evaluate('window.btEvents');self.assertTrue(any(e['event']=='seeked' and e['position']>=35 for e in events))
        page.reload();page.locator('#workspace').wait_for(state='visible');page.locator('#tasks-tab').click();page.locator('#task-list').get_by_role('button',name='视频文件',exact=True).click();page.locator('#bt-file-list button').click()
        page.wait_for_function("() => document.getElementById('player').currentTime>=35 && document.getElementById('player').readyState>=2",timeout=30000)
        self.assertEqual(errors,[]);self.assertEqual(external,[])
        artifact=pathlib.Path(bt.media.auth.BINARY).resolve().parent/'Testing'/'artifacts';artifact.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(artifact/'bt-stream-player.png'),full_page=True)
        evidence={'recorded_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'browser':browser.version,'payload_bytes':len(f.original),'fixture':'90 seconds 640x360 H264 faststart MP4, zero padding bytes','fixture_sha256':hashlib.sha256(f.original).hexdigest(),'source_upload_limit_bytes_per_second':128*1024,'startup_seconds':start_elapsed,'verified_progress_at_playback_start':start_state['progress'],'decoded_frames_at_start':frames,'seek_seconds':seek_elapsed,'seek_target_seconds':35,'saved_position_seconds':saved,'waiting_events':sum(e['event']=='waiting' for e in events),'stalled_events':sum(e['event']=='stalled' for e in events),'events':events,'responses':responses,'sandbox':False,'sandbox_scope':'generated local fixture only','passed':True}
        (artifact/'bt-stream-browser-evidence.json').write_text(json.dumps(evidence,indent=2))

if __name__=='__main__':unittest.main(verbosity=2)
