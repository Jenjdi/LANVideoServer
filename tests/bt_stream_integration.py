#!/usr/bin/env python3
"""Only true libtorrent piece availability and independently compared HTTP bytes."""
import concurrent.futures
import hashlib
import http.client
import json
import pathlib
import signal
import socket
import time
import unittest
import bt_integration as bt

class BtStream(unittest.TestCase):
    def setUp(self):
        self.f=bt.BtFixture(); self.addCleanup(self.f.close); self.task=self.f.imported(); self.id=self.task['id']; self.path=f'/bt/{self.id}/file/0'
    def raw(self,headers=None,session=None,method='GET',path=None):
        c=http.client.HTTPConnection('127.0.0.1',self.f.server.port,timeout=6)
        values={'Cookie':'lv_session='+self.f.admin['token']} if session is None else {'Cookie':'lv_session='+session['token']} if session else {}
        values.update(headers or {})
        try:
            c.request(method,path or self.path,headers=values); response=c.getresponse(); return response.status,response.read(),dict(response.headers)
        finally:c.close()
    def resume(self):self.f.call('POST',f'/api/tasks/{self.id}/resume',{},self.f.admin)
    def complete(self): self.resume(); self.f.wait(self.id,lambda x:x['state']=='seeding' and x['progress']==1)
    def test_01_unavailable_is_retryable_not_zeros_or_sparse_file(self):
        code,data,headers=self.raw({'Range':'bytes=0-99'})
        self.assertEqual(code,503); self.assertEqual(headers['Retry-After'],'1'); self.assertTrue(json.loads(data)['retryable'])
        self.assertNotEqual(data,self.f.original[:100]); self.assertEqual(self.f.state(self.id)['progress'],0)
        self.assertEqual(self.raw(method='HEAD')[0],200)
        self.assertEqual(self.raw()[0],503)
    def test_02_real_verified_prefix_and_later_unavailable_piece(self):
        self.resume()
        deadline=time.monotonic()+15
        while True:
            code,data,h=self.raw({'Range':'bytes=0-'})
            if code==206 or time.monotonic()>=deadline:break
            self.assertEqual(code,503);self.assertEqual(h['Retry-After'],'1');time.sleep(.05)
        self.assertEqual(code,206); self.assertEqual(data,self.f.original[:len(data)]); self.assertGreater(len(data),0)
        self.assertLess(self.f.state(self.id)['progress'],1)
        start,endtotal=h['Content-Range'].removeprefix('bytes ').split('-'); end,total=endtotal.split('/')
        self.assertEqual(int(end)+1,len(data)); self.assertEqual(int(total),len(self.f.original))
        self.f.call('POST',f'/api/tasks/{self.id}/pause',{},self.f.admin)
        self.f.wait(self.id,lambda x:x['paused_actual'])
        prefix=self.f.call('GET',f'/api/tasks/{self.id}/files',session=self.f.admin)[0]['files'][0]['contiguous_from_start']
        self.assertLess(prefix,len(self.f.original))
        self.assertEqual(self.raw({'Range':f'bytes={prefix}-'})[0],503)
    def test_03_complete_file_closed_open_suffix_full_and_head(self):
        self.complete()
        for requested,begin,end in [('bytes=19-70000',19,70000),('bytes=-99',len(self.f.original)-99,len(self.f.original)-1),('bytes=65530-65540',65530,65540)]:
            code,data,h=self.raw({'Range':requested}); self.assertEqual(code,206); self.assertEqual(data,self.f.original[begin:end+1]); self.assertEqual(h['Content-Range'],f'bytes {begin}-{end}/{len(self.f.original)}')
        code,data,h=self.raw(); self.assertEqual(code,200); self.assertEqual(hashlib.sha256(data).digest(),hashlib.sha256(self.f.original).digest())
        code,data,h=self.raw({'Range':'bytes=0-'},method='HEAD'); self.assertEqual(code,200); self.assertEqual(data,b''); self.assertEqual(int(h['Content-Length']),len(self.f.original))
        code,data,h=self.raw({'Range':'bytes=1-'}); self.assertEqual(code,206); self.assertLessEqual(len(data),1048576); self.assertEqual(data,self.f.original[1:1+len(data)])
    def test_04_invalid_ranges_reauthenticate_and_no_traversal(self):
        for value in ['bytes=999999999-','bytes=3-2','bytes=0-1,3-4','bytes=-0','bytes=oops','chars=1-2']:
            code,data,h=self.raw({'Range':value}); self.assertEqual(code,416,value); self.assertEqual(h['Content-Range'],f'bytes */{len(self.f.original)}')
        self.assertEqual(self.raw({'Range':'bytes=oops'},session=False)[0],401)
        for suffix in ['../source.torrent','-1','1000']:
            self.assertIn(self.raw(path=f'/bt/{self.id}/file/{suffix}')[0],(400,404))
    def test_05_owner_csrf_and_persisted_playback_position(self):
        password=bt.secrets.token_urlsafe(24)
        self.f.call('POST','/api/users',{'username':'stream_other','password':password},self.f.admin,expected=201); other=self.f.login('stream_other',password)
        for suffix in ['/files','/schedule']:self.f.call('GET',f'/api/tasks/{self.id}'+suffix,session=other,expected=404)
        self.assertEqual(self.raw({'Range':'bytes=0-1'},session=other)[0],404)
        self.f.call('POST',f'/api/tasks/{self.id}/focus',{'file_index':0,'byte_offset':0},self.f.admin,csrf=False,expected=403)
        history=f'/api/tasks/{self.id}/files/0/history'
        self.f.call('POST',history,{'position':6.25},self.f.admin)
        self.f.server.stop(); self.f.server=bt.media.auth.Server(self.f.data)
        files=self.f.call('GET',f'/api/tasks/{self.id}/files',session=self.f.admin)[0]['files']
        self.assertEqual(files[0]['position'],6.25)
        self.f.call('POST',history,{'position':-1},self.f.admin,expected=400)
        self.f.call('POST',history,{'position':6.25},other,expected=404)
    def test_06_focus_mapping_and_background_fairness_are_real_priorities(self):
        offset=17*65536+41
        response=self.f.call('POST',f'/api/tasks/{self.id}/focus',{'file_index':0,'byte_offset':offset},self.f.admin)[0]
        self.assertEqual(response['mapped_piece'],17); self.assertEqual(response['offset_in_piece'],41)
        phases=set(); deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            data=self.f.call('GET',f'/api/tasks/{self.id}/schedule',session=self.f.admin)[0]
            current=data['focus']; phases.add(current['phase'])
            self.assertTrue(all(p['priority']==(7 if current['phase']=='focus' else 4) for p in current['pieces']))
            time.sleep(.05)
        self.assertEqual(phases,{'focus','background'})
        self.resume(); before=time.monotonic()
        while time.monotonic()-before<20:
            self.f.call('POST',f'/api/tasks/{self.id}/focus',{'file_index':0,'byte_offset':offset},self.f.admin); time.sleep(.1)
            if self.f.state(self.id)['progress']==1:break
        self.assertLess(time.monotonic()-before,20)
        self.f.wait(self.id,lambda x:x['state']=='seeding')
        self.assertEqual(self.raw()[1],self.f.original)
    def test_07_corrupt_piece_and_symlink_are_not_transmitted(self):
        self.complete(); self.f.call('POST',f'/api/tasks/{self.id}/pause',{},self.f.admin); self.f.wait(self.id,lambda x:x['paused_actual'])
        dest=self.f.data/'downloads'/f'task-{self.id}'/self.f.payload.name
        with dest.open('r+b') as stream:stream.seek(100);stream.write(b'bad')
        code,data,h=self.raw({'Range':'bytes=0-120'}); self.assertEqual(code,409); self.assertIn('checksum',json.loads(data)['error'])
        dest.unlink();dest.symlink_to(self.f.payload)
        self.assertIn(self.raw({'Range':'bytes=65536-65540'})[0],(403,409))
    def test_08_cancel_task_and_concurrent_reads(self):
        self.complete()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.raw({'Range':'bytes=65530-70000'}),range(4)))
        for code,data,h in results:self.assertEqual(code,206);self.assertEqual(data,self.f.original[65530:70001])
        self.f.call('DELETE',f'/api/tasks/{self.id}',session=self.f.admin)
        self.assertEqual(self.raw({'Range':'bytes=0-1'})[0],404)
    def test_09_shutdown_during_unavailable_read(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(self.raw,{'Range':'bytes=0-99'}); time.sleep(.2)
            started=time.monotonic(); self.f.server.stop(); self.assertLess(time.monotonic()-started,5)
            try:response=pending.result(timeout=5);self.assertIn(response[0],(400,503))
            except (OSError,http.client.HTTPException):pass
    def test_10_multifile_non_aligned_offsets_and_cross_file_piece_hash(self):
        f=bt.BtFixture(multi=True); self.addCleanup(f.close); item=f.imported(); id=item['id']
        files=f.call('GET',f'/api/tasks/{id}/files',session=f.admin)[0]['files']; video=files[0]
        self.assertEqual(video['torrent_offset'],37001);self.assertEqual(video['index'],1)
        focus=f.call('POST',f'/api/tasks/{id}/focus',{'file_index':1,'byte_offset':30000},f.admin)[0]
        self.assertEqual(focus['mapped_piece'],1);self.assertEqual(focus['offset_in_piece'],1465)
        f.call('POST',f'/api/tasks/{id}/resume',{},f.admin);f.wait(id,lambda x:x['state']=='seeding')
        old=self.f;self.f=f
        try:
            code,data,h=self.raw({'Range':'bytes=0-40000'},path=f'/bt/{id}/file/1');self.assertEqual(code,206);self.assertEqual(data,f.original[:40001])
            self.assertEqual(self.raw(path=f'/bt/{id}/file/0')[0],415)
        finally:self.f=old

if __name__=='__main__':unittest.main(verbosity=2)
