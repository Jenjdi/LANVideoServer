#!/usr/bin/env python3
"""Generate a private, standalone Nginx HTTPS entry config; never print its secret.
Existing config files are not overwritten. Same-host proxy and application only.
"""
import argparse, os, pathlib, re, secrets, stat, urllib.parse

def origin(value):
    if not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?',value): raise ValueError('HTTPS origin without path required')
    parsed=urllib.parse.urlsplit(value)
    if not parsed.hostname or parsed.hostname.startswith('.') or parsed.hostname.endswith('.') or '..' in parsed.hostname or len(parsed.hostname)>253: raise ValueError('invalid origin hostname')
    port=443 if parsed.port is None else parsed.port
    if not 1<=port<=65535: raise ValueError('invalid TLS port')
    return parsed,port

def safe_path(value):
    path=pathlib.Path(value).absolute()
    if not re.fullmatch(r'[A-Za-z0-9/_.-]+',str(path)): raise ValueError('deployment paths require ASCII letters, numbers, / _ . -')
    return path

def private_write(path,text):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as output: output.write(text); output.flush(); os.fsync(output.fileno())

def configure(data, output, public_origin, cert, key, upstream_port=8080, listen='0.0.0.0', upload_mib=512):
    parsed,port=origin(public_origin)
    if listen not in ('127.0.0.1','0.0.0.0'): raise ValueError('listen must be 127.0.0.1 or 0.0.0.0')
    if not 1<=upstream_port<=65535 or not 1<=upload_mib<=4096: raise ValueError('invalid upstream port or upload limit')
    data,output,cert,key=map(safe_path,(data,output,cert,key))
    if not cert.is_file() or not key.is_file(): raise ValueError('existing TLS certificate and private key required')
    data.mkdir(parents=True,exist_ok=True)
    if data.is_symlink() or stat.S_IMODE(data.stat().st_mode)&0o077: raise ValueError('data directory must be private 0700 and not a symlink')
    key_file=data/'ingress.key'
    try: private_write(key_file,secrets.token_hex(32))
    except FileExistsError: pass
    fd=os.open(key_file,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'r') as source:
        info=os.fstat(source.fileno()); ingress=source.read(65)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.geteuid() or info.st_nlink!=1 or not re.fullmatch('[0-9a-f]{64}',ingress): raise ValueError('invalid private ingress key')
    cache=data/'nginx-cache'; cache.mkdir(mode=0o700,exist_ok=True)
    # No request URIs, cookies, authorization headers or bodies are logged.
    text=f'''daemon off;
master_process off;
pid "{data}/nginx.pid";
error_log stderr warn;
events {{ worker_connections 128; }}
http {{
  access_log off;
  server_tokens off;
  client_body_temp_path "{cache}/body";
  proxy_temp_path "{cache}/proxy";
  fastcgi_temp_path "{cache}/fastcgi";
  uwsgi_temp_path "{cache}/uwsgi";
  scgi_temp_path "{cache}/scgi";
  client_header_timeout 10s;
  client_body_timeout 120s;
  send_timeout 15s;
  keepalive_timeout 15s;
  limit_conn_zone $binary_remote_addr zone=per_client:1m;
  server {{
    listen {listen}:{port} ssl;
    server_name {parsed.hostname};
    ssl_certificate "{cert}";
    ssl_certificate_key "{key}";
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_tickets off;
    client_max_body_size {upload_mib}m;
    limit_conn per_client 16;
    location / {{
      proxy_pass http://127.0.0.1:{upstream_port};
      proxy_http_version 1.1;
      proxy_set_header Connection "";
      proxy_set_header Host "{parsed.netloc}";
      proxy_set_header X-Lanvideo-Ingress "{ingress}";
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-Host "{parsed.netloc}";
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For "";
      proxy_buffering off;
      proxy_request_buffering off;
      proxy_connect_timeout 3s;
      proxy_read_timeout 15s;
      proxy_send_timeout 120s;
    }}
  }}
}}
'''
    private_write(output,text)
    return output

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('data','output','origin','cert','key'): parser.add_argument('--'+name,required=True)
    parser.add_argument('--upstream-port',type=int,default=8080)
    parser.add_argument('--listen',choices=['127.0.0.1','0.0.0.0'],default='0.0.0.0')
    parser.add_argument('--upload-mib',type=int,default=512)
    args=parser.parse_args(); os.umask(0o077)
    try:
        result=configure(args.data,args.output,args.origin,args.cert,args.key,args.upstream_port,args.listen,args.upload_mib)
        print('Private Nginx configuration created: '+str(result))
    except (OSError,ValueError) as error: parser.exit(1,'Configuration failed: '+str(error)+'\n')
