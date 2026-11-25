"""Provider contract tests against controlled HTTP responses."""

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from telegram_ai_newsroom.editorial import Score, portable_schema
from telegram_ai_newsroom.llm import (
    GeminiProvider,
    ModelError,
    ModelProfile,
    OpenAICompatibleProvider,
    ProviderKind,
    Request,
    Usage,
)

REQUEST = Request(
    "score",
    "score.v1",
    "Rate this article",
    {"article": "source"},
    portable_schema(Score.model_json_schema()),
)


def envelope(kind: ProviderKind, case: str) -> dict[str, Any]:
    if case == "bad_envelope":
        return {}
    if kind == "gemini":
        if case == "prompt_block":
            return {"promptFeedback": {"blockReason": "SAFETY"}}
        return {
            "candidates": [
                {
                    "finishReason": {"refusal": "SAFETY", "truncated": "MAX_TOKENS"}.get(
                        case, "STOP"
                    ),
                    "content": {"parts": [{"text": "thought", "thought": True}, {"text": "{}"}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 80,
                "candidatesTokenCount": 12,
                "thoughtsTokenCount": 3,
            },
        }
    return {
        "choices": [
            {
                "finish_reason": "length" if case == "truncated" else "stop",
                "message": {
                    "content": None if case == "bad_content" else "{}",
                    "refusal": "blocked" if case == "refusal" else None,
                },
            }
        ],
        "usage": {"prompt_tokens": 80, "completion_tokens": 15},
    }


@pytest.mark.parametrize("kind", ["gemini", "openai_compatible"])
@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("ok", None),
        ("refusal", "refusal"),
        ("truncated", "incomplete_output"),
        ("bad_envelope", "invalid_envelope"),
        ("invalid_json", "invalid_envelope"),
        ("prompt_block", "refusal"),
        ("bad_content", "invalid_envelope"),
        ("429", "rate_limit"),
        ("401", "authentication"),
        ("400", "request_or_capability"),
        ("503", "provider_unavailable"),
        ("302", "unexpected_http_status"),
        ("timeout", "timeout"),
        ("network", "network_error"),
        ("provider_error", "provider_error"),
    ],
)
def test_provider_contract(kind: ProviderKind, case: str, error: str | None) -> None:
    if (case == "prompt_block" and kind != "gemini") or (
        case == "bad_content" and kind == "gemini"
    ):
        return

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if kind == "gemini":
            assert request.headers["x-goog-api-key"] == "test-secret"
            assert payload["generationConfig"]["responseJsonSchema"] == REQUEST.schema
        else:
            assert request.url.path == "/api/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer test-secret"
            assert payload["response_format"]["json_schema"]["strict"]
            assert payload["provider"] == {"require_parameters": True}
        if case == "timeout":
            raise httpx.ReadTimeout("simulated")
        if case == "network":
            raise httpx.ConnectError("simulated")
        if case.isdigit():
            return httpx.Response(int(case))
        if case == "invalid_json":
            return httpx.Response(200, content=b"not json")
        if case == "provider_error":
            return httpx.Response(200, json={"error": {"message": "failure"}})
        return httpx.Response(200, json=envelope(kind, case))

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            profile = ModelProfile(
                backend=kind,
                model="test-model",
                base_url="https://openrouter.ai/api/v1" if kind == "openai_compatible" else None,
                input_per_million_usd=Decimal("1"),
                output_per_million_usd=Decimal("2"),
            )
            adapter = GeminiProvider if kind == "gemini" else OpenAICompatibleProvider
            provider = adapter(client, SecretStr("test-secret"))
            if error:
                with pytest.raises(ModelError, match=error):
                    await provider.complete(profile, REQUEST)
            else:
                reply = await provider.complete(profile, REQUEST)
                assert reply.text == "{}"
                assert reply.usage == Usage(80, 15)
                assert profile.cost(reply.usage) == Decimal("0.00011")

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "profile",
    [
        {"backend": "gemini", "model": "../bad"},
        {"backend": "openai_compatible", "base_url": "http://example.com"},
        {"base_url": "https://example.com"},
    ],
)
def test_profile_validation(profile: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelProfile.model_validate(profile)
