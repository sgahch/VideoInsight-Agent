from __future__ import annotations

import httpx
import pytest

from app.adapters.llm import OpenAICompatibleAnalyzer
from app.config import Settings
from app.errors import WorkflowError
from app.schemas import AnalysisMode, AnalyzeRequest
from app.workflow import _generate_output, _select_llm


def _analyzer() -> OpenAICompatibleAnalyzer:
    return OpenAICompatibleAnalyzer(
        Settings(),
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        api_key="test-key",
    )


def test_llm_402_has_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    response = httpx.Response(402, request=request)

    def post(*args: object, **kwargs: object) -> httpx.Response:
        return response

    monkeypatch.setattr(httpx, "post", post)

    with pytest.raises(WorkflowError) as exc_info:
        _analyzer()._complete("test")

    message = str(exc_info.value)
    assert "DeepSeek 账户余额不足或尚未开通计费" in message
    assert "充值" in message
    assert "developer.mozilla.org" not in message


def test_llm_network_error_does_not_expose_httpx_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def post(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("secret transport details")

    monkeypatch.setattr(httpx, "post", post)

    with pytest.raises(WorkflowError) as exc_info:
        _analyzer()._complete("test")

    message = str(exc_info.value)
    assert message == "LLM 服务连接失败，请检查网络、API Base URL 后重试。"
    assert "secret transport details" not in message


def test_analysis_api_key_is_ephemeral() -> None:
    request = AnalyzeRequest(
        url="https://example.com/video",
        mode=AnalysisMode.summary,
        use_mock=False,
        llm_provider="custom",
        llm_base_url="https://llm.example.com/v1",
        llm_model="example-model",
        llm_api_key="never-persist-this-key",
    )

    assert request.llm_api_key == "never-persist-this-key"
    serialized = request.model_dump_json()
    assert "never-persist-this-key" not in serialized
    assert "llm_api_key" not in serialized
    assert "llm_base_url" not in serialized


def test_transcript_mode_does_not_create_or_call_llm() -> None:
    request = AnalyzeRequest(
        url="https://example.com/video",
        mode=AnalysisMode.transcript,
        use_mock=False,
    )

    assert _select_llm(request, False, Settings()) is None
    assert _generate_output(None, AnalysisMode.transcript, "逐字稿正文", {}) == "逐字稿正文"


def test_ai_analysis_uses_request_model_config() -> None:
    request = AnalyzeRequest(
        url="https://example.com/video",
        mode=AnalysisMode.summary,
        use_mock=False,
        llm_provider="custom",
        llm_base_url="https://llm.example.com/v1",
        llm_model="example-model",
        llm_api_key="request-key",
    )

    analyzer = _select_llm(request, False, Settings())

    assert isinstance(analyzer, OpenAICompatibleAnalyzer)
    assert analyzer._provider() == "custom"
    assert analyzer._base_url() == "https://llm.example.com/v1"
    assert analyzer._model() == "example-model"
