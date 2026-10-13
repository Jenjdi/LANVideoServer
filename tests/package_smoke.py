#!/usr/bin/env python3
"""Verify a real release archive's manifest, contents, start and clean shutdown."""
import argparse, hashlib, http.client, json, os, pathlib, signal, socket, subprocess, tarfile, tempfile, time

def verify(archive,libraries):
    with tempfile.TemporaryDirectory() as folder:
        root=pathlib.Path(folder)
        with tarfile.open(archive,'r:gz') as source:
            members=source.getmembers()
            if not members or len(members)>2000 or sum(m.size for m in members)>256*1024*1024: raise ValueError('invalid package bounds')
            if any(not(m.isfile() or m.isdir()) or m.size>64*1024*1024 for m in members): raise ValueError('invalid package member')
            source.extractall(root,filter='data')
        children=list(root.iterdir())
        if len(children)!=1 or not children[0].is_dir(): raise ValueError('one package root required')
        prefix=children[0]; entries={}
        for line in (prefix/'SHA256SUMS').read_text().splitlines():
            digest,name=line.split('  ',1); path=prefix/name
            if path.resolve().parent!=prefix.resolve() and prefix.resolve() not in path.resolve().parents: raise ValueError('manifest traversal')
            if name in entries or hashlib.sha256(path.read_bytes()).hexdigest()!=digest: raise ValueError('manifest integrity failure')
            entries[name]=digest
        actual={str(p.relative_to(prefix)) for p in prefix.rglob('*') if p.is_file()}-{'SHA256SUMS'}
        if actual!=set(entries): raise ValueError('unlisted/missing package files')
        if any(p.endswith(('.db','.key','.pem','.torrent')) or '/var/' in p or 'bt_fixture' in p for p in actual): raise ValueError('private state/test binary in package')
        info=json.loads((prefix/'BUILDINFO.json').read_text())
        if info['configuration']!='Release' or info['data_included']: raise ValueError('incorrect package metadata')
        moved=root/'relocated'; prefix.rename(moved); prefix=moved
        with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        environment={**os.environ,'LANVIDEO_BIND':'127.0.0.1','LANVIDEO_PORT':str(port),'LANVIDEO_DATA':str(root/'state'),'LANVIDEO_REMOTE_MODE':'local','LANVIDEO_PUBLIC_ORIGIN':'','LANVIDEO_WEB_ROOT':''}
        if libraries: environment['LD_LIBRARY_PATH']=libraries
        log=root/'process.log'
        with log.open('w') as output:
            process=subprocess.Popen([str(prefix/'bin/lanvideo')],cwd=root,env=environment,stdout=output,stderr=output)
            try:
                def request(path):
                    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
                    try:
                        connection.request('GET',path); response=connection.getresponse(); return response.status,response.read()
                    finally: connection.close()
                for _ in range(100):
                    try:
                        code,body=request('/api/health')
                        if code==200 and json.loads(body)['status']=='ok': break
                    except OSError: pass
                    if process.poll() is not None: raise ValueError('packaged process failed startup')
                    time.sleep(.05)
                else: raise ValueError('packaged readiness timeout')
                if request('/')[1]!=(prefix/'share/lanvideo/web/index.html').read_bytes(): raise ValueError('incorrect installed asset')
                if request('/share/app.js')[1]!=(prefix/'share/lanvideo/web/share.js').read_bytes(): raise ValueError('incorrect share asset')
                if request('/api/admin/status')[0]!=401 or request('/api/shares')[0]!=401: raise ValueError('anonymous permission boundary failed')
                if json.loads(request('/api/bootstrap/status')[1])['initialized']: raise ValueError('default admin in package')
            finally:
                if process.poll() is None: process.send_signal(signal.SIGTERM)
                process.wait(timeout=10)
            if process.returncode: raise ValueError('unclean packaged shutdown')
        return {'passed':True,'manifest_files':len(entries),'relocated':True,'health':200,'anonymous_status':401,'default_admin':False,'shutdown_exit_code':0,'runtime_libraries':'external provided directory' if libraries else 'system loader'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('archive'); parser.add_argument('--library-path',default=''); args=parser.parse_args()
    print(json.dumps(verify(args.archive,args.library_path),ensure_ascii=False))
