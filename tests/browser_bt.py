#!/usr/bin/env python3
"""Actual Chromium BT/proxy interface against live local seeder/tracker/relay."""
import json
import pathlib
import unittest
from playwright.sync_api import sync_playwright
import bt_integration as bt

class BrowserBt(unittest.TestCase):
    def test_real_torrent_proxy_controls_and_display(self):
        f=bt.BtFixture(); self.addCleanup(f.close)
        proxy=bt.Proxy('socks5',bt.secrets.token_hex(8),bt.secrets.token_hex(12)); self.addCleanup(proxy.close)
        playwright=sync_playwright().start(); self.addCleanup(playwright.stop)
        browser=playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,chromium_sandbox=False)
        self.addCleanup(browser.close); context=browser.new_context(); self.addCleanup(context.close)
        external=[]
        def local_only(route):
            if route.request.url.startswith(f.server.base+'/'): route.continue_()
            else: external.append('unexpected external request'); route.abort()
        context.route('**/*',local_only); page=context.new_page(); errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f.server.base); page.locator('#username').fill('bt_administrator'); page.locator('#password').fill(f.password)
        page.locator('#login-form button').click(); page.locator('#workspace').wait_for(state='visible')
        page.locator('#tasks-tab').click(); page.locator('#bt-admin').wait_for(state='visible')
        page.wait_for_function("() => document.getElementById('proxy-summary').textContent.includes('当前')")
        page.locator('#proxy-type').select_option('socks5'); page.locator('#proxy-host').fill('127.0.0.1'); page.locator('#proxy-port').fill(str(proxy.port))
        page.locator('#proxy-user').fill(proxy.username); page.locator('#proxy-password').fill(proxy.password); page.locator('#proxy-form button').click()
        page.wait_for_function("() => document.getElementById('proxy-summary').textContent.includes('已加密保存')")
        self.assertEqual(page.locator('#proxy-password').input_value(),'')
        page.locator('#proxy-test-host').fill('127.0.0.1'); page.locator('#proxy-test-port').fill(str(f.tracker.seed_port)); page.locator('#proxy-test-form button').click()
        page.wait_for_function("() => document.getElementById('proxy-status').textContent.includes('连接成功')")
        page.locator('#torrent-file').set_input_files(f.torrent); page.locator('#torrent-form button').click()
        page.locator('#task-list section').wait_for(state='visible'); row=page.locator('#task-list section')
        self.assertIn('暂停',row.inner_text()); row.get_by_role('button',name='恢复',exact=True).click()
        page.wait_for_function("() => document.getElementById('task-list').textContent.includes('100.0%')",timeout=45000)
        item=f.call('GET','/api/tasks',session=f.admin)[0]['tasks'][0]
        self.assertEqual(item['progress'],1); self.assertEqual(item['source'],'libtorrent 2.0.11')
        self.assertEqual((f.data/'downloads'/f'task-{item["id"]}'/f.payload.name).read_bytes(),f.original)
        self.assertGreater(proxy.bytes,len(f.original)); row.get_by_role('button',name='暂停',exact=True).click()
        page.wait_for_function("() => document.getElementById('task-list').textContent.includes('暂停 · 已校验')")
        self.assertTrue(f.state(item['id'])['paused']); self.assertEqual(page.evaluate('Object.keys(localStorage).length'),0)
        self.assertEqual(errors,[]); self.assertEqual(external,[])
        artifact=pathlib.Path(bt.media.auth.BINARY).resolve().parent/'Testing'/'artifacts'
        artifact.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(artifact/'bt-browser.png'),full_page=True)
        (artifact/'bt-browser-evidence.json').write_text(json.dumps({'passed':True,'browser':browser.version,'transport':'actual local SOCKS5 authenticated forwarding','verified_bytes':item['verified_bytes'],'piece_count':item['piece_count'],'file_sha256':bt.hashlib.sha256(f.original).hexdigest(),'sandbox':False,'sandbox_scope':'generated local fixture only'},indent=2))

if __name__=='__main__': unittest.main(verbosity=2)
