"""Session-only model discovery and small, explicit protocol checks."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit
import httpx

from .llm import LLMError, parse_json
from .models import LLMConfig


def headers(config: LLMConfig) -> dict:
    secret = config.api_key.get_secret_value() or os.getenv('PAPERLOOM_LLM_API_KEY', '')
    return {'Authorization': 'Bearer ' + secret} if secret else {}


def safe_error(status: int, listing: bool = False) -> LLMError:
    hints = {400: '请求参数不兼容，请检查 JSON 模式、思考模式及 token 参数',
             401: 'API Key 无效或已过期', 403: '没有此模型的访问权限',
             404: '服务没有提供 /models，请手动填写模型名称' if listing else '模型名称或接口路径不存在',
             405: '服务没有提供 /models，请手动填写模型名称', 429: '服务限流或额度不足'}
    return LLMError(f'LLM HTTP {status}：' + hints.get(status, '服务调用失败'))


def discover_models(config: LLMConfig) -> dict:
    # The provider, not a hardcoded catalogue, defines the available model IDs.
    try:
        with httpx.Client(timeout=min(config.timeout, 30), follow_redirects=False) as client:
            response = client.get(config.base_url + '/models', headers=headers(config))
    except httpx.TransportError as exc:
        raise LLMError('模型检测无法连接服务，请检查 Base URL 和网络') from exc
    if response.status_code != 200:
        raise safe_error(response.status_code, listing=True)
    try:
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError('large response')
        body = response.json()
        entries = body.get('data')
        if not isinstance(entries, list) or len(entries) > 5000:
            raise ValueError('invalid model list')
    except (ValueError, AttributeError) as exc:
        raise LLMError('服务返回的模型列表不符合 OpenAI 兼容格式') from exc
    models = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mid = entry.get('id')
        if not isinstance(mid, str) or not mid.strip() or len(mid) > 200 or any(ord(c) < 32 for c in mid):
            continue
        # Known non-text names are excluded from suggestions, but still displayed.
        non_chat = bool(re.search(r'(?:^|[-/_.])(tts|asr|whisper|embedding|embed|rerank|dall-e|image-generation)(?:$|[-/_.])', mid, re.I))
        owner = entry.get('owned_by', '')
        models[mid] = {'id': mid, 'owned_by': owner[:100] if isinstance(owner, str) else '',
                       'text_candidate': not non_chat, 'verified': False}
    ordered = sorted(models.values(), key=lambda m: (not m['text_candidate'], m['id'].casefold()))
    candidates = [m['id'] for m in ordered if m['text_candidate']]
    # Prefer a general chat model to specialized legacy completion names.
    preferred = next((m for m in candidates if re.search(r'chat|instruct|flash', m, re.I)), None)
    recommended = {}
    if (urlsplit(config.base_url).hostname or '').endswith('.xiaomimimo.com'):
        recommended = {'token_parameter': 'max_completion_tokens', 'thinking_mode': 'disabled'}
    return {'models': ordered, 'suggested_model': preferred or (candidates[0] if candidates else ''),
            'recommended_settings': recommended,
            'message': f'发现 {len(ordered)} 个模型，{len(candidates)} 个文本候选。列表不代表已验证聊天或视觉能力。'}


def test_connection(config: LLMConfig) -> dict:
    models = list(dict.fromkeys([config.layout_model, config.translation_model]))
    if not models or any(not m for m in models):
        raise LLMError('请先选择布局模型和翻译模型')
    results = []
    with httpx.Client(timeout=config.timeout, follow_redirects=False) as client:
        for model in models:
            request = {'model': model, 'messages': [{'role': 'system', 'content': 'This is an API compatibility test. Return JSON only.'},
                       {'role': 'user', 'content': 'Return exactly {"ok":true}.'}], config.token_parameter: 1024}
            if config.json_mode:
                request['response_format'] = {'type': 'json_object'}
            if config.thinking_mode != 'auto':
                request['thinking'] = {'type': config.thinking_mode}
            try:
                response = client.post(config.base_url + '/chat/completions', json=request, headers=headers(config))
            except httpx.TransportError as exc:
                raise LLMError('连接测试超时或网络不可用') from exc
            if response.status_code != 200:
                raise safe_error(response.status_code)
            try:
                body = response.json()
                choice = body['choices'][0]
                parsed = parse_json(choice['message']['content'])
                if choice.get('finish_reason') == 'length' or parsed.get('ok') is not True:
                    raise ValueError('invalid compatibility output')
            except (KeyError, IndexError, ValueError, TypeError, LLMError) as exc:
                raise LLMError('模型未完成 JSON 连接测试，可调整思考模式或 JSON 模式') from exc
            results.append({'id': model, 'chat_verified': True, 'json_verified': True})
    return {'models': results, 'message': f'{len(results)} 个所选模型通过聊天和 JSON 测试；视觉能力需单独核验。'}
