"""Private account-scoped ASR configuration. Secrets are write-only in the API."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.config import Settings
from app.errors import WorkflowError

PROVIDERS = ('siliconflow', 'siliconflow_qwen', 'cloudflare', 'groq', 'faster_whisper')
_LOCK = threading.RLock()


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    api_key: SecretStr | None = None
    api_token: SecretStr | None = None
    account_id: str | None = Field(default=None, pattern=r'^(?:[a-f0-9]{32})?$')
    free_tier_confirmed: bool | None = None
    paid_authorized: bool | None = None
    fallback_authorized: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=5, le=600)
    make_default: bool = False


def _scope(settings: Settings, owner: str | None) -> str:
    if owner and owner.startswith('account:'):
        return owner  # Set by signed-session middleware, never accepted from a header.
    if settings.auth_password_hash:
        return 'admin:' + settings.auth_username
    if not owner:
        raise ValueError('客户端身份尚未确认，请刷新页面。')
    return 'client:' + owner


def _path(settings: Settings, owner: str | None) -> Path:
    scope = _scope(settings, owner)
    return settings.db_path.parent / 'asr-settings' / (hashlib.sha256(scope.encode()).hexdigest()+'.json')


def _read(settings: Settings, owner: str | None) -> dict:
    path = _path(settings, owner)
    if not path.exists(): return {}
    if path.is_symlink(): raise WorkflowError('ASR 配置路径无效。')
    try:
        if path.stat().st_size > 32768: raise ValueError()
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict): raise ValueError()
        return data
    except (OSError, ValueError):
        raise WorkflowError('无法读取本账号的 ASR 配置，请检查文件权限或重新保存配置。') from None


def _write(settings: Settings, owner: str | None, data: dict):
    path = _path(settings, owner)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink(): raise WorkflowError('ASR 配置路径无效。')
    temporary = path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _apply(settings: Settings, profile: dict, owner: str | None = None) -> Settings:
    values = profile.get('providers', {})
    updates = {'asr_free_only': True} if profile else {}
    if owner and owner.startswith('account:'):
        updates.update(qwen_asr_api_key=None, qwen_asr_authorized=False, qwen_fallback_authorized=False)
    sf, cf, groq = [values.get(p, {}) for p in ('siliconflow', 'cloudflare', 'groq')]
    if sf.get('api_key'): updates['siliconflow_api_key'] = sf['api_key']
    qwen = values.get('siliconflow_qwen', {})
    if qwen.get('api_key'): updates['qwen_asr_api_key'] = qwen['api_key']
    if 'paid_authorized' in qwen: updates['qwen_asr_authorized'] = qwen['paid_authorized']
    if 'fallback_authorized' in qwen: updates['qwen_fallback_authorized'] = qwen['fallback_authorized']
    if cf.get('api_token'): updates['cloudflare_api_token'] = cf['api_token']
    if cf.get('account_id'): updates['cloudflare_account_id'] = cf['account_id']
    if groq.get('api_key'):
        updates.update(groq_api_key=groq['api_key'], groq_proxy_url=None,
                       groq_free_tier_confirmed=groq.get('free_tier_confirmed', False))
    if profile.get('timeout_seconds'): updates['asr_timeout_seconds'] = profile['timeout_seconds']
    if profile.get('default') in PROVIDERS: updates['asr_provider'] = profile['default']
    return replace(settings, **updates)


def effective_settings(settings: Settings, owner: str | None) -> Settings:
    # Legacy unowned/local jobs continue using server configuration.
    if owner is None and not settings.auth_password_hash: return settings
    return _apply(settings, _read(settings, owner), owner)


def public_settings(settings: Settings, owner: str | None) -> dict:
    from app.adapters.multi_asr import provider_catalog
    profile = _read(settings, owner)
    effective = _apply(settings, profile, owner)
    owned = profile.get('providers', {})
    fields = {}
    for p in PROVIDERS:
        value = owned.get(p, {})
        fields[p] = {'credential_saved': bool(value.get('api_key') or value.get('api_token')),
                     'source': 'account' if value.get('api_key') or value.get('api_token') else 'server',
                     'account_id': value.get('account_id', ''),
                     'free_tier_confirmed': value.get('free_tier_confirmed', False)}
        if p == 'siliconflow_qwen':
            fields[p].update(paid_authorized=effective.qwen_asr_authorized, fallback_authorized=effective.qwen_fallback_authorized)
    return {'default': effective.asr_provider, 'providers': provider_catalog(effective),
            'timeout_seconds': effective.asr_timeout_seconds, 'fields': fields}


def save_settings(settings: Settings, owner: str | None, provider: str, payload: SettingsUpdate) -> dict:
    if provider not in PROVIDERS: raise ValueError('不支持的转写服务。')
    with _LOCK:
        profile = _read(settings, owner)
        value = profile.setdefault('providers', {}).setdefault(provider, {})
        for name, allowed in [('api_key', provider in ('siliconflow','siliconflow_qwen','groq')), ('api_token', provider=='cloudflare')]:
            secret = getattr(payload, name)
            if secret and secret.get_secret_value().strip():
                raw = secret.get_secret_value().strip()
                if not allowed or not 4 <= len(raw) <= 4096 or any(not 33 <= ord(c) <= 126 for c in raw):
                    raise ValueError('凭据格式无效，请检查对应服务商字段。')
                value[name] = raw
        if provider=='cloudflare' and payload.account_id:
            value['account_id'] = payload.account_id
        if provider=='groq' and payload.free_tier_confirmed is not None:
            value['free_tier_confirmed'] = payload.free_tier_confirmed
        if provider=='siliconflow_qwen':
            for name in ('paid_authorized','fallback_authorized'):
                if getattr(payload,name) is not None:value[name]=getattr(payload,name)
        if payload.timeout_seconds is not None: profile['timeout_seconds'] = payload.timeout_seconds
        if provider=='siliconflow_qwen' and payload.paid_authorized is False:
            if payload.make_default or profile.get('default')==provider:profile['default']='faster_whisper'
        elif payload.make_default:
            from app.adapters.multi_asr import provider_catalog
            if not next(p for p in provider_catalog(_apply(settings, profile, owner)) if p['id']==provider)['configured']:
                raise ValueError('该服务尚未配置完整或未确认免费计划，不能设为默认。')
            profile['default'] = provider
        _write(settings, owner, profile)
    return public_settings(settings, owner)


def clear_settings(settings: Settings, owner: str | None, provider: str) -> dict:
    if provider not in PROVIDERS: raise ValueError('不支持的转写服务。')
    with _LOCK:
        profile = _read(settings, owner)
        profile.setdefault('providers', {}).pop(provider, None)
        if profile.get('default') == provider: profile.pop('default', None)
        _write(settings, owner, profile)
    return public_settings(settings, owner)


def check_settings(settings: Settings, owner: str | None, provider: str) -> dict:
    from app.adapters.multi_asr import ProviderError, UnifiedTranscriber
    if provider not in PROVIDERS: raise ValueError('不支持的转写服务。')
    cfg = replace(effective_settings(settings, owner), asr_provider=provider,
                  asr_require_timestamps=provider!='siliconflow', asr_timeout_seconds=15)
    try:
        if provider=='groq':
            if not cfg.groq_api_key: raise ProviderError('configuration','未配置 Groq API Key')
            if cfg.asr_free_only and not cfg.groq_free_tier_confirmed:
                raise ProviderError('cost_policy','需要确认 Groq 为免费账号；系统不能自动核验其套餐')
            with httpx.Client(proxy=cfg.groq_proxy_url,trust_env=False,timeout=15) as client:
                try: response = client.get('https://api.groq.com/openai/v1/models',headers={'Authorization':'Bearer '+cfg.groq_api_key})
                except httpx.TimeoutException: raise ProviderError('timeout','Groq 连接超时') from None
                except httpx.RequestError: raise ProviderError('network','Groq 网络连接失败') from None
                if response.status_code!=200: raise ProviderError('access',f'Groq HTTP {response.status_code}')
        elif provider=='siliconflow_qwen':
            from app.adapters.paid_asr import PaidQwenTranscriber,QWEN_MODEL
            adapter=PaidQwenTranscriber(cfg);adapter.check_config()
            with adapter._client() as client:
                response=adapter._request(client,'GET','https://api.siliconflow.cn/v1/models',headers={'Authorization':'Bearer '+cfg.qwen_asr_api_key})
                try:available=any(item.get('id')==QWEN_MODEL for item in response.json()['data'])
                except (ValueError,TypeError,KeyError):available=False
                if not available:raise ProviderError('configuration','本账号未列出 Qwen ASR 模型')
        else:
            transcriber = UnifiedTranscriber(cfg)
            with transcriber._client() as client:
                transcriber._preflight(client)
                if provider=='siliconflow':
                    transcriber._request(client,'GET','https://api.siliconflow.cn/v1/models',
                                         headers={'Authorization':'Bearer '+cfg.siliconflow_api_key})
        return {'ok':True,'audio_uploaded':False,'message':'凭据/配置检查通过，未上传音频；真实识别请通过新建任务验证。'}
    except ProviderError as error:
        return {'ok':False,'audio_uploaded':False,'category':error.category,'message':str(error)}


def task_settings(settings: Settings, request) -> Settings:
    cfg=effective_settings(settings,request.client_id)
    if request.asr_provider:
        cfg=replace(cfg,asr_provider=request.asr_provider,asr_require_timestamps=request.asr_require_timestamps)
    if cfg.asr_provider=='siliconflow_qwen':
        def live_authorization():
            current=effective_settings(settings,request.client_id)
            return (current.qwen_asr_authorized and request.asr_paid_authorized is True,
                    current.qwen_fallback_authorized and request.asr_fallback_authorized is True)
        cfg=replace(cfg,qwen_asr_authorized=cfg.qwen_asr_authorized and request.asr_paid_authorized is True,
                    qwen_fallback_authorized=cfg.qwen_fallback_authorized and request.asr_fallback_authorized is True,
                    asr_authorization_check=live_authorization)
    return cfg
