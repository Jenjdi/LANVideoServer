#!/usr/bin/env python3
"""Install/move real binary, verify packaged assets and private runtime persistence."""
import http.client, json, os, pathlib, signal, socket, subprocess, tempfile, time, unittest, shutil
import media_integration as media
CMAKE=os.environ.get('CMAKE', shutil.which('cmake') or 'cmake')
LIBS=os.environ.get('LD_LIBRARY_PATH', '/workspace/toolchain/sysroot/usr/lib/x86_64-linux-gnu' if pathlib.Path('/workspace/toolchain/sysroot/usr/lib/x86_64-linux-gnu').exists() else '')

class DeliveryIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(); cls.root=pathlib.Path(cls.temp.name)
        original=cls.root/'installed'; cls.prefix=cls.root/'moved-install'
        subprocess.run([CMAKE,'--install',str(pathlib.Path(media.BINARY).resolve().parent),'--prefix',str(original)],check=True,capture_output=True,timeout=30)
        original.rename(cls.prefix); cls.binary=cls.prefix/'bin/lanvideo'; cls.web=cls.prefix/'share/lanvideo/web'
        cls.marker='<!-- installed and relocated asset probe -->'
        index=cls.web/'index.html'; index.write_text(index.read_text()+cls.marker)
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()
    def launch(self,data,**values):
        with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        log=(data/'run.log').open('a')
        env=media.auth.environment(data,LANVIDEO_PORT=str(port),LD_LIBRARY_PATH=LIBS,**values)
        process=subprocess.Popen([str(self.binary)],env=env,cwd=self.root,stdout=log,stderr=log)
        def stop():
            if process.poll() is None: process.send_signal(signal.SIGTERM); process.wait(timeout=10)
            log.close(); self.assertEqual(process.returncode,0)
        self.addCleanup(stop)
        for _ in range(100):
            try:
                response=self.request(port,'GET','/api/health')
                if response[0]==200: return port,process,stop
            except OSError: pass
            if process.poll() is not None: raise AssertionError('installed process failed to start')
            time.sleep(.05)
        raise AssertionError('installed service readiness timeout')
    def request(self,port,method,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
        try:
            connection.request(method,path,body,headers or {}); response=connection.getresponse()
            return response.status,response.read(),dict(response.headers)
        finally: connection.close()
    def test_01_install_relocation_assets_health_authentication_and_restart(self):
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            data=pathlib.Path(folder); port,process,stop=self.launch(data)
            code,content,_=self.request(port,'GET','/'); self.assertEqual(code,200); self.assertIn(self.marker.encode(),content)
            self.assertEqual(self.request(port,'GET','/share/app.js')[1],(self.web/'share.js').read_bytes())
            self.assertEqual(self.request(port,'GET','/api/admin/status')[0],401)
            self.assertEqual(self.request(port,'GET','/api/health')[0],200)
            stop(); port,_,stop=self.launch(data)
            self.assertEqual(self.request(port,'GET','/api/bootstrap/status')[1],b'{"initialized":false}')
            self.assertEqual((data/'lanvideo.db').stat().st_mode&0o077,0)
            self.assertEqual((data/'proxy.key').stat().st_mode&0o777,0o600)
            stop()
    def test_02_invalid_web_root_and_symlink_asset_rejected(self):
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            data=pathlib.Path(folder); assets=data/'assets'; assets.mkdir()
            import shutil
            for path in self.web.iterdir(): shutil.copyfile(path,assets/path.name)
            (assets/'app.js').unlink(); (assets/'app.js').symlink_to(self.web/'app.js')
            for web in ['relative',str(data/'missing'),str(assets)]:
                result=subprocess.run([str(self.binary)],env=media.auth.environment(data,LD_LIBRARY_PATH=LIBS,LANVIDEO_WEB_ROOT=web),cwd=self.root,capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,1); self.assertEqual(json.loads(result.stdout)['event'],'startup_failure')
    def test_03_install_payload_excludes_private_state_and_test_executables(self):
        files=[str(path.relative_to(self.prefix)) for path in self.prefix.rglob('*') if path.is_file()]
        self.assertIn('bin/lanvideo',files); self.assertIn('share/doc/lanvideo/LICENSE',files)
        self.assertIn('share/doc/lanvideo/docs/js-sha256-LICENSE.txt',files)
        self.assertIn('share/doc/lanvideo/deploy/lanvideo.service.example',files)
        self.assertFalse(any('/var/' in path or path.endswith(('.db','.key','.pem','.torrent')) or 'bt_fixture' in path for path in files))

if __name__=='__main__': unittest.main(verbosity=2)
