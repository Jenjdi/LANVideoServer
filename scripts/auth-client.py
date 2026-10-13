#!/usr/bin/env python3
"""Interactive API client; credentials stay in memory and never enter arguments/history."""
import argparse
import getpass
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

def main():
    parser=argparse.ArgumentParser(description='LANVideoServer phase-2 authentication client')
    parser.add_argument('--url',default='http://127.0.0.1:8080',help='HTTP local or HTTPS public origin, without credentials or path')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('me'); commands.add_parser('users'); commands.add_parser('change-password')
    create=commands.add_parser('create'); create.add_argument('username'); create.add_argument('--role',choices=['user','admin'],default='user')
    for name in ['disable','enable','delete','reset-password']:
        commands.add_parser(name).add_argument('id',type=int)
    role=commands.add_parser('set-role'); role.add_argument('id',type=int); role.add_argument('role',choices=['admin','user'])
    options=parser.parse_args()
    origin=urllib.parse.urlsplit(options.url)
    if origin.scheme not in ('http','https') or not origin.hostname or origin.username or origin.password or origin.path not in ('','/') or origin.query or origin.fragment:
        parser.error('URL must be an HTTP(S) origin without credentials, path or query')
    if not sys.stdin.isatty(): parser.error('run in a terminal so password input can be hidden')
    if origin.scheme=='http': print('当前使用 HTTP。公网访问应配置 HTTPS；本客户端不提供 TLS 终止。',file=sys.stderr)
    jar=http.cookiejar.CookieJar()
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base=options.url.rstrip('/')
    csrf=None
    def request(method,path,data=None):
        headers={}
        payload=None
        if data is not None:
            headers['Content-Type']='application/json'; payload=json.dumps(data).encode()
        if csrf and method!='GET': headers['X-CSRF-Token']=csrf
        req=urllib.request.Request(base+path,data=payload,headers=headers,method=method)
        with opener.open(req,timeout=10) as response: return json.load(response)
    def new_password():
        value=getpass.getpass('New password (12+ bytes): ')
        if getpass.getpass('Confirm new password: ')!=value: raise ValueError('passwords do not match')
        return value
    username=input('Login username: ')
    password=getpass.getpass('Login password: ')
    authenticated=False
    try:
        login=request('POST','/api/login',{'username':username,'password':password})
        csrf=login['csrf_token']; authenticated=True
        operation=options.command
        if operation=='me': result=request('GET','/api/me')['user']
        elif operation=='users': result=request('GET','/api/users')
        elif operation=='create': result=request('POST','/api/users',{'username':options.username,'password':new_password(),'role':options.role})
        elif operation=='change-password': result=request('POST','/api/password',{'old_password':password,'new_password':new_password()})
        elif operation=='reset-password': result=request('POST',f'/api/users/{options.id}/password',{'password':new_password()})
        elif operation=='set-role': result=request('PATCH',f'/api/users/{options.id}',{'role':options.role})
        elif operation in ('disable','enable'): result=request('PATCH',f'/api/users/{options.id}',{'disabled':operation=='disable'})
        else: result=request('DELETE',f'/api/users/{options.id}')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        if authenticated:
            try: request('POST','/api/logout')
            except urllib.error.HTTPError as exc:
                if exc.code!=401: raise
                exc.close()

if __name__=='__main__':
    try: main()
    except urllib.error.HTTPError as exc:
        try: message=json.load(exc).get('error','request failed')
        except (ValueError,AttributeError): message='request failed'
        finally: exc.close()
        print(f'HTTP {exc.code}: {message}',file=sys.stderr); sys.exit(1)
    except (OSError,ValueError,EOFError) as exc:
        print(f'Client error: {type(exc).__name__}',file=sys.stderr); sys.exit(1)
    except KeyboardInterrupt:
        print('\nCancelled.',file=sys.stderr); sys.exit(1)
