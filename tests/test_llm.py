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
        model="deepseek-flash",
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


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages"])
def test_official_deepseek_rejects_display_name_before_request(monkeypatch, protocol):
    def unexpected_request(*args, **kwargs):
        raise AssertionError("Invalid model names must not reach the provider")

    monkeypatch.setattr(httpx, "post", unexpected_request)
    monkeypatch.setattr(httpx, "stream", unexpected_request)
    analyzer = OpenAICompatibleAnalyzer(
        Settings(), provider="deepseek", base_url="https://api.deepseek.com",
        model="DeepSeek V4 Flash", api_key="private-key", protocol=protocol,
    )
    with pytest.raises(WorkflowError, match="deepseek-v4-flash"):
        analyzer.probe()
    with pytest.raises(WorkflowError, match="API 模型 ID"):
        list(analyzer.chat_stream("question", "transcript"))


@pytest.mark.parametrize("model", [
    "deepseek-flash", "deepseek-pro", "deepseek-v4-flash", "deepseek-v4-pro", "future-model",
])
def test_official_deepseek_model_ids_are_not_rewritten(model):
    analyzer = OpenAICompatibleAnalyzer(
        Settings(), base_url="https://api.deepseek.com/v1", model=model, api_key="test-key",
    )
    assert analyzer._build_payload("test", None)["model"] == model


@pytest.mark.parametrize("base_url", [
    "https://gateway.example/v1", "https://api.deepseek.com.gateway.example/v1",
])
def test_custom_gateways_keep_their_model_names_and_probe_parameters(base_url):
    analyzer = OpenAICompatibleAnalyzer(
        Settings(), provider="deepseek", base_url=base_url, model="DeepSeek V4 Flash",
        api_key="test-key", protocol="responses",
    )
    payload = analyzer._build_payload("test", 32, for_probe=True)
    assert payload["model"] == "DeepSeek V4 Flash"
    assert "reasoning" not in payload
    assert "thinking" not in payload


@pytest.mark.parametrize("protocol,path,answer", [
    ("chat_completions", "/chat/completions", {"choices": [{"message": {"content": "正常"}}]}),
    ("responses", "/responses", {"output": [{"type": "message", "content": [{"type": "output_text", "text": "正常"}]}]}),
])
def test_deepseek_probe_disables_thinking_only_for_short_test(monkeypatch, protocol, path, answer):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        assert url == "https://api.deepseek.com" + path
        return httpx.Response(200, request=httpx.Request("POST", url), json=answer)

    monkeypatch.setattr(httpx, "post", post)
    analyzer = OpenAICompatibleAnalyzer(
        Settings(), provider="deepseek", base_url="https://api.deepseek.com",
        model="deepseek-v4-flash", api_key="test-key", protocol=protocol,
    )
    result = analyzer.probe()
    assert result["ok"] and result["sample"] == "正常"
    assert result["model"] == "deepseek-v4-flash" and result["protocol"] == protocol
    assert calls[0]["model"] == "deepseek-v4-flash"
    if protocol == "responses":
        assert calls[0]["reasoning"] == {"effort": "none"}
        assert calls[0]["max_output_tokens"] == 32
    else:
        assert calls[0]["thinking"] == {"type": "disabled"}
        assert calls[0]["max_tokens"] == 32
    regular_payload = analyzer._build_payload("analyze", None)
    assert "reasoning" not in regular_payload
    assert "thinking" not in regular_payload


def test_empty_probe_does_not_report_success(monkeypatch):
    monkeypatch.setattr(OpenAICompatibleAnalyzer, "_complete", lambda *args, **kwargs: " ")
    with pytest.raises(WorkflowError, match="空内容"):
        _analyzer().probe()


def test_llm_400_suggests_model_id_without_exposing_response(monkeypatch):
    def post(url, **kwargs):
        return httpx.Response(
            400, request=httpx.Request("POST", url),
            json={"error": {"message": "invalid model private-key"}},
        )

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(WorkflowError) as exc_info:
        _analyzer().probe()
    message = str(exc_info.value)
    assert "HTTP 400" in message and "deepseek-v4-flash" in message
    assert "private-key" not in message


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
