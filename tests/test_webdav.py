import base64,json
from fastapi.testclient import TestClient
from paperloom.server import create_app
from paperloom.webdav import DAVConfig,target
from dav_fixture import dav_server
import pytest

def encoded(config):return base64.b64encode(json.dumps(config,ensure_ascii=False).encode()).decode()

def test_webdav_relay_real_http_unicode_streams_and_conditional_writes(tmp_path):
    server,thread=dav_server(tmp_path/'dav')
    config={'url':f'http://127.0.0.1:{server.server_port}/dav/','username':'reader','password':'test-only-password','allowHttp':True}
    try:
        with TestClient(create_app(tmp_path/'jobs')) as client:
            def request(method,path='',headers=None,data=''):
                response=client.post('/api/webdav/request',json={**config,'method':method,'path':path,'headers':headers or {},'data':data});assert response.status_code==200;return response.json()
            assert request('PROPFIND',headers={'Depth':'0'})['status']==207
            assert request('MKCOL','研读')['status']==201
            value=request('PUT','研读/device.json',{'If-None-Match':'*'},'{"version":1}')
            assert value['status']==201
            assert request('PUT','研读/device.json',{'If-None-Match':'*'},'bad')['status']==412
            assert request('PUT','研读/device.json',{'If-Match':'"stale"'},'bad')['status']==412
            assert request('PUT','研读/device.json',{'If-Match':value['headers']['etag']},'{"version":2}')['status']==204
            data=b'PK-test-'+b'x'*(1024*1024+123)
            transfer={**config,'method':'PUT','path':'研读/paper.folio','headers':{'If-None-Match':'*'}}
            uploaded=client.post('/api/webdav/upload',content=data,headers={'X-Paperloom-DAV':encoded(transfer)})
            assert uploaded.status_code==200 and uploaded.json()['status']==201
            transfer['method']='GET'
            downloaded=client.post('/api/webdav/download',headers={'X-Paperloom-DAV':encoded(transfer)})
            assert downloaded.status_code==200 and downloaded.content==data
            assert 'test-only-password' not in json.dumps(dict(downloaded.headers))
            assert not any('test-only-password' in p.read_text() for p in (tmp_path/'jobs').rglob('*.json'))
            denied=client.post('/api/webdav/request',json={**config,'password':'wrong','method':'PROPFIND'})
            assert denied.json()['status']==401
            assert client.post('/api/webdav/request',json={**config,'method':'DELETE'}).status_code==422
            assert client.post('/api/webdav/request',json={**config,'method':'GET','path':'../outside'}).status_code==422
    finally:server.shutdown();server.server_close();thread.join(timeout=3)

def test_webdav_requires_explicit_http_opt_in_and_rejects_credentials_in_url():
    for url in ['file:///etc/secret','https://reader:secret@example.org/dav/','https://example.org/dav/?token=secret','http://example.org/dav/']:
        with pytest.raises(ValueError):DAVConfig(url=url)
    config=DAVConfig(url='https://example.org/dav/')
    for path in ['../secret','/secret','x/%2fsecret','x\\secret']:
        with pytest.raises(Exception):target(config,path)

def test_webdav_api_is_protected_by_same_origin_and_pc_token(tmp_path):
    with TestClient(create_app(tmp_path/'jobs','x'*32)) as client:
        value={'url':'https://example.org/dav/','method':'PROPFIND'}
        assert client.post('/api/webdav/request',json=value).status_code==401
        assert client.post('/api/webdav/request',json=value,headers={'Authorization':'Bearer '+'x'*32,'Origin':'https://other.example'}).status_code==403
