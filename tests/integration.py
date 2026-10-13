#!/usr/bin/env python3
"""Real process / HTTP / database integration tests, no external Python packages."""
import concurrent.futures, http.client, json, os, pathlib, signal, socket, sqlite3, stat, subprocess, sys, tempfile, time, unittest, urllib.request, urllib.error
BINARY = sys.argv.pop(1)
class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0)); cls.port = s.getsockname()[1]
        cls.base = f'http://127.0.0.1:{cls.port}'
        cls.log = open(pathlib.Path(cls.temp.name)/'server.log', 'w+')
        cls.process = subprocess.Popen([BINARY], env={**os.environ,'LANVIDEO_PORT':str(cls.port),'LANVIDEO_DATA':cls.temp.name},stdout=cls.log,stderr=cls.log)
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base+'/api/health',timeout=1).close(); break
            except OSError:
                if cls.process.poll() is not None: cls.log.seek(0); raise RuntimeError(cls.log.read())
                time.sleep(.05)
        else: raise RuntimeError('server did not become ready')
    @classmethod
    def tearDownClass(cls):
        cls.process.send_signal(signal.SIGTERM)
        try: result=cls.process.wait(timeout=10)
        except subprocess.TimeoutExpired: cls.process.kill(); raise
        cls.log.close(); cls.temp.cleanup()
        if result != 0: raise AssertionError(f'unclean shutdown: {result}')
    def test_01_health(self):
        with urllib.request.urlopen(self.base+'/api/health') as r:
            self.assertEqual(r.status,200)
            self.assertEqual(json.load(r)['status'],'ok')
            self.assertEqual(r.headers['X-Content-Type-Options'],'nosniff')
        self.assertTrue((pathlib.Path(self.temp.name)/'lanvideo.db').is_file())
    def test_02_sqlite_and_private_files(self):
        with sqlite3.connect(pathlib.Path(self.temp.name)/'lanvideo.db') as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'settings','audit'}.issubset(names))
        for name in ['', 'lanvideo.db']:
            mode = stat.S_IMODE((pathlib.Path(self.temp.name)/name).stat().st_mode)
            self.assertEqual(mode & 0o077, 0)
    def test_03_concurrent_requests(self):
        def health(_):
            with urllib.request.urlopen(self.base+'/api/health', timeout=5) as r: return json.load(r)['status']
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            self.assertEqual(list(pool.map(health,range(64))),['ok']*64)
    def test_04_json_errors(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(self.base+'/missing')
        self.assertEqual(context.exception.code,404)
        self.assertIn('error',json.load(context.exception))
        context.exception.close()
    def test_05_bind_failure(self):
        result=subprocess.run([BINARY],env={**os.environ,'LANVIDEO_PORT':str(self.port),'LANVIDEO_DATA':self.temp.name},capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,1)
        self.assertEqual(json.loads(result.stdout)['event'],'startup_failure')
    def test_06_invalid_configuration(self):
        cases = [('LANVIDEO_PORT','0'),('LANVIDEO_PORT','65536'),('LANVIDEO_PORT','8080junk'),('LANVIDEO_BIND','invalid'),('LANVIDEO_LOG_LEVEL','verbose'),('LANVIDEO_PUBLIC_ORIGIN','http://example.com'),('LANVIDEO_SESSION_TTL','0'),('LANVIDEO_SESSION_TTL','86401')]
        for key,value in cases:
            with self.subTest(key=key,value=value):
                result=subprocess.run([BINARY],env={**os.environ,'LANVIDEO_DATA':self.temp.name,key:value},capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,1)
                self.assertEqual(json.loads(result.stdout)['level'],'error')
    def test_07_restart_persistence_and_error_log_level(self):
        with tempfile.TemporaryDirectory() as data:
            def cycle():
                with socket.socket() as s:
                    s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
                process=subprocess.Popen([BINARY],env={**os.environ,'LANVIDEO_PORT':str(port),'LANVIDEO_DATA':data,'LANVIDEO_LOG_LEVEL':'error'},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    for _ in range(100):
                        try:
                            urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health',timeout=1).close(); break
                        except OSError:
                            if process.poll() is not None: raise AssertionError('startup failed')
                            time.sleep(.05)
                    else: raise AssertionError('startup timeout')
                finally:
                    process.send_signal(signal.SIGTERM)
                    stdout,stderr=process.communicate(timeout=10)
                self.assertEqual(process.returncode,0)
                self.assertEqual(stdout,'')
                self.assertEqual(stderr,'')
            cycle()
            with sqlite3.connect(pathlib.Path(data)/'lanvideo.db') as db:
                db.execute("INSERT INTO settings(key,value) VALUES('test','persisted')")
            cycle()
            with sqlite3.connect(pathlib.Path(data)/'lanvideo.db') as db:
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='test'").fetchone()[0],'persisted')
    def test_08_oversized_payload(self):
        # The service now rejects at headers, before accepting a large body.
        # Read the real 413 rather than racing urllib's sendall against early close.
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            connection.putrequest('POST','/missing')
            connection.putheader('Content-Length',str(8*1024*1024+1)); connection.endheaders()
            response=connection.getresponse()
            self.assertEqual(response.status,413)
            self.assertIn('error',json.loads(response.read()))
        finally: connection.close()
if __name__ == '__main__': unittest.main(verbosity=2)
