"""Explicit providers: no cross-provider failover, no audio POST retries, no paid models."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from time import monotonic
from typing import Protocol

import httpx
from app.adapters.audio import FfmpegAudioPreprocessor
from app.adapters.asr import _best_effort_unlink, _split_wav, _wav_duration
from app.config import Settings
from app.errors import WorkflowError
from app.schemas import TranscriptSegment

SF_MODEL = "FunAudioLLM/SenseVoiceSmall"
CF_MODEL = "@cf/openai/whisper-large-v3-turbo"
_LOCAL_GATE = threading.BoundedSemaphore(1)


class Transcriber(Protocol):
    last_duration: float | None
    def transcribe(self, audio_path: Path | None, task_id: str, reporter: object | None = None) -> list[TranscriptSegment]: ...


class ProviderError(WorkflowError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(f"ASR [{category}] {message}；未自动重传或切换服务商。")


@dataclass(frozen=True)
class Capability:
    id: str
    timestamps: bool
    cloud: bool


CAPABILITIES = {p.id: p for p in [Capability("siliconflow", False, True),
    Capability("cloudflare", True, True), Capability("faster_whisper", True, False), Capability("groq", True, True)]}


def pin_provider(request, settings):
    """Persist the destination before queuing, independently of later defaults."""
    from app.asr_settings import effective_settings
    settings = effective_settings(settings, request.client_id)
    if request.asr_provider is None and settings.asr_provider in CAPABILITIES:
        return request.model_copy(update={"asr_provider": settings.asr_provider})
    return request


def provider_catalog(settings: Settings) -> list[dict]:
    ready = {"siliconflow": bool(settings.siliconflow_api_key) and settings.siliconflow_asr_model == SF_MODEL,
             "cloudflare": bool(settings.cloudflare_api_token and re.fullmatch(r"[a-f0-9]{32}", settings.cloudflare_account_id)),
             "faster_whisper": bool(settings.faster_whisper_model_path and Path(settings.faster_whisper_model_path, "model.bin").is_file() and importlib.util.find_spec("faster_whisper")),
             "groq": bool(settings.groq_api_key) and (not settings.asr_free_only or settings.groq_free_tier_confirmed)}
    return [{"id": p.id, "timestamps": p.timestamps, "cloud": p.cloud, "configured": ready[p.id],
             "free_policy": "offline" if not p.cloud else "verified_free_plan" if p.id == "cloudflare" else "free_model" if p.id == "siliconflow" else "operator_free_account",
             "model": SF_MODEL if p.id == "siliconflow" else CF_MODEL if p.id == "cloudflare" else "base-int8" if p.id == "faster_whisper" else settings.groq_asr_model}
            for p in CAPABILITIES.values()]


class UnifiedTranscriber:
    def __init__(self, settings: Settings, preprocessor=None):
        self.settings = settings
        self.provider = settings.asr_provider
        self.timestamps = CAPABILITIES[self.provider].timestamps
        self.preprocessor = preprocessor or FfmpegAudioPreprocessor()
        self.last_duration = None

    def _client(self):
        return httpx.Client(timeout=httpx.Timeout(max(1, self.settings.asr_timeout_seconds), connect=10),
                            trust_env=False, follow_redirects=False)

    def _request(self, client, method, url, **kwargs):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            raise ProviderError("timeout", "服务连接或识别超时") from None
        except httpx.RequestError:
            raise ProviderError("network", "无法连接所选 ASR 服务") from None
        if not response.is_success:
            category = {401:"authentication",403:"access",402:"cost_policy",429:"quota",413:"size"}.get(response.status_code,"upstream")
            raise ProviderError(category, f"{self.provider} HTTP {response.status_code}")
        return response

    def _preflight(self, client):
        if self.provider == "siliconflow":
            if self.settings.siliconflow_asr_model != SF_MODEL:
                raise ProviderError("cost_policy", "严格免费模式仅允许 SenseVoiceSmall，Qwen3-ASR 当前为收费模型")
            if self.settings.asr_require_timestamps:
                raise ProviderError("capability", "SenseVoiceSmall 仅返回纯文本，不能生成真实时间戳；请选择时间戳服务")
            if not self.settings.siliconflow_api_key:
                raise ProviderError("configuration", "未配置 SILICONFLOW_API_KEY")
            response = self._request(client, "GET", "https://www.siliconflow.cn/pricing")
            text = html.unescape(re.sub(r"<[^>]+>", " ", response.text))
            match = re.search(r"FunAudioLLM/SenseVoiceSmall(.{0,400})", text, re.S)
            if not match or "免费" not in match[1] or re.search(r"[¥￥$]\s*\d", match[1].split("免费")[0]):
                raise ProviderError("cost_policy", "无法从官方价格页确认 SenseVoiceSmall 免费，已禁止音频上传")
        elif self.provider == "cloudflare":
            s = self.settings
            if not s.cloudflare_api_token or not re.fullmatch(r"[a-f0-9]{32}", s.cloudflare_account_id):
                raise ProviderError("configuration", "未配置 Cloudflare 账号或 Workers AI 令牌")
            response = self._request(client, "GET", f"https://api.cloudflare.com/client/v4/accounts/{s.cloudflare_account_id}/subscriptions",
                                     headers={"Authorization": f"Bearer {s.cloudflare_api_token}"})
            try:
                plans = response.json()["result"]
                names = [str(p["rate_plan"].get("id", ""))+" "+str(p["rate_plan"].get("name", "")) for p in plans]
            except (ValueError,TypeError,KeyError):
                raise ProviderError("cost_policy", "无法验证 Cloudflare 免费计划") from None
            if any("workers" in n.lower() and "free" not in n.lower() for n in names) or not any("workers" in n.lower() and "free" in n.lower() for n in names):
                raise ProviderError("cost_policy", "仅允许已验证的 Workers Free 计划；付费或未知计划均不上传音频")
        else:
            path = Path(self.settings.faster_whisper_model_path)
            if not self.settings.faster_whisper_model_path or not (path / "model.bin").is_file():
                raise ProviderError("configuration", "离线 Whisper 模型未安装，禁止运行时自动下载")

    def transcribe(self, audio_path, task_id, reporter=None):
        if audio_path is None: raise ProviderError("input", "缺少音频文件")
        wav = Path(audio_path).parent / f"{task_id}-provider-16k.wav"
        chunks = []
        deadline = monotonic() + max(1, self.settings.asr_task_timeout_seconds)
        try:
            with self._client() as client:
                self._preflight(client)  # Capability/cost checks happen before any audio upload.
                self.preprocessor.convert_to_wav16k(audio_path, wav)
                self.last_duration = _wav_duration(wav)
                if not self.last_duration: raise ProviderError("input", "音频为空或时长无效")
                chunks = _split_wav(wav, task_id, 60, self.last_duration) if self.last_duration > 60 else [wav]
                if reporter: reporter.mark_transcribe_started(self.last_duration, len(chunks))
                output = []
                for index, chunk in enumerate(chunks):
                    if monotonic() >= deadline: raise ProviderError("timeout", "转写总时限已到")
                    remaining = max(1, min(self.settings.asr_timeout_seconds, deadline-monotonic()))
                    data = self._recognize(client, chunk, remaining)
                    if self.timestamps:
                        from app.adapters.groq_asr import _parse_segments
                        try: segments = _parse_segments(data, _wav_duration(chunk) or 0)
                        except (ValueError,KeyError,TypeError,OverflowError): raise ProviderError("response", "服务返回的时间戳格式无效") from None
                        output.extend(s.model_copy(update={"start":s.start+index*60,"end":s.end+index*60}) for s in segments)
                    else:
                        if not isinstance(data, dict) or not isinstance(data.get("text"), str): raise ProviderError("response", "服务返回的文本格式无效")
                        output.append(TranscriptSegment(text=data["text"].strip()))
                    if reporter: reporter.on_transcribe_chunk(index+1, len(chunks))
                if not output or not any(s.text for s in output): raise ProviderError("empty", "未识别到有效语音")
                return output
        finally:
            for path in chunks:
                if path != wav: _best_effort_unlink(path)
            _best_effort_unlink(wav)

    def _recognize(self, client, chunk, timeout):
        if self.provider == "faster_whisper":
            started = monotonic()
            if not _LOCAL_GATE.acquire(timeout=timeout): raise ProviderError("capacity", "离线转写繁忙")
            try:
                timeout -= monotonic() - started
                if timeout <= 0: raise ProviderError("timeout", "离线识别等待超时")
                env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
                try:
                    result = subprocess.run([sys.executable, "-m", "app.adapters.whisper_worker", self.settings.faster_whisper_model_path, str(chunk)],
                        env=env, capture_output=True, timeout=timeout, check=False)
                except subprocess.TimeoutExpired: raise ProviderError("timeout", "离线识别超时，子进程已终止") from None
                if result.returncode: raise ProviderError("local_runtime", "离线识别进程失败，请检查模型与内存")
                try: return json.loads(result.stdout)
                except ValueError: raise ProviderError("response", "离线结果格式无效") from None
            finally: _LOCAL_GATE.release()
        if self.provider == "siliconflow":
            with chunk.open("rb") as stream:
                r = self._request(client, "POST", "https://api.siliconflow.cn/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.settings.siliconflow_api_key}"},
                    data={"model": SF_MODEL}, files={"file": ("audio.wav", stream, "audio/wav")}, timeout=timeout)
        else:
            r = self._request(client, "POST", f"https://api.cloudflare.com/client/v4/accounts/{self.settings.cloudflare_account_id}/ai/run/{CF_MODEL}",
                headers={"Authorization": f"Bearer {self.settings.cloudflare_api_token}"},
                json={"audio": base64.b64encode(chunk.read_bytes()).decode(), "task": "transcribe"}, timeout=timeout)
        try:
            data = r.json()
            if self.provider == "cloudflare":
                if data.get("success") is not True: raise ProviderError("upstream", "Cloudflare 拒绝了识别请求")
                return data["result"]
            return data
        except (ValueError,KeyError,TypeError): raise ProviderError("response", "ASR 返回了无效 JSON") from None
