"""Private byte adapter for native loopback-only encrypted sync requests."""
import io
import os
from pathlib import Path
import re
import struct
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

LIMIT = 128*1024*1024
METHODS = {'GET':1,'POST':2,'PUT':3,'HEAD':4}
HEADERS = {'Content-Type','Authorization','X-Daochi-User','X-Daochi-Tx','X-Daochi-Signature'}
HASH = r'[0-9a-f]{64}'


def program():
    root=Path(__file__).resolve().parents[1]
    path=next((p for p in [root/'sync_http',root/'build/sync-http/sync_http'] if p.is_file()),None)
    if path is None:
        raise ValueError('Build or install native Photos transport before backing up or restoring')
    return path


def route(path,method):
    if type(path) is not str or type(method) is not str: return False
    if method=='GET' and (path=='/api/v1/node' or re.fullmatch('/api/v1/sync/challenge\\?user_id='+HASH,path)): return True
    if method=='POST' and path in {'/api/v1/sync','/api/v1/sync/login','/api/v1/account/devices'}: return True
    return method in {'GET','PUT','HEAD'} and re.fullmatch('/api/v1/blobs/photos/'+HASH,path) is not None


def frame(node,path,method='GET',body=None,headers=None,limit=LIMIT,timeout_ms=60000):
    try:
        origin=urlsplit(node)
        if (origin.scheme!='http' or origin.hostname!='127.0.0.1' or not origin.port or
                origin.path or origin.query or origin.fragment or origin.username or origin.password):
            raise ValueError()
        if (not route(path,method) or (body is not None and type(body) is not bytes) or
                type(limit) is not int or not 0<=limit<=LIMIT or
                type(timeout_ms) is not int or not 1<=timeout_ms<=60000): raise ValueError()
        body=b'' if body is None else body
        if len(body)>LIMIT or (method in {'GET','HEAD'} and body): raise ValueError()
        headers={} if headers is None else headers
        if type(headers) is not dict or len(headers)>5: raise ValueError()
        path_bytes=path.encode('ascii'); pairs=[]
        for name,value in headers.items():
            if name not in HEADERS or type(value) is not str: raise ValueError()
            name=name.encode('ascii');value=value.encode('ascii')
            if len(value)>=8192 or any(b<32 or b>126 for b in value): raise ValueError()
            pairs.append(struct.pack('!HH',len(name),len(value))+name+value)
        return struct.pack('!BBHHBIII',1,METHODS[method],origin.port,len(path_bytes),len(pairs),len(body),limit,timeout_ms)+path_bytes+b''.join(pairs)+body
    except (ValueError,TypeError,KeyError,UnicodeError):
        raise ValueError('Invalid Photos loopback sync request') from None


def request(node,path,method='GET',body=None,headers=None,limit=LIMIT,timeout_ms=60000):
    data=frame(node,path,method,body,headers,limit,timeout_ms)
    environment={k:v for k,v in os.environ.items() if k not in
                 {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'}}
    try:
        result=subprocess.run(['prlimit','--core=0','--as=536870912','--cpu=65','--',str(program())],
                              input=data,capture_output=True,env=environment,timeout=timeout_ms/1000+5)
    except (OSError,subprocess.TimeoutExpired):
        raise URLError('Photos local transport is unavailable; originals and keys were preserved') from None
    if result.returncode==3:
        raise ValueError('Local node response exceeded its safe limit')
    if result.returncode!=0 or result.stderr or len(result.stdout)<6:
        raise URLError('Photos local transport could not finish; originals and keys were preserved')
    status,size=struct.unpack('!HI',result.stdout[:6]);body=result.stdout[6:]
    if not 100<=status<=599 or size!=len(body) or size>limit:
        raise ValueError('Invalid native Photos transport response')
    if 300<=status<400:
        raise ValueError('Photos forbids redirects outside its pinned local node')
    if status>=400:
        raise HTTPError(node+path,status,'Local Photos node request failed',{},io.BytesIO(body))
    return body
