"""Structured model requests and provider adapters."""

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

Operation = Literal["score", "draft", "review"]
ProviderKind = Literal["fake", "gemini", "openai_compatible"]


class ModelError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: ProviderKind = "fake"
    model: str = Field(default="fixture-v1", min_length=1, max_length=200)
    base_url: str | None = None
    api_key_env: str | None = None
    structured_output: bool = True
    input_per_million_usd: Decimal | None = Field(default=None, ge=0)
    output_per_million_usd: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_endpoint(self) -> "ModelProfile":
        if self.backend == "gemini" and not re.fullmatch(r"[A-Za-z0-9_.-]+", self.model):
            raise ValueError("Gemini model must be an unqualified model ID")
        if self.backend == "openai_compatible":
            url = urlsplit(self.base_url or "")
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.query
                or url.fragment
            ):
                raise ValueError("A clean HTTPS base_url is required")
        elif self.base_url is not None:
            raise ValueError("Only openai_compatible accepts a custom base_url")
        return self

    def cost(self, usage: "Usage") -> Decimal | None:
        if (
            self.input_per_million_usd is None
            or self.output_per_million_usd is None
            or usage.input is None
            or usage.output is None
        ):
            return None
        return (
            self.input_per_million_usd * usage.input + self.output_per_million_usd * usage.output
        ) / 1_000_000


@dataclass(frozen=True)
class Usage:
    input: int | None = None
    output: int | None = None


@dataclass(frozen=True)
class Request:
    operation: Operation
    version: str
    instructions: str
    data: dict[str, str]
    schema: dict[str, Any]


@dataclass(frozen=True)
class Reply:
    text: str
    usage: Usage = Usage()


class Provider(Protocol):
    async def complete(self, profile: ModelProfile, request: Request) -> Reply: ...


class FakeProvider:
    async def complete(self, profile: ModelProfile, request: Request) -> Reply:
        title, article = request.data["title"], request.data["article"]
        if request.operation == "score":
            scores = (80, 75, 70, 75) if len(article.split()) >= 80 else (20, 20, 20, 20)
            result = dict(
                zip(("relevance", "importance", "novelty", "source_quality"), scores, strict=True)
            )
            return Reply(json.dumps({**result, "reason": "Deterministic fixture"}), Usage(0, 0))
        quote = article.splitlines()[0][:400]
        if request.operation == "draft":
            return Reply(
                json.dumps(
                    {
                        "headline": title[:120],
                        "body": quote,
                        "tags": ["tech"],
                        "supporting_facts": [quote],
                    }
                ),
                Usage(0, 0),
            )
        return Reply(json.dumps({"supported": True, "issues": []}), Usage(0, 0))


def _tokens(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


class HttpProvider:
    def __init__(self, client: httpx.AsyncClient, key: SecretStr) -> None:
        self.client = client
        self.key = key

    async def _post(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            response = await self.client.post(
                url, headers=headers, json=body, follow_redirects=False
            )
        except httpx.TimeoutException as exc:
            raise ModelError("timeout") from exc
        except httpx.RequestError as exc:
            raise ModelError("network_error") from exc
        if response.status_code == 429:
            raise ModelError("rate_limit")
        if response.status_code in {401, 403}:
            raise ModelError("authentication")
        if response.status_code in {400, 404, 422}:
            raise ModelError("request_or_capability")
        if response.status_code >= 500:
            raise ModelError("provider_unavailable")
        if not response.is_success:
            raise ModelError("unexpected_http_status")
        try:
            data: object = response.json()
        except ValueError as exc:
            raise ModelError("invalid_envelope") from exc
        if not isinstance(data, dict):
            raise ModelError("invalid_envelope")
        if data.get("error"):
            raise ModelError("provider_error")
        return data


class GeminiProvider(HttpProvider):
    async def complete(self, profile: ModelProfile, request: Request) -> Reply:
        data = await self._post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{profile.model}:generateContent",
            {"x-goog-api-key": self.key.get_secret_value()},
            {
                "systemInstruction": {"parts": [{"text": request.instructions}]},
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": json.dumps(request.data, ensure_ascii=False)}],
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": request.schema,
                    "maxOutputTokens": 2048,
                },
            },
        )
        try:
            if data.get("promptFeedback", {}).get("blockReason"):
                raise ModelError("refusal")
            candidate = data["candidates"][0]
            finish = candidate.get("finishReason")
            if finish in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
                raise ModelError("refusal")
            if finish != "STOP":
                raise ModelError("incomplete_output")
            content = "".join(
                part["text"]
                for part in candidate["content"]["parts"]
                if "text" in part and not part.get("thought")
            )
            usage = data.get("usageMetadata") or {}
            output = _tokens(usage.get("candidatesTokenCount"))
            thoughts = _tokens(usage.get("thoughtsTokenCount"))
            return Reply(
                content,
                Usage(
                    _tokens(usage.get("promptTokenCount")),
                    None if output is None else output + (thoughts or 0),
                ),
            )
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModelError("invalid_envelope") from exc


class OpenAICompatibleProvider(HttpProvider):
    async def complete(self, profile: ModelProfile, request: Request) -> Reply:
        assert profile.base_url is not None
        body: dict[str, Any] = {
            "model": profile.model,
            "messages": [
                {"role": "system", "content": request.instructions},
                {"role": "user", "content": json.dumps(request.data, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.operation,
                    "strict": True,
                    "schema": request.schema,
                },
            },
            "max_completion_tokens": 2048,
        }
        if urlsplit(profile.base_url).hostname == "openrouter.ai":
            body["provider"] = {"require_parameters": True}
        data = await self._post(
            f"{profile.base_url.rstrip('/')}/chat/completions",
            {"Authorization": f"Bearer {self.key.get_secret_value()}"},
            body,
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise ModelError("refusal")
            if choice.get("finish_reason") != "stop":
                raise ModelError("incomplete_output")
            content = message["content"]
            if not isinstance(content, str):
                raise ModelError("invalid_envelope")
            usage = data.get("usage") or {}
            return Reply(
                content,
                Usage(_tokens(usage.get("prompt_tokens")), _tokens(usage.get("completion_tokens"))),
            )
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModelError("invalid_envelope") from exc
