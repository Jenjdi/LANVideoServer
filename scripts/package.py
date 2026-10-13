#!/usr/bin/env python3
"""Package a built Release target and public documentation, never runtime state.

Runtime libraries remain external licensed dependencies. The archive is not
self-contained; see the deployment guide. Existing outputs are not overwritten.
"""
import argparse, gzip, hashlib, json, os, pathlib, platform, re, subprocess, tarfile, tempfile, shutil, sys

def revision(project):
    result=subprocess.run(['git','-C',str(project),'rev-parse','HEAD'],capture_output=True,text=True)
    return result.stdout.strip() if result.returncode==0 else None

def source_listing(project,output):
    files=[project/'CMakeLists.txt']
    for folder,extensions in [('include',{'.hpp'}),('src',{'.cpp'}),('web',{'.html','.css','.js'}),('scripts',{'.py','.sh'})]:
        files.extend(p for p in sorted((project/folder).iterdir()) if p.suffix in extensions and p.name!='sha256.min.js')
    parts=[]; lines=0
    for path in files:
        if path.is_symlink(): raise ValueError('source symlinks are not packaged')
        content=path.read_text(); lines+=len(content.splitlines())
        parts.append('\n===== '+str(path.relative_to(project))+' =====\n'+content)
    with output.open('x') as stream: stream.write('轻量级视频共享与传输软件 V1.0 — 项目源码\n'+''.join(parts))
    return {'files':len(files),'source_lines':lines}

def package(build,output,cmake):
    project=pathlib.Path(__file__).resolve().parents[1]; build=pathlib.Path(build).absolute(); output=pathlib.Path(output).absolute()
    cache=(build/'CMakeCache.txt').read_text()
    home=re.search(r'^CMAKE_HOME_DIRECTORY:INTERNAL=(.*)$',cache,re.M)
    if home is None or pathlib.Path(home[1]).resolve()!=project: raise ValueError('build directory belongs to another project')
    if not re.search(r'^CMAKE_BUILD_TYPE:STRING=Release$',cache,re.M): raise ValueError('package requires an already-built Release configuration')
    subprocess.run([cmake,'--build',str(build),'--target','lanvideo','-j2'],check=True,stdout=subprocess.DEVNULL)
    version=re.search(r'project\(LANVideoServer VERSION ([0-9.]+)',(project/'CMakeLists.txt').read_text())[1]
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as temp:
        prefix=pathlib.Path(temp)/('lanvideo-'+version)
        subprocess.run([cmake,'--install',str(build),'--prefix',str(prefix)],check=True,stdout=subprocess.DEVNULL)
        info={'name':'轻量级视频共享与传输软件','version':version,'configuration':'Release','platform':platform.system()+'-'+platform.machine(),'runtime_libraries':'external; no system/third-party binaries bundled','data_included':False,'git_revision':revision(project)}
        info['source']=source_listing(project,prefix/'share/doc/lanvideo/first-party-source.txt')
        (prefix/'BUILDINFO.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n')
        entries=[]
        for path in sorted(prefix.rglob('*')):
            if path.is_symlink(): raise ValueError('unexpected package symlink')
            if path.is_file(): entries.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+str(path.relative_to(prefix)))
        manifest='\n'.join(entries)+'\n'; (prefix/'SHA256SUMS').write_text(manifest)
        fingerprint=hashlib.sha256(manifest.encode()).hexdigest()[:12]
        archive=output/f'lanvideo-{version}-{platform.system().lower()}-{platform.machine()}-{fingerprint}.tar.gz'
        fd=os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'wb') as raw, gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as zipped, tarfile.open(fileobj=zipped,mode='w') as tar:
                for path in [prefix,*sorted(prefix.rglob('*'))]:
                    relative=path.relative_to(prefix.parent); member=tar.gettarinfo(str(path),arcname=str(relative))
                    member.uid=member.gid=0; member.uname=member.gname=''; member.mtime=0
                    if path.is_file():
                        with path.open('rb') as stream: tar.addfile(member,stream)
                    else: tar.addfile(member)
        except BaseException:
            archive.unlink(missing_ok=True); raise
        return {'archive':str(archive),'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'files':len(entries),'source':info['source']}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    sibling=pathlib.Path(sys.executable).with_name('cmake')
    default_cmake=os.environ.get('CMAKE') or shutil.which('cmake') or (str(sibling) if sibling.is_file() else 'cmake')
    parser.add_argument('--build',default='build-release'); parser.add_argument('--output',default='build/artifacts'); parser.add_argument('--cmake',default=default_cmake)
    args=parser.parse_args()
    try: print(json.dumps(package(args.build,args.output,args.cmake),ensure_ascii=False))
    except (OSError,ValueError,subprocess.CalledProcessError) as error: parser.exit(1,'Packaging failed: '+str(error)+'\n')
