from __future__ import annotations

import json
import httpx
import pytest
from fastapi.testclient import TestClient

from paperloom.discovery import discover_models, test_connection as probe_connection
from paperloom.models import LLMConfig
from paperloom.llm import LLMError
from paperloom.server import create_app


def transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr('paperloom.discovery.httpx.Client', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


def test_discovery_real_ids_dedup_and_non_chat(monkeypatch):
    def handler(request):
        assert request.url.path == '/v1/models'
        assert request.headers['authorization'] == 'Bearer secret-key'
        return httpx.Response(200, json={'data': [{'id': 'mimo-v2.6-flash'}, {'id': 'mimo-v2.6-pro'},
            {'id': 'mimo-v2.5-tts'}, {'id': 'mimo-v2.5-asr'}, {'id': 'mimo-v2.6-flash'}, {'id': '\ninvalid'}, {'bad': True}]})
    transport(monkeypatch, handler)
    result = discover_models(LLMConfig(base_url='https://token-plan-cn.xiaomimimo.com/v1', api_key='secret-key'))
    assert len(result['models']) == 4
    assert result['suggested_model'] == 'mimo-v2.6-flash'
    assert sum(m['text_candidate'] for m in result['models']) == 2
    assert result['recommended_settings']['token_parameter'] == 'max_completion_tokens'
    assert all(not m['verified'] for m in result['models'])
    assert 'secret-key' not in json.dumps(result)


@pytest.mark.parametrize('status', [401, 404, 302])
def test_discovery_safe_errors_no_redirect_or_echo(monkeypatch, status):
    seen = []
    def handler(request):
        seen.append(request.url)
        return httpx.Response(status, headers={'Location': 'https://unexpected.example/models'}, json={'error': 'secret-provider-body'})
    transport(monkeypatch, handler)
    with pytest.raises(LLMError) as error: discover_models(LLMConfig(base_url='https://example.test/v1'))
    assert len(seen) == 1
    assert 'secret-provider-body' not in str(error.value)


def test_connection_selected_models_and_parameters(monkeypatch):
    seen = []
    def handler(request):
        body = json.loads(request.content); seen.append(body['model'])
        assert body['max_completion_tokens'] == 1024
        assert body['thinking'] == {'type': 'disabled'}
        assert body['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]})
    transport(monkeypatch, handler)
    cfg = LLMConfig(base_url='https://example.test/v1', layout_model='a', translation_model='a', token_parameter='max_completion_tokens', thinking_mode='disabled')
    assert len(probe_connection(cfg)['models']) == 1 and seen == ['a']


def test_model_routes_are_guarded_and_do_not_persist(tmp_path, monkeypatch):
    transport(monkeypatch, lambda r: httpx.Response(200, json={'data': [{'id': 'text-model'}]}))
    with TestClient(create_app(tmp_path / 'jobs', 'a' * 32)) as client:
        cfg = {'base_url': 'https://example.test/v1', 'api_key': 'NEVER_STORE_THIS'}
        assert client.post('/api/models/discover', json=cfg).status_code == 401
        response = client.post('/api/models/discover', json=cfg, headers={'Authorization': 'Bearer ' + 'a' * 32})
        assert response.status_code == 200 and response.json()['models'][0]['id'] == 'text-model'
        assert 'NEVER_STORE_THIS' not in response.text
        assert not list((tmp_path / 'jobs').rglob('*.json'))
