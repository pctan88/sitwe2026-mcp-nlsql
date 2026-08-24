"""Tests for the Stage 3 cross-model LLM abstraction."""

from __future__ import annotations

import json
import pytest

from pilot import llm_client


API_ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_COMPAT_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "XAI_API_KEY",
]


def _clear_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_auto_selects_mock_backend_without_api_keys(monkeypatch):
    _clear_api_keys(monkeypatch)

    backend = llm_client.resolve_backend("auto")

    assert isinstance(backend, llm_client.MockBackend)
    assert backend.backend_id == "mock"
    assert backend.model_id == "mock"


@pytest.mark.parametrize(
    ("short_name", "backend_id", "model_id"),
    [
        ("haiku", "anthropic", llm_client.HAIKU_MODEL),
        ("gpt4o-mini", "openai_compat", llm_client.GPT4O_MINI_MODEL),
        ("gpt-4o", "openai_compat", llm_client.GPT4O_MODEL),
        ("llama31", "openai_compat", llm_client.LLAMA31_MODEL),
        ("qwen-coder", "openai_compat", llm_client.QWEN_CODER_MODEL),
        ("qwen", "openai_compat", llm_client.QWEN_MODEL),
        ("qwen-small", "openai_compat", llm_client.QWEN_SMALL_MODEL),
        ("mock", "mock", "mock"),
    ],
)
def test_model_registry_resolves_short_names(short_name, backend_id, model_id):
    spec = llm_client.resolve_model(short_name)
    backend = llm_client.resolve_backend(short_name)

    assert spec.name == short_name
    assert spec.backend == backend_id
    assert spec.model_id == model_id
    assert backend.name == short_name
    assert backend.backend_id == backend_id
    assert backend.model_id == model_id


def test_mock_llm_response_records_model_identifier():
    resp = llm_client.generate_sql(
        "TABLE artist ( Singer_ID INTEGER, Name TEXT )",
        "How many singers do we have?",
        model="mock",
    )

    assert resp.backend == "mock"
    assert resp.model == "mock"


def test_openai_compat_response_records_model_identifier(monkeypatch):
    def fake_call(system, user, model, *, base_url=None, api_key=None):
        return llm_client.LLMResponse(
            text="SELECT 1",
            backend="openai_compat",
            model=model,
            latency_s=0.01,
            input_tokens=3,
            output_tokens=2,
        )

    monkeypatch.setattr(llm_client, "_call_openai_compat", fake_call)

    resp = llm_client.resolve_backend("llama31").complete("system", "user")

    assert resp.backend == "openai_compat"
    assert resp.model == llm_client.LLAMA31_MODEL


def test_gemini_retries_transient_errors(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    class FakeUsage:
        prompt_token_count = 7
        candidates_token_count = 3

    class FakeResponse:
        text = "SELECT 1"
        usage_metadata = FakeUsage()

    class TransientGeminiError(Exception):
        status_code = 503

    class FakeModels:
        def generate_content(self, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientGeminiError("503 UNAVAILABLE high demand")
            return FakeResponse()

    class FakeClient:
        def __init__(self, api_key=None):
            self.models = FakeModels()

    class FakeGenerateContentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    from google import genai
    from google.genai import types as genai_types
    monkeypatch.setattr(llm_client, "_transient_gemini_errors", lambda: (TransientGeminiError,))
    monkeypatch.setattr(llm_client.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setattr(genai_types, "GenerateContentConfig", FakeGenerateContentConfig)

    resp = llm_client._call_gemini(
        "system",
        "user",
        model=llm_client.GEMINI_MODEL,
        max_retries=3,
        base_delay_s=0.5,
    )

    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]
    assert resp.backend == "gemini"
    assert resp.model == llm_client.GEMINI_MODEL
    assert resp.text == "SELECT 1"


def test_local_thinking_models_get_no_think_tag(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["payload"] = json.loads(req.data.decode())

        class FakeResp:
            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "SELECT 1"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return FakeResp()

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)

    llm_client._call_openai_compat(
        "system prompt", "user", llm_client.QWEN_MODEL,
        base_url="http://localhost:11434/v1",
    )
    system_msg = captured["payload"]["messages"][0]["content"]
    assert system_msg.endswith("/no_think")

    # Non-thinking local model (qwen3-coder) must NOT get the tag.
    llm_client._call_openai_compat(
        "system prompt", "user", llm_client.QWEN_CODER_MODEL,
        base_url="http://localhost:11434/v1",
    )
    system_msg = captured["payload"]["messages"][0]["content"]
    assert "/no_think" not in system_msg


def test_strip_fences_removes_think_blocks():
    assert llm_client._strip_fences(
        "<think>\nreasoning here\n</think>\nSELECT 1"
    ) == "SELECT 1"
    assert llm_client._strip_fences("<think></think>SELECT 1") == "SELECT 1"
    assert llm_client._strip_fences(
        "<think>a</think>```sql\nSELECT 1\n```"
    ) == "SELECT 1"
    # No think block — unchanged behaviour.
    assert llm_client._strip_fences("```sql\nSELECT 1;\n```") == "SELECT 1"
