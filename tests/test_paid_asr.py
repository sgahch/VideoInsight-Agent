from dataclasses import replace
from types import SimpleNamespace
import io
import wave
import httpx
import pytest
from app.config import Settings
from app.schemas import TranscriptSegment, AnalyzeRequest
from app.adapters.multi_asr import ProviderError, pin_provider

def setup_engine(tmp_path,monkeypatch,handler,**changes):
    from app.adapters.paid_asr import PaidQwenTranscriber
    source=tmp_path/'speech.wav'
    with wave.open(str(source),'wb') as out:
        out.setparams((1,2,16000,0,'NONE',''));out.writeframes(b'\x01\x00'*32000)
    model=tmp_path/'model';model.mkdir();(model/'model.bin').touch()
    values=dict(asr_provider='siliconflow_qwen',qwen_asr_api_key='private-paid-key',
        qwen_asr_authorized=True,qwen_fallback_authorized=True,faster_whisper_model_path=str(model))
    values.update(changes);cfg=replace(Settings(),**values)
    engine=PaidQwenTranscriber(cfg)
    def offline(path,task_id,reporter=None):
        engine.local.last_duration=2
        return [TranscriptSegment(start=0,end=1,text='离线一'),TranscriptSegment(start=1,end=2,text='离线二')]
    monkeypatch.setattr(engine.local,'transcribe',offline)
    engine._client=lambda:httpx.Client(transport=httpx.MockTransport(handler))
    return source,engine

def test_paid_authorization_is_required_before_any_processing(tmp_path,monkeypatch):
    source,e=setup_engine(tmp_path,monkeypatch,lambda r:pytest.fail('unauthorized upload'),qwen_asr_authorized=False)
    e.local.transcribe=lambda *a,**k:pytest.fail('unauthorized processing')
    with pytest.raises(ProviderError) as err:e.transcribe(source,'job')
    assert err.value.category=='cost_policy'

def test_paid_text_uses_actual_audio_interval_and_preserves_timeline(tmp_path,monkeypatch):
    requests=[]
    def handle(r):
        requests.append(r)
        assert b'Qwen/Qwen3-ASR-1.7B' in r.content
        assert r.headers['authorization']=='Bearer private-paid-key'
        wav=r.content[r.content.index(b'RIFF'):]; wav=wav[:32044]
        with wave.open(io.BytesIO(wav),'rb') as stream:assert stream.getnframes()==16000
        return httpx.Response(200,json={'text':'付费'+str(len(requests))})
    source,e=setup_engine(tmp_path,monkeypatch,handle)
    result=e.transcribe(source,'job')
    assert [(s.start,s.end,s.text) for s in result]==[(0,1,'付费1'),(1,2,'付费2')]
    assert e.diagnostics['asr_paid_segments']==2 and e.diagnostics['asr_fallback_segments']==0
    assert e.timestamps and e.diagnostics['asr_timestamp_source']=='faster_whisper_segments'

@pytest.mark.parametrize('status',[401,402,429,503])
def test_first_cloud_failure_stops_paid_calls_and_keeps_free_segments(tmp_path,monkeypatch,status):
    calls=[]
    def handle(r):calls.append(r);return httpx.Response(status,json={'message':'private-paid-key'})
    source,e=setup_engine(tmp_path,monkeypatch,handle)
    assert [s.text for s in e.transcribe(source,'job')]==['离线一','离线二']
    assert len(calls)==1 and e.diagnostics['asr_fallback_segments']==2
    assert e.diagnostics['asr_provider']=='faster_whisper'
    assert 'private-paid-key' not in str(e.diagnostics)

def test_partial_success_is_kept_and_failed_segment_uses_free_fallback(tmp_path,monkeypatch):
    calls=[]
    def handle(r):
        calls.append(r)
        if len(calls)==1:return httpx.Response(200,json={'text':'付费首段'})
        raise httpx.ReadTimeout('secret-key',request=r)
    source,e=setup_engine(tmp_path,monkeypatch,handle)
    result=e.transcribe(source,'job')
    assert [s.text for s in result]==['付费首段','离线二']
    assert e.diagnostics['asr_paid_segments']==1 and e.diagnostics['asr_fallback_reason']=='timeout'

def test_no_fallback_without_pinned_consent(tmp_path,monkeypatch):
    source,e=setup_engine(tmp_path,monkeypatch,lambda r:httpx.Response(503),qwen_fallback_authorized=False)
    with pytest.raises(ProviderError):e.transcribe(source,'job')

def test_qwen_consent_is_pinned_and_explicit_denial_survives_retry():
    cfg=replace(Settings(),auth_password_hash=None,qwen_asr_authorized=True,qwen_fallback_authorized=True)
    req=AnalyzeRequest(url='upload://a.wav',asr_provider='siliconflow_qwen')
    pinned=pin_provider(req,cfg)
    assert pinned.asr_paid_authorized and pinned.asr_fallback_authorized
    denied=req.model_copy(update={'asr_paid_authorized':False,'asr_fallback_authorized':False})
    retried=pin_provider(denied,cfg)
    assert retried.asr_paid_authorized is False and retried.asr_fallback_authorized is False


def test_revocation_after_first_interval_stops_further_paid_uploads(tmp_path,monkeypatch):
    consent=[True,True];calls=[]
    def handle(r):
        calls.append(r);consent[0]=False
        return httpx.Response(200,json={'text':'付费一'})
    source,e=setup_engine(tmp_path,monkeypatch,handle,asr_authorization_check=lambda:tuple(consent))
    assert [s.text for s in e.transcribe(source,'job')]==['付费一','离线二']
    assert len(calls)==1 and e.diagnostics['asr_fallback_reason']=='cost_policy'


def test_fallback_revocation_is_checked_after_upstream_failure(tmp_path,monkeypatch):
    consent=[True,True]
    def handle(r):consent[1]=False;return httpx.Response(503)
    source,e=setup_engine(tmp_path,monkeypatch,handle,asr_authorization_check=lambda:tuple(consent))
    with pytest.raises(ProviderError):e.transcribe(source,'job')


def test_invalid_later_timestamp_is_rejected_before_any_paid_upload(tmp_path,monkeypatch):
    source,e=setup_engine(tmp_path,monkeypatch,lambda r:pytest.fail('spent before timeline validation'))
    def invalid(*a,**kw):
        e.local.last_duration=2
        return [TranscriptSegment(start=0,end=1,text='正常'),TranscriptSegment(start=1,end=1,text='空段')]
    e.local.transcribe=invalid
    with pytest.raises(ProviderError) as err:e.transcribe(source,'job')
    assert err.value.category=='response'


def test_second_conversion_is_bounded_by_remaining_budget(tmp_path,monkeypatch):
    source,e=setup_engine(tmp_path,monkeypatch,lambda r:httpx.Response(200,json={'text':'正常'}),asr_task_timeout_seconds=20)
    observed=[]
    def convert(src,target,*,timeout_seconds):
        observed.append(timeout_seconds);target.write_bytes(src.read_bytes());return target
    e.preprocessor=SimpleNamespace(convert_to_wav16k=convert)
    e.transcribe(source,'job')
    assert observed and 0<observed[0]<=20
