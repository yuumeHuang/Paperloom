"""Same-origin WebDAV relay for the PC reader; credentials remain request-scoped."""
from __future__ import annotations
import json
import base64
from urllib.parse import quote, urlsplit, urlunsplit
import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import Field, model_validator
from .models import Model

MAX_FILE=180*1024*1024
MAX_TEXT=2*1024*1024

class DAVConfig(Model):
    url: str = Field(max_length=2000)
    username: str = Field(default='',max_length=500)
    password: str = Field(default='',max_length=2000)
    allowHttp: bool = False
    @model_validator(mode='after')
    def valid_url(self):
        try:
            parts=urlsplit(self.url)
            port=parts.port
        except ValueError:
            raise ValueError('WebDAV 地址无效')
        if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or any(c.isspace() or c=='\\' for c in self.url):
            raise ValueError('WebDAV 地址无效，不应包含登录信息或查询参数')
        if parts.scheme=='http' and not self.allowHttp:
            raise ValueError('HTTP 连接需要主动允许明文传输')
        self.url=urlunsplit((parts.scheme,parts.netloc,parts.path.rstrip('/')+'/', '', ''))
        return self

class DAVRequest(DAVConfig):
    method: str
    path: str = Field(default='',max_length=600)
    headers: dict[str,str] = Field(default_factory=dict)
    data: str = Field(default='',max_length=MAX_TEXT)

def target(config,path,collection=False):
    if path and any(not part or part in ('.','..') or any(c in part for c in '\\:%\x00') for part in path.split('/')):
        raise HTTPException(422,'同步资源路径无效')
    if any(ord(c)<32 for c in path):raise HTTPException(422,'同步资源路径无效')
    url=config.url+'/'.join(quote(p,safe='') for p in path.split('/'))
    return url.rstrip('/')+'/' if collection else url

def headers_for(value):
    allowed={'content-type','if-match','if-none-match','depth'}
    result={}
    for key,data in value.items():
        if key.lower() not in allowed or not isinstance(data,str) or len(data)>400 or '\r' in data or '\n' in data:
            raise HTTPException(422,'同步请求头无效')
        result[key]=data
    return result

def response_headers(response):
    return {key:response.headers[key] for key in ('etag','content-length','content-type','dav','last-modified') if key in response.headers}

def client(config):
    return httpx.AsyncClient(auth=httpx.BasicAuth(config.username,config.password) if config.username or config.password else None,
                             follow_redirects=False,timeout=httpx.Timeout(90,connect=25))

def transfer_config(value):
    try:
        if not value or len(value)>24000:raise ValueError()
        request=DAVRequest.model_validate_json(base64.b64decode(value,validate=True))
        headers_for(request.headers);target(request,request.path)
        return request
    except (ValueError,TypeError):raise HTTPException(422,'WebDAV 传输配置无效')

router=APIRouter(prefix='/api/webdav',tags=['WebDAV'])

@router.post('/request')
async def metadata(value:DAVRequest):
    method=value.method.upper()
    if method not in ('GET','HEAD','PUT','PROPFIND','MKCOL','OPTIONS'):raise HTTPException(422,'不支持的 WebDAV 方法')
    data=value.data.encode('utf-8')
    if len(data)>MAX_TEXT:raise HTTPException(413,'同步状态最大 2 MB')
    try:
        async with client(value) as upstream:
            async with upstream.stream(method,target(value,value.path,method in ('PROPFIND','MKCOL')),headers=headers_for(value.headers),content=data or None) as response:
                body=bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body)>MAX_TEXT:raise HTTPException(413,'WebDAV 目录或状态响应超限')
                return {'status':response.status_code,'headers':response_headers(response),'body':body.decode('utf-8',errors='replace')}
    except httpx.HTTPError:raise HTTPException(502,'WebDAV 连接失败，请检查地址、TLS 证书和网络')

@router.post('/upload')
async def upload(request:Request,x_paperloom_dav:str=Header(default='')):
    value=transfer_config(x_paperloom_dav)
    if value.method!='PUT' or not value.path.endswith('.folio'):raise HTTPException(422,'文献上传路径无效')
    async def chunks():
        total=0
        async for chunk in request.stream():
            total+=len(chunk)
            if total>MAX_FILE:raise HTTPException(413,'文献包最大 180 MB')
            yield chunk
    upstream_headers=headers_for(value.headers)
    if request.headers.get('content-length'):
        try:
            size=int(request.headers['content-length'])
            if size<0 or size>MAX_FILE:raise ValueError()
            upstream_headers['Content-Length']=str(size)
        except ValueError:raise HTTPException(413,'文献包大小超限')
    try:
        async with client(value) as upstream:
            async with upstream.stream('PUT',target(value,value.path),headers=upstream_headers,content=chunks()) as response:
                return {'status':response.status_code,'headers':response_headers(response),'body':''}
    except httpx.HTTPError:raise HTTPException(502,'WebDAV 上传失败，请检查网络后重试')

@router.post('/download')
async def download(x_paperloom_dav:str=Header(default='')):
    value=transfer_config(x_paperloom_dav)
    if value.method!='GET' or not value.path.endswith('.folio'):raise HTTPException(422,'文献下载路径无效')
    upstream=client(value)
    try:response=await upstream.send(upstream.build_request('GET',target(value,value.path),headers={'Accept-Encoding':'identity'}),stream=True)
    except httpx.HTTPError:
        await upstream.aclose();raise HTTPException(502,'WebDAV 下载连接失败')
    if response.status_code!=200:
        status=response.status_code;await response.aclose();await upstream.aclose()
        return StreamingResponse(iter(()),status_code=status)
    try:
        size=int(response.headers.get('content-length','0'))
        if size<0 or size>MAX_FILE:raise ValueError()
    except ValueError:
        await response.aclose();await upstream.aclose();raise HTTPException(413,'文献包大小超限')
    async def content():
        total=0
        try:
            async for chunk in response.aiter_bytes():
                total+=len(chunk)
                if total>MAX_FILE:raise ValueError('文献包下载超限')
                yield chunk
        finally:await response.aclose();await upstream.aclose()
    headers={key:value for key,value in response_headers(response).items() if key in ('etag','content-length')}
    return StreamingResponse(content(),media_type='application/zip',headers=headers)
