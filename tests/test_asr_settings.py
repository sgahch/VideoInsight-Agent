from dataclasses import replace
from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient
import app.main as main
from app.config import Settings

@pytest.fixture
def api(tmp_path,monkeypatch):
    cfg=replace(Settings(),db_path=tmp_path/'tasks.db',auth_password_hash=None,
                siliconflow_api_key=None,cloudflare_api_token=None,cloudflare_account_id='',
                groq_api_key=None,groq_free_tier_confirmed=False,asr_provider='faster_whisper')
    monkeypatch.setattr(main,'settings',cfg)
    return TestClient(main.app),cfg

def test_save_mask_isolation_and_clear(api):
    client,cfg=api
    alice={'X-Client-Id':'alice'};bob={'X-Client-Id':'bob'}
    response=client.put('/asr/settings/siliconflow',json={'api_key':'private-alice-key','make_default':True},headers=alice)
    assert response.status_code==200,response.text
    assert 'private-alice-key' not in response.text
    assert response.json()['default']=='siliconflow'
    assert next(p for p in response.json()['providers'] if p['id']=='siliconflow')['configured']
    assert not next(p for p in client.get('/asr/settings',headers=bob).json()['providers'] if p['id']=='siliconflow')['configured']
    assert client.get('/config',headers=alice).json()['asr']['default']=='siliconflow'
    from app.asr_settings import effective_settings
    assert effective_settings(cfg,'alice').siliconflow_api_key=='private-alice-key'
    assert effective_settings(cfg,'bob').siliconflow_api_key is None
    assert client.delete('/asr/settings/siliconflow',headers=bob).status_code==200
    assert effective_settings(cfg,'alice').siliconflow_api_key=='private-alice-key'
    assert client.delete('/asr/settings/siliconflow',headers=alice).status_code==200
    assert effective_settings(cfg,'alice').siliconflow_api_key is None

def test_blank_key_keeps_saved_secret_and_validation_never_echoes_it(api):
    client,cfg=api;headers={'X-Client-Id':'alice'}
    client.put('/asr/settings/siliconflow',json={'api_key':'private-saved-key'},headers=headers).raise_for_status()
    client.put('/asr/settings/siliconflow',json={'api_key':'','timeout_seconds':45},headers=headers).raise_for_status()
    from app.asr_settings import effective_settings
    assert effective_settings(cfg,'alice').siliconflow_api_key=='private-saved-key'
    assert effective_settings(cfg,'alice').asr_timeout_seconds==45
    response=client.put('/asr/settings/siliconflow',json={'api_key':'private-invalid-key','timeout_seconds':-1},headers=headers)
    assert response.status_code==422 and 'private-invalid-key' not in response.text

def test_groq_confirmation_and_cloudflare_validation(api):
    client,cfg=api;headers={'X-Client-Id':'alice'}
    assert client.put('/asr/settings/cloudflare',json={'api_token':'token','account_id':'../../other'},headers=headers).status_code==422
    response=client.put('/asr/settings/groq',json={'api_key':'private-groq-key'},headers=headers)
    assert response.status_code==200
    assert not next(p for p in response.json()['providers'] if p['id']=='groq')['configured']
    response=client.put('/asr/settings/groq',json={'free_tier_confirmed':True},headers=headers)
    assert next(p for p in response.json()['providers'] if p['id']=='groq')['configured']
    assert client.put('/asr/settings/qwen',json={'api_key':'no-paid-model'},headers=headers).status_code==422

def test_connection_check_never_posts_audio(api,monkeypatch):
    import httpx
    from app.adapters.multi_asr import UnifiedTranscriber
    client,cfg=api;headers={'X-Client-Id':'alice'}
    client.put('/asr/settings/siliconflow',json={'api_key':'private-saved-key'},headers=headers).raise_for_status()
    requests=[]
    def handle(request):
        requests.append(request)
        assert request.method=='GET'
        if 'pricing' in str(request.url):return httpx.Response(200,text='FunAudioLLM/SenseVoiceSmall 免费')
        assert request.headers['authorization']=='Bearer private-saved-key'
        return httpx.Response(200,json={'data':[]})
    monkeypatch.setattr(UnifiedTranscriber,'_client',lambda self:httpx.Client(transport=httpx.MockTransport(handle)))
    result=client.post('/asr/settings/siliconflow/check',headers=headers).json()
    assert result['ok'] and result['audio_uploaded'] is False and len(requests)==2
    assert 'private-saved-key' not in json.dumps(result)

def test_profile_is_applied_when_task_provider_is_pinned(api):
    client,cfg=api
    client.put('/asr/settings/siliconflow',json={'api_key':'private-key','make_default':True},headers={'X-Client-Id':'alice'}).raise_for_status()
    from app.adapters.multi_asr import pin_provider
    from app.schemas import AnalyzeRequest
    task=pin_provider(AnalyzeRequest(url='upload://audio.wav',client_id='alice'),cfg)
    assert task.asr_provider=='siliconflow'
    assert pin_provider(task,replace(cfg,asr_provider='groq')).asr_provider=='siliconflow'


def test_paid_provider_requires_own_key_and_account_scoped_consent(api,monkeypatch,tmp_path):
    client,cfg=api;alice={'X-Client-Id':'alice'};bob={'X-Client-Id':'bob'}
    model=tmp_path/'base';model.mkdir();(model/'model.bin').touch()
    cfg=replace(cfg,faster_whisper_model_path=str(model))
    monkeypatch.setattr(main,'settings',cfg)
    monkeypatch.setattr('app.adapters.multi_asr.importlib.util.find_spec',lambda name:True)
    assert client.put('/asr/settings/siliconflow_qwen',json={'api_key':'private-qwen-key','make_default':True},headers=alice).status_code==422
    result=client.put('/asr/settings/siliconflow_qwen',json={'api_key':'private-qwen-key','paid_authorized':True,'fallback_authorized':True,'make_default':True},headers=alice)
    assert result.status_code==200,result.text
    assert 'private-qwen-key' not in result.text and result.json()['default']=='siliconflow_qwen'
    from app.asr_settings import task_settings,effective_settings
    from app.adapters.multi_asr import pin_provider
    from app.schemas import AnalyzeRequest
    task=pin_provider(AnalyzeRequest(url='upload://speech.wav',client_id='alice'),cfg)
    assert task.asr_paid_authorized and task.asr_fallback_authorized
    active=task_settings(cfg,task)
    assert active.qwen_asr_api_key=='private-qwen-key' and active.qwen_asr_authorized
    assert not effective_settings(cfg,'bob').qwen_asr_authorized
    assert not client.get('/asr/settings',headers=bob).json()['fields']['siliconflow_qwen']['credential_saved']
    revoked=client.put('/asr/settings/siliconflow_qwen',json={'paid_authorized':False,'make_default':True},headers=alice)
    assert revoked.status_code==200 and revoked.json()['default']=='faster_whisper'
    assert not task_settings(cfg,task).qwen_asr_authorized
    assert not task_settings(cfg,task.model_copy(update={'asr_paid_authorized':False})).qwen_asr_authorized
    client.delete('/asr/settings/siliconflow_qwen',headers=alice).raise_for_status()
    assert effective_settings(cfg,'alice').qwen_asr_api_key is None


def test_signed_account_cannot_inherit_shared_paid_credentials(tmp_path):
    from app.asr_settings import effective_settings,save_settings,SettingsUpdate
    cfg=replace(Settings(),db_path=tmp_path/'db',qwen_asr_api_key='server-only-key',qwen_asr_authorized=True,qwen_fallback_authorized=True)
    owner='account:'+'a'*32
    save_settings(cfg,owner,'siliconflow_qwen',SettingsUpdate(paid_authorized=True,fallback_authorized=True))
    assert effective_settings(cfg,owner).qwen_asr_api_key is None
