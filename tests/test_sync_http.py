"""Exercise the native transport against disposable loopback fixtures."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import gzip
import json
import os
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from photos import sync_http
from photos.library import Library
from photos.sync import Client
from owner_fixture import ACCOUNT

BLOB='/api/v1/blobs/photos/'+'b'*64


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def respond(self,status,body,extra=None):
        self.send_response(status)
        for name,value in (extra or {}).items():self.send_header(name,value)
        self.send_header('Content-Length',str(len(body)));self.end_headers()
        if self.command!='HEAD':
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass

    def do_GET(self):
        self.server.calls.append((self.command,self.path,dict(self.headers)))
        if self.server.delay:time.sleep(self.server.delay)
        if self.path=='/api/v1/node':
            self.respond(self.server.status,self.server.content,self.server.extra)
        elif self.path==BLOB:
            if self.headers.get('Authorization')=='Bearer expired':self.respond(401,b'expired')
            else:self.respond(200,self.server.content)
        else:self.respond(404,b'missing')

    def do_HEAD(self):
        self.server.calls.append((self.command,self.path,dict(self.headers)))
        self.respond(200,b'x'*1234)

    def do_PUT(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.server.calls.append((self.command,self.path,dict(self.headers)))
        self.server.upload=body
        self.respond(201,body)

    def do_POST(self):
        self.do_PUT()


class SyncHTTPTest(unittest.TestCase):
    def setUp(self):
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.content=b'{"node_id":"'+b'a'*64+b'","known_nodes":[]}'
        self.server.calls=[];self.server.extra={};self.server.status=200
        self.server.delay=0
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.node='http://127.0.0.1:'+str(self.server.server_port)
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def test_json_binary_upload_large_signatures_and_head_with_no_body(self):
        self.assertEqual(json.loads(sync_http.request(self.node,'/api/v1/node'))['node_id'],'a'*64)
        body=bytes(range(256))*32768+b'\0private fixture'
        headers={'Content-Type':'application/octet-stream','Authorization':'Bearer fixture',
                 'X-Daochi-User':'a'*64,'X-Daochi-Tx':'x'*6000,'X-Daochi-Signature':'b'*4840}
        self.assertEqual(sync_http.request(self.node,BLOB,'PUT',body,headers),body)
        self.assertEqual(self.server.upload,body)
        for name,value in headers.items():self.assertEqual(self.server.calls[-1][2][name],value)
        self.assertEqual(sync_http.request(self.node,BLOB,'HEAD',limit=0),b'')
        self.server.content=b'\0\xffbinary\0'
        self.assertEqual(sync_http.request(self.node,BLOB),self.server.content)
        self.assertEqual(sync_http.request(self.node,'/api/v1/sync','POST',b'{}'),b'{}')

    def test_inherited_proxies_are_bypassed_and_redirects_never_followed(self):
        with patch.dict(os.environ,{'http_proxy':'http://127.0.0.1:1','HTTP_PROXY':'http://127.0.0.1:1',
                                    'ALL_PROXY':'http://127.0.0.1:1','all_proxy':'http://127.0.0.1:1',
                                    'no_proxy':'','NO_PROXY':''}):
            self.assertTrue(sync_http.request(self.node,'/api/v1/node'))
        self.server.content=b''
        for status in [301,302,303,307,308]:
            for location in [self.node+BLOB,'https://example.invalid/private']:
                self.server.status=status;self.server.extra={'Location':location};before=len(self.server.calls)
                with self.assertRaisesRegex(ValueError,'redirects'):sync_http.request(self.node,'/api/v1/node')
                self.assertEqual(len(self.server.calls),before+1)

    def test_response_bounds_include_decoded_compressed_bytes_and_exact_limit(self):
        self.server.content=b'x'*4096
        self.assertEqual(len(sync_http.request(self.node,'/api/v1/node',limit=4096)),4096)
        with self.assertRaisesRegex(ValueError,'safe limit'):sync_http.request(self.node,'/api/v1/node',limit=4095)
        self.server.content=gzip.compress(b'x'*4096);self.server.extra={'Content-Encoding':'gzip'}
        self.assertEqual(len(sync_http.request(self.node,'/api/v1/node',limit=4096)),4096)
        with self.assertRaisesRegex(ValueError,'safe limit'):sync_http.request(self.node,'/api/v1/node',limit=4095)

    def test_nonlocal_urls_invalid_routes_and_injected_headers_never_send_requests(self):
        for node in ['https://127.0.0.1:1','http://localhost:1','http://127.0.0.2:1','http://127.0.0.1',
                     'http://user@127.0.0.1:1',self.node+'/',self.node+'?secret=x',self.node+'#part']:
            with self.assertRaises(ValueError):sync_http.request(node,'/api/v1/node')
        for path in ['/api/v1/node?x=x','/api/v1/../node','//example.invalid','/api/v1/node\0',
                     '/api/v1/blobs/photos/'+'B'*64,'/api/v1/sync/challenge?user_id=x']:
            with self.assertRaises(ValueError):sync_http.request(self.node,path)
        for headers in [{'Host':'example.invalid'},{'Authorization':'Bearer a\r\nInjected: x'},
                        {'Content-Type':'\0bad'},{'X-Daochi-Tx':'x'*8192}]:
            with self.assertRaises(ValueError):sync_http.request(self.node,'/api/v1/node',headers=headers)
        self.assertEqual(self.server.calls,[])

    def test_raw_malformed_and_appended_frames_are_rejected_before_network_access(self):
        valid=sync_http.frame(self.node,'/api/v1/node')
        header=sync_http.frame(self.node,'/api/v1/node',headers={'Authorization':'Bearer fixture'})
        cases=[b'',valid[:18],valid[:-1],valid+b'extra',b'\0'+valid[1:],valid[:1]+b'\5'+valid[2:],
               valid[:2]+b'\0\0'+valid[4:],valid[:11]+struct.pack('!I',sync_http.LIMIT+1)+valid[15:],
               header.replace(b'Bearer fixture',b'Bearer\rfixture'),
               valid.replace(b'/api/v1/node',b'/api/v1/else')]
        for data in cases:
            result=subprocess.run([str(sync_http.program())],input=data,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,2)
            self.assertEqual(result.stdout,b'');self.assertEqual(result.stderr,b'')
        self.assertEqual(self.server.calls,[])

    def test_http_errors_reauthentication_and_pinned_identity_keep_existing_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            library=Library(Path(folder)/'library')
            try:
                client=Client(library,dict(node=self.node,node_id='a'*64),account=ACCOUNT)
                identity=client.identity_path.read_bytes();device=client.device_path.read_bytes()
                client.check_node()
                self.server.content=json.dumps(dict(node_id='c'*64)).encode()
                with self.assertRaisesRegex(ValueError,'identity mismatch'):client.check_node()
                self.server.content=json.dumps(dict(node_id='a'*64,known_nodes=[dict(sync=dict(direction='push',apps=['photos']))])).encode()
                with self.assertRaisesRegex(ValueError,'outward'):client.check_node()
                # The account's shared photos are refused on such a node as well.
                self.server.content=json.dumps(dict(node_id='a'*64,known_nodes=[dict(sync=dict(direction='bidirectional',apps=['entity']))])).encode()
                with self.assertRaisesRegex(ValueError,'outward'):client.check_node()
                client.tokens[ACCOUNT.account]='expired';self.server.content=b'restored fixture'
                def login(account):client.tokens[account.account]='renewed'
                with patch.object(client,'login',side_effect=login) as authenticate:
                    self.assertEqual(client.request(BLOB,binary=True),b'restored fixture')
                    self.assertEqual(authenticate.call_count,1)
                self.server.status=404
                with self.assertRaises(HTTPError) as error:client.raw('/api/v1/node')
                self.assertEqual(error.exception.code,404)
                self.assertEqual(client.identity_path.read_bytes(),identity)
                self.assertEqual(client.device_path.read_bytes(),device)
            finally:library.db.close()

    def test_interrupted_transport_has_no_fallback_and_malformed_reply_is_not_accepted(self):
        for error in [OSError(),subprocess.TimeoutExpired('sync_http',65)]:
            with patch.object(sync_http.subprocess,'run',side_effect=error):
                with self.assertRaisesRegex(URLError,'preserved'):sync_http.request(self.node,'/api/v1/node')
        for data in [struct.pack('!HI',200,10)+b'partial',struct.pack('!HI',0,0)]:
            result=subprocess.CompletedProcess([],0,data,b'')
            with patch.object(sync_http.subprocess,'run',return_value=result):
                with self.assertRaises(ValueError):sync_http.request(self.node,'/api/v1/node')

    def test_native_node_policy_permits_only_no_outward_photos_and_preserves_identity(self):
        def node(peers):
            return json.dumps(dict(node_id='a'*64,known_nodes=peers,description='私有 nodo')).encode()
        allowed=[[],[dict(sync=dict(direction='pull',apps=[]))],
                 [dict(sync=dict(direction='pull',apps=['photos','*']))],
                 [dict(sync=dict(direction='none',apps=['photos']))],
                 [dict(sync=dict(direction='push',apps=['other']))],
                 [dict(sync=dict(direction='bidirectional',apps=['other']))]]
        for peers in allowed:
            self.server.content=node(peers)
            sync_http.check_node(self.node,'a'*64)
        for direction in ['push','bidirectional']:
            for apps in [[],['photos'],['*'],['other','photos']]:
                self.server.content=node([dict(sync=dict(direction=direction,apps=apps))])
                with self.assertRaisesRegex(ValueError,'outward'):
                    sync_http.check_node(self.node,'a'*64)
        for peer in [{},dict(sync=None),dict(sync=dict(direction='push'))]:
            self.server.content=node([peer])
            with self.assertRaisesRegex(ValueError,'outward'):
                sync_http.check_node(self.node,'a'*64)
        self.server.content=node([])
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            sync_http.check_node(self.node,'b'*64)

    def test_native_node_policy_refuses_duplicate_escaped_keys_malformed_types_and_invalid_utf8(self):
        prefix=b'{"node_id":"'+b'a'*64+b'",'
        bodies=[prefix+b'"node_id":"'+b'a'*64+b'"}',
                b'{"node_id":"'+b'a'*64+b'"}',
                prefix+b'"known_nodes":[],"known\\u005fnodes":[]}',
                prefix+b'"known_nodes":null}',prefix+b'"known_nodes":[null]}',
                prefix+b'"known_nodes":[{"sync":{},"sync":{"direction":"pull"}}]}',
                prefix+b'"known_nodes":[{"sync":{"direction":"pull","direction":"push"}}]}',
                prefix+b'"known_nodes":[{"sync":{"direction":"push","apps":"other"}}]}',
                prefix+b'"known_nodes":[{"sync":{"direction":"pull","apps":[null]}}]}',
                prefix+b'"known_nodes":[{"sync":{"direction":"pull","apps":["photos\\u0000"]}}]}',
                prefix+b'"known_nodes":[{"sync":{"direction":"unknown","apps":["other"]}}]}',
                prefix+b'"extra":"\xff"}',prefix+b'"known_nodes":[]} trailing',
                b'[]',b'null',prefix+b'"extra":'+b'['*66+b'0'+b']'*66+b'}']
        for body in bodies:
            self.server.content=body
            with self.assertRaisesRegex(ValueError,'policy'):
                sync_http.check_node(self.node,'a'*64)
        self.server.content=prefix+b'"known_nodes":[]}'
        sync_http.check_node(self.node,'a'*64)
        # Escapes are decoded before the sensitive field names are matched.
        self.server.content=b'{"node\\u005fid":"'+b'a'*64+b'","known\\u005fnodes":[]}'
        sync_http.check_node(self.node,'a'*64)

    def test_node_policy_binary_mode_rejects_invalid_frames_before_contacting_node(self):
        valid=sync_http.frame(self.node,'/api/v1/node',limit=1048576,node_id='a'*64)
        cases=[valid[:40],valid[:19]+valid[51:],valid+b'extra',
               valid[:11]+struct.pack('!I',1048577)+valid[15:],valid[:1]+b'\2'+valid[2:]]
        for data in cases:
            result=subprocess.run([str(sync_http.program())],input=data,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,2);self.assertFalse(result.stdout);self.assertFalse(result.stderr)
        self.assertEqual(self.server.calls,[])

    def test_actual_network_deadline_reaps_stalled_request_and_next_request_can_retry(self):
        self.server.delay=.3
        start=time.monotonic()
        with self.assertRaises(URLError):sync_http.request(self.node,'/api/v1/node',timeout_ms=50)
        self.assertLess(time.monotonic()-start,2)
        self.server.delay=0
        self.assertEqual(sync_http.request(self.node,'/api/v1/node'),self.server.content)


if __name__=='__main__':unittest.main()
