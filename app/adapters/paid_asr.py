"""Opt-in Qwen text on real offline speech intervals, with bounded free fallback."""
from dataclasses import replace
import math
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
import wave

from app.adapters.multi_asr import UnifiedTranscriber, ProviderError
from app.schemas import TranscriptSegment

QWEN_MODEL = 'Qwen/Qwen3-ASR-1.7B'


class PaidQwenTranscriber(UnifiedTranscriber):
    def __init__(self, settings, preprocessor=None):
        super().__init__(settings, preprocessor)
        self.local = UnifiedTranscriber(replace(settings, asr_provider='faster_whisper', asr_require_timestamps=True), preprocessor)
        self.diagnostics = {}

    def check_config(self):
        if not self.settings.qwen_asr_authorized:
            raise ProviderError('cost_policy', '付费 Qwen ASR 尚未获得本账号授权')
        if not self.settings.qwen_asr_api_key:
            raise ProviderError('configuration', '未配置本账号的 Qwen ASR Key')
        with self.local._client() as client:
            self.local._preflight(client)

    def _authorization(self):
        if self.settings.asr_authorization_check:
            return self.settings.asr_authorization_check()
        return self.settings.qwen_asr_authorized, self.settings.qwen_fallback_authorized

    def _result(self, segments, paid, reason):
        fallback=len(segments)-paid
        self.diagnostics={
            'asr_provider':'siliconflow_qwen' if paid else 'faster_whisper',
            'asr_model':QWEN_MODEL if paid else 'base-int8',
            'asr_requested_provider':'siliconflow_qwen','asr_timestamp_source':'faster_whisper_segments',
            'asr_paid_segments':paid,'asr_fallback_segments':fallback,
            'asr_fallback_reason':reason,'asr_fallback_provider':'faster_whisper' if fallback else None,
        }
        return segments

    def _convert(self, source, target, deadline):
        remaining=deadline-monotonic()
        if remaining<=0:raise ProviderError('timeout','音频转码总时限已到')
        return self.preprocessor.convert_to_wav16k(source,target,timeout_seconds=remaining)

    def transcribe(self, audio_path, task_id, reporter=None):
        self.check_config()  # Check consent and timestamp prerequisites before paid uploads.
        if audio_path is None: raise ProviderError('input', '缺少音频文件')
        deadline = monotonic() + max(1, self.settings.asr_task_timeout_seconds)
        if reporter: reporter.mark_transcribe_started(0, 1)
        self.local.preprocessor=SimpleNamespace(convert_to_wav16k=lambda source,target:self._convert(source,target,deadline))
        baseline = self.local.transcribe(audio_path, task_id + '-timeline')
        self.last_duration = self.local.last_duration
        if not baseline: raise ProviderError('empty', '未识别到可用的语音时间段')
        if reporter: reporter.mark_transcribe_started(self.last_duration, len(baseline))
        wav = Path(audio_path).parent / (task_id + '-qwen-16k.wav')
        clip = wav.with_name(task_id + '-qwen-clip.wav')
        output = []; paid = 0; reason = None
        try:
            if monotonic()>=deadline:
                if not self._authorization()[1]:raise ProviderError('timeout','付费转写总时限已到')
                return self._result(baseline,0,'timeout')
            self._convert(audio_path, wav, deadline)
            with wave.open(str(wav), 'rb') as source, self._client() as client:
                rate = source.getframerate(); frames = source.getnframes()
                for segment in baseline:
                    if not all(math.isfinite(v) for v in (segment.start, segment.end)) or not 0 <= segment.start < segment.end <= frames / rate + .1:
                        raise ProviderError('response', '离线时间段格式无效，未上传该音频')
                    if round(segment.start*rate)>=min(frames,round(segment.end*rate)):
                        raise ProviderError('response','音频片段为空，未上传该音频')
                for index, segment in enumerate(baseline):
                    text = segment.text
                    if reason is None:
                        remaining = min(self.settings.asr_timeout_seconds, deadline-monotonic())
                        try:
                            if not self._authorization()[0]: raise ProviderError('cost_policy', '本账号已撤销付费授权，已停止后续上传')
                            if remaining <= 0: raise ProviderError('timeout', '付费转写总时限已到')
                            first = round(segment.start * rate); last = min(frames, round(segment.end * rate))
                            if first >= last: raise ProviderError('response', '音频片段为空')
                            source.setpos(first)
                            with wave.open(str(clip), 'wb') as target:
                                target.setparams(source.getparams()); target.writeframes(source.readframes(last-first))
                            with clip.open('rb') as stream:
                                response = self._request(client, 'POST', 'https://api.siliconflow.cn/v1/audio/transcriptions',
                                    headers={'Authorization': 'Bearer ' + self.settings.qwen_asr_api_key},
                                    data={'model': QWEN_MODEL}, files={'file': ('interval.wav', stream, 'audio/wav')}, timeout=remaining)
                            try: value = response.json().get('text')
                            except (ValueError, AttributeError): raise ProviderError('response', 'Qwen 返回格式无效') from None
                            if not isinstance(value, str) or not value.strip(): raise ProviderError('empty', 'Qwen 未返回有效文本')
                            text = value.strip(); paid += 1
                        except ProviderError as error:
                            if not self._authorization()[1]: raise
                            reason = error.category  # No more paid requests after the first failure.
                    if reason is not None and not self._authorization()[1]:
                        raise ProviderError('cost_policy', '本账号已撤销自动兜底授权')
                    output.append(TranscriptSegment(start=segment.start, end=segment.end, text=text))
                    if reporter: reporter.on_transcribe_chunk(index+1, len(baseline))
            return self._result(output,paid,reason)
        finally:
            wav.unlink(missing_ok=True); clip.unlink(missing_ok=True)
