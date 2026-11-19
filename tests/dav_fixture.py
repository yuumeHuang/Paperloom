"""Loopback-only RFC-style WebDAV fixture for integration checks and browser QA."""
from __future__ import annotations
import base64,hashlib,threading
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import unquote,urlsplit,quote
from xml.sax.saxutils import escape

AUTH='Basic '+base64.b64encode(b'reader:test-only-password').decode()
def dav_server(root:Path,port=0):
    root.mkdir(parents=True,exist_ok=True)
    lock=threading.RLock()
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def send(self,status,body=b'',headers=None):
            self.send_response(status)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Access-Control-Allow-Origin',self.headers.get('Origin','*'))
            self.send_header('Access-Control-Allow-Methods','GET,HEAD,PUT,MKCOL,PROPFIND,OPTIONS')
            self.send_header('Access-Control-Allow-Headers','Authorization,Content-Type,Depth,If-Match,If-None-Match')
            self.send_header('Access-Control-Expose-Headers','ETag,DAV,Content-Length')
            for key,value in (headers or {}).items():self.send_header(key,value)
            self.end_headers()
            if self.command!='HEAD':self.wfile.write(body)
        def resource_path(self):
            raw=unquote(urlsplit(self.path).path)
            if not raw.startswith('/dav/') or any(part in ('.','..') for part in raw.split('/')):return None
            path=(root/raw.removeprefix('/dav/')).resolve()
            if not path.is_relative_to(root.resolve()):return None
            return path
        def valid(self):
            if self.headers.get('Authorization')!=AUTH:self.send(401,headers={'WWW-Authenticate':'Basic realm="test"'});return None
            path=self.resource_path()
            if path is None:self.send(403)
            return path
        def read_body(self):
            length=int(self.headers.get('Content-Length','0'))
            if length>190*1024*1024:raise ValueError()
            return self.rfile.read(length)
        def etag(self,path):return '"'+hashlib.sha256(path.read_bytes()).hexdigest()+'"'
        def do_OPTIONS(self):self.send(204,headers={'DAV':'1'})
        def do_MKCOL(self):
            self.read_body();path=self.valid()
            if path is None:return
            with lock:
                if path.exists():self.send(405);return
                if not path.parent.exists():self.send(409);return
                path.mkdir();self.send(201)
        def do_PROPFIND(self):
            self.read_body();path=self.valid()
            if path is None:return
            if not path.exists():self.send(404);return
            nodes=[path]
            if self.headers.get('Depth')=='1' and path.is_dir():nodes.extend(sorted(path.iterdir()))
            values=[]
            for node in nodes:
                relative=node.relative_to(root).as_posix();href='/dav/'+('' if relative=='.' else quote(relative))
                if node.is_dir() and not href.endswith('/'):href+='/'
                prop='<d:collection/>' if node.is_dir() else ''
                etag='' if node.is_dir() else '<d:getetag>'+escape(self.etag(node))+'</d:getetag>'
                values.append(f'<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop><d:resourcetype>{prop}</d:resourcetype>{etag}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
            body=('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'+''.join(values)+'</d:multistatus>').encode()
            self.send(207,body,{'Content-Type':'application/xml; charset=utf-8','DAV':'1'})
        def do_GET(self):
            path=self.valid()
            if path is None:return
            if not path.is_file():self.send(404);return
            with lock:
                data=path.read_bytes();etag=self.etag(path)
                if self.headers.get('If-Match') and self.headers['If-Match'] not in ('*',etag):self.send(412);return
                self.send(200,data,{'Content-Type':'application/zip' if path.suffix=='.folio' else 'application/json','ETag':etag})
        do_HEAD=do_GET
        def do_PUT(self):
            data=self.read_body();path=self.valid()
            if path is None:return
            with lock:
                if not path.parent.exists():self.send(409);return
                exists=path.exists()
                if self.headers.get('If-None-Match')=='*' and exists:self.send(412);return
                if self.headers.get('If-Match') and (not exists or self.headers['If-Match'] not in ('*',self.etag(path))):self.send(412);return
                temp=path.with_name(path.name+'.temporary');temp.write_bytes(data);temp.replace(path)
                self.send(204 if exists else 201,headers={'ETag':self.etag(path)})
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    return server,thread
