"""Minimal OpenAI-compatible transport, bounded retries, JSON repair and cache."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from pathlib import Path
from typing import Callable

import httpx
from .models import LLMConfig, LLMEndpoint

PROMPTS = Path(__file__).parent / "prompts"


class Cancelled(Exception):
    pass


class LLMError(Exception):
    pass


class LLMResponseError(LLMError):
    """A successful transport response needs bounded semantic/JSON repair."""
    pass


@dataclass
class Target:
    endpoint: LLMEndpoint
    model: str
    tier: int
    secret: str = field(default="",repr=False)

    @property
    def identity(self):
        return self.endpoint.base_url, self.model

    def audit_identity(self):
        return {'model':self.model,'provider':urlsplit(self.endpoint.base_url).hostname,'tier':self.tier}


def check_cancel(event: threading.Event):
    if event.is_set():
        raise Cancelled("任务已取消")


def parse_json(content: str) -> dict:
    if not isinstance(content,str):raise LLMResponseError('模型没有返回文本 JSON')
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMResponseError("模型没有返回完整 JSON，请检查输出 token 上限或模型兼容性") from exc
    if not isinstance(result, dict):
        raise LLMResponseError("模型必须返回 JSON 对象")
    return result


class LLMClient:
    def __init__(self, config: LLMConfig, cache_dir: Path, cancel: threading.Event, audit: Callable):
        self.config = config
        self.cache = cache_dir
        self.cancel = cancel
        self.audit = audit
        self.cache.mkdir(parents=True, exist_ok=True)
        self.local = threading.local()

    def targets(self, stage):
        translation = stage in ('translate','translation_verify')
        primary = self.config.translation_model if translation else self.config.layout_model
        secret = self.config.api_key.get_secret_value() or os.getenv('PAPERLOOM_LLM_API_KEY','')
        result = [Target(self.config,primary,0,secret)] if primary else []
        for tier,provider in enumerate(self.config.fallbacks,1):
            key = secret if provider.inherit_primary_key else provider.api_key.get_secret_value()
            result.extend(Target(provider,model,tier,key) for model in provider.models)
        # Existing two-model configurations automatically get a distinct checker and
        # an alternate text model without copying credentials to another endpoint.
        other = self.config.layout_model if translation else self.config.translation_model
        if other and other!=primary:
            endpoint = LLMEndpoint.model_validate(self.config.model_dump(include=set(LLMEndpoint.model_fields)))
            endpoint.vision = False
            result.append(Target(endpoint,other,len(self.config.fallbacks)+1,secret))
        unique = {};[unique.setdefault(t.identity,t) for t in result]
        return list(unique.values())

    def reviewer_targets(self, stage, producer_model=None):
        chain=self.targets(stage)
        active=getattr(self.local,'used',{}).get(stage,chain[0].identity if chain else None)
        if producer_model:active=next((t.identity for t in chain if t.model==producer_model),('',producer_model))
        position=next((i for i,t in enumerate(chain) if t.identity==active),-1)
        ordered=chain[position+1:]+chain[:position+1]
        result=[t for t in ordered if t.identity!=active and t.model!=active[1]]
        if not result:raise LLMError('自动校验需要不同的模型；请配置备用 provider／模型，或设置不同的布局与翻译模型')
        return result

    def remember(self, stage, target):
        if not hasattr(self.local,'used'):self.local.used={}
        self.local.used[stage]=target.identity

    def complete(self, stage: str, payload: dict, images: list[Path] | None = None, repair: str = "", *, validator=None, target=None, attempts=None) -> dict:
        check_cancel(self.cancel)
        system = (PROMPTS / f"{stage}.md").read_text(encoding="utf-8")
        chain=[target] if isinstance(target,Target) else target if isinstance(target,list) else self.targets(stage)
        if not chain:raise LLMError('请填写布局模型和翻译模型名称')
        error=None
        for candidate in chain:
            if stage=='ocr' and not candidate.endpoint.vision:continue
            try:return self._complete(stage,payload,images,system,repair,candidate,validator,attempts)
            except LLMError as exc:
                error=exc
                self.audit({'stage':'llm_fallback','operation':stage,**candidate.audit_identity(),'reason':str(exc)[:250]})
        raise error or LLMError('没有可用且支持本任务的模型')

    def _complete(self,stage,payload,images,base_system,repair,target,validator,attempts):
        config=target.endpoint;feedback=repair;last='模型调用失败'
        image_data=[p.read_bytes() for p in images or []] if config.vision else []
        count=attempts or self.config.retry_attempts
        with httpx.Client(timeout=config.timeout,follow_redirects=False) as transport:
            for attempt in range(count):
                check_cancel(self.cancel)
                system=base_system+('\nPrevious output failed validation. Correct these errors: '+feedback[:2000] if feedback else '')
                key, cached, request = self._request(stage,payload,image_data,system,target)
                result=None
                if cached.exists():
                    try:result=json.loads(cached.read_text(encoding='utf-8'))
                    except (OSError,ValueError):pass
                if result is not None:
                    try:
                        if validator:validator(result)
                        self.remember(stage,target);return result
                    except (ValueError,KeyError,TypeError,LLMResponseError):
                        feedback='Cached output did not pass the required schema or coverage validation.'
                        result=None
                        key,cached,request=self._request(stage,payload,image_data,base_system+'\nPrevious output failed validation. Correct these errors: '+feedback,target)
                headers={'Content-Type':'application/json'}
                if target.secret:headers['Authorization']='Bearer '+target.secret
                try:
                    response=transport.post(config.base_url+'/chat/completions',headers=headers,json=request)
                except httpx.TransportError:
                    last='无法连接 LLM 服务或请求超时'
                    self.audit({'stage':'llm_retry','operation':stage,**target.audit_identity(),'attempt':attempt+1,'reason':'transport'})
                    if attempt<count-1 and self.cancel.wait(min(8,2**attempt)):check_cancel(self.cancel)
                    continue
                check_cancel(self.cancel)
                if response.status_code!=200:
                    hints={400:'请求字段不兼容',401:'API Key 无效',403:'模型访问被拒绝',404:'Base URL 或模型名称有误',429:'限流或余额不足'}
                    last=f'LLM HTTP {response.status_code}：'+hints.get(response.status_code,'服务调用失败')
                    self.audit({'stage':'llm_retry','operation':stage,**target.audit_identity(),'attempt':attempt+1,'status':response.status_code})
                    if response.status_code not in (408,429) and response.status_code<500:break
                    if attempt<count-1:
                        delay=min(8,2**attempt)
                        try:delay=min(10,max(delay,float(response.headers.get('Retry-After',0))))
                        except ValueError:pass
                        if self.cancel.wait(delay):check_cancel(self.cancel)
                    continue
                try:
                    body=response.json();choice=body['choices'][0]
                    if choice.get('finish_reason')=='length':raise LLMResponseError('模型输出被 token 上限截断')
                    result=parse_json(choice['message']['content'])
                    if validator:validator(result)
                except (ValueError,KeyError,IndexError,TypeError,LLMResponseError) as exc:
                    last='模型输出未通过 JSON、结构或来源覆盖校验'
                    if hasattr(exc,'errors'):
                        errors=exc.errors(include_input=False,include_url=False)
                        feedback='Schema errors: '+json.dumps([{'path':x['loc'],'type':x['type']} for x in errors[:12]],ensure_ascii=False)
                    elif isinstance(exc,(ValueError,LLMResponseError)):feedback=str(exc)[:1800]
                    else:feedback='Return the exact required JSON schema and source IDs; no extra fields or missing IDs.'
                    for candidate in self.targets(stage):
                        if candidate.secret:feedback=feedback.replace(candidate.secret,'[redacted]')
                    self.audit({'stage':'llm_retry','operation':stage,**target.audit_identity(),'attempt':attempt+1,'reason':'output_validation','validation':feedback})
                    continue
                temporary=cached.with_suffix('.'+uuid.uuid4().hex+'.tmp')
                temporary.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');temporary.replace(cached)
                raw_usage=body.get('usage',{});raw_usage=raw_usage if isinstance(raw_usage,dict) else {}
                usage={k:v for k,v in raw_usage.items() if k in ('prompt_tokens','completion_tokens','total_tokens') and isinstance(v,(int,float))}
                self.audit({'stage':stage,**target.audit_identity(),'input_hash':key,'prompt_hash':hashlib.sha256(system.encode()).hexdigest(),'usage':usage})
                self.remember(stage,target);return result
        raise LLMError(last)

    def _request(self,stage,payload,image_data,system,target):
        config=target.endpoint;model=target.model
        key_data = json.dumps({"system": system, "payload": payload, "endpoint": config.base_url,
                               "model": model, "json": config.json_mode, "temperature": config.temperature,
                               "limit": config.max_output_tokens, "token_parameter": config.token_parameter,
                               "thinking_mode": config.thinking_mode,
                               "images": [hashlib.sha256(v).hexdigest() for v in image_data]}, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(key_data.encode()).hexdigest()
        cached = self.cache / f"{key}.json"
        content: str | list = json.dumps(payload,ensure_ascii=False)
        if image_data:
            content = [{"type": "text", "text": content}]
            content += [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(v).decode()}} for v in image_data]
        request = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                   "temperature": config.temperature, config.token_parameter: config.max_output_tokens}
        if config.json_mode:
            request["response_format"] = {"type": "json_object"}
        if config.thinking_mode != "auto":request["thinking"]={"type":config.thinking_mode}
        return key,cached,request
