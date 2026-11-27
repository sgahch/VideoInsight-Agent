from dataclasses import replace
from pathlib import Path
import wave
import httpx
import pytest
from app.config import Settings
from app.adapters.multi_asr import ProviderError, UnifiedTranscriber, provider_catalog

def audio(tmp_path):
    p=tmp_path/'speech.wav'
    with wave.open(str(p),'wb') as w:w.setparams((1,2,16000,0,'NONE',''));w.writeframes(b'\0\0'*16000)
    return p

class Preprocessor:
    def convert_to_wav16k(self,source,target):target.write_bytes(source.read_bytes());return target

def engine(tmp_path,provider,handler,**kwargs):
    cfg=replace(Settings(),asr_provider=provider,siliconflow_api_key='private-sf-key',cloudflare_api_token='private-cf-key',cloudflare_account_id='a'*32,**kwargs)
    result=UnifiedTranscriber(cfg,Preprocessor())
    result._client=lambda:httpx.Client(transport=httpx.MockTransport(handler))
    return result

def test_paid_qwen_model_is_blocked_before_network_or_upload(tmp_path):
    e=engine(tmp_path,'siliconflow',lambda r:pytest.fail('paid request attempted'),siliconflow_asr_model='Qwen/Qwen3-ASR-1.7B',asr_require_timestamps=False)
    with pytest.raises(ProviderError,match='免费') as error:e.transcribe(audio(tmp_path),'job')
    assert error.value.category=='cost_policy'

def test_text_only_provider_rejects_timestamps_before_upload(tmp_path):
    e=engine(tmp_path,'siliconflow',lambda r:pytest.fail('capability failure uploaded audio'))
    with pytest.raises(ProviderError) as error:e.transcribe(audio(tmp_path),'job')
    assert error.value.category=='capability'

def test_sensevoice_contract_and_no_fake_timestamps(tmp_path):
    calls=[]
    def handle(r):
        calls.append(r)
        if r.method=='GET':return httpx.Response(200,text='FunAudioLLM/SenseVoiceSmall 输入 免费')
        assert b'FunAudioLLM/SenseVoiceSmall' in r.content and b'filename=' in r.content
        return httpx.Response(200,json={'text':'真实语音测试'})
    e=engine(tmp_path,'siliconflow',handle,asr_require_timestamps=False)
    assert e.transcribe(audio(tmp_path),'job')[0].text=='真实语音测试'
    assert not e.timestamps and len([r for r in calls if r.method=='POST'])==1

@pytest.mark.parametrize('status,category',[(401,'authentication'),(403,'access'),(429,'quota'),(503,'upstream')])
def test_cloud_failure_never_retries_audio_or_switches_provider(tmp_path,status,category):
    calls=[]
    def handle(r):
        calls.append(r)
        if r.method=='GET':return httpx.Response(200,text='FunAudioLLM/SenseVoiceSmall 免费')
        return httpx.Response(status,json={'error':{'message':'private-sf-key'}})
    e=engine(tmp_path,'siliconflow',handle,asr_require_timestamps=False)
    with pytest.raises(ProviderError) as error:e.transcribe(audio(tmp_path),'job')
    assert error.value.category==category and 'private-sf-key' not in str(error.value)
    assert len([r for r in calls if r.method=='POST'])==1

def test_cloudflare_paid_or_unknown_plan_cannot_upload(tmp_path):
    for plan in ['Workers Paid','Unrelated Free']:
        def handle(r):
            assert r.method=='GET'
            return httpx.Response(200,json={'success':True,'result':[{'rate_plan':{'name':plan}}]})
        e=engine(tmp_path,'cloudflare',handle)
        with pytest.raises(ProviderError) as error:e.transcribe(audio(tmp_path),'job')
        assert error.value.category=='cost_policy'

def test_cloudflare_verified_free_plan_parses_real_segment_schema(tmp_path):
    def handle(r):
        if r.method=='GET':return httpx.Response(200,json={'success':True,'result':[{'rate_plan':{'id':'workers_free','name':'Workers Free'}}]})
        assert '/ai/run/@cf/openai/whisper-large-v3-turbo' in str(r.url)
        return httpx.Response(200,json={'success':True,'result':{'text':'测试','segments':[{'start':0.1,'end':0.8,'text':'测试'}]}})
    segments=engine(tmp_path,'cloudflare',handle).transcribe(audio(tmp_path),'job')
    assert segments[0].start==0.1 and segments[0].end==0.8

def test_network_timeout_is_classified_and_not_repeated(tmp_path):
    def handle(r):raise httpx.ReadTimeout('secret',request=r)
    e=engine(tmp_path,'siliconflow',handle,asr_require_timestamps=False)
    with pytest.raises(ProviderError) as error:e.transcribe(audio(tmp_path),'job')
    assert error.value.category=='timeout' and 'secret' not in str(error.value)

def test_local_timeout_stops_process_without_cloud_fallback(tmp_path,monkeypatch):
    import subprocess
    model=tmp_path/'model';model.mkdir();(model/'model.bin').touch()
    e=engine(tmp_path,'faster_whisper',lambda r:pytest.fail('offline ASR made HTTP request'),faster_whisper_model_path=str(model))
    def stop(args,**kwargs):
        assert kwargs['env']['HF_HUB_OFFLINE']=='1' and kwargs['timeout']>0
        raise subprocess.TimeoutExpired(args,1)
    monkeypatch.setattr(subprocess,'run',stop)
    with pytest.raises(ProviderError) as error:e.transcribe(audio(tmp_path),'job')
    assert error.value.category=='timeout'

def test_strict_free_mode_blocks_legacy_paid_or_unverified_cloud():
    from app.adapters.asr import select_transcriber
    from app.errors import ExternalDependencyError
    for provider in ['paraformer','groq']:
        with pytest.raises(ExternalDependencyError,match='免费'):
            select_transcriber(replace(Settings(),asr_provider=provider,asr_free_only=True,groq_free_tier_confirmed=False),False)

def test_default_provider_is_pinned_before_later_config_changes():
    from app.adapters.multi_asr import pin_provider
    from app.schemas import AnalyzeRequest
    request=AnalyzeRequest(url='upload://speech.wav')
    first=pin_provider(request,replace(Settings(),asr_provider='faster_whisper'))
    assert first.asr_provider=='faster_whisper' and request.asr_provider is None
    assert pin_provider(first,replace(Settings(),asr_provider='siliconflow')).asr_provider=='faster_whisper'

def test_semaphore_wait_is_subtracted_from_process_timeout(tmp_path,monkeypatch):
    import app.adapters.multi_asr as module
    from types import SimpleNamespace
    times=iter([0.0,9.0])
    monkeypatch.setattr(module,'monotonic',lambda:next(times))
    monkeypatch.setattr(module,'_LOCAL_GATE',SimpleNamespace(acquire=lambda **kwargs:True,release=lambda:None))
    def run(*args,**kwargs):
        assert kwargs['timeout']==1.0
        return SimpleNamespace(returncode=0,stdout=b'{"segments":[]}')
    monkeypatch.setattr(module.subprocess,'run',run)
    e=engine(tmp_path,'faster_whisper',lambda r:pytest.fail('cloud request'))
    e._recognize(None,audio(tmp_path),10)
