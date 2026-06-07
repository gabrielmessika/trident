from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.trident_ai.config import TridentAILLMConfig


OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}

TransportFn = Callable[
    [str, dict[str, str], dict[str, object], float],
    tuple[int, dict[str, str], dict[str, object]],
]
SleepFn = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = None

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object] | None,
        *,
        model: str,
        pricing: Mapping[str, tuple[float, float]] | None = None,
    ) -> LLMUsage:
        payload = payload or {}
        input_tokens = _int_value(
            payload.get("input_tokens", payload.get("prompt_tokens", 0))
        )
        output_tokens = _int_value(
            payload.get("output_tokens", payload.get("completion_tokens", 0))
        )
        total_tokens = _int_value(
            payload.get("total_tokens", input_tokens + output_tokens)
        )
        estimated_cost = estimate_token_cost_usd(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            pricing=pricing,
        )
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass(frozen=True, slots=True)
class LLMRequest:
    request_id: str
    model: str
    system_prompt: str
    user_prompt: str
    schema_name: str
    schema: dict[str, object]
    temperature: float = 0.1
    metadata: dict[str, str] = field(default_factory=dict)

    def cache_payload(self) -> dict[str, object]:
        return {
            "model": self.model,
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "schema_name": self.schema_name,
            "schema": self.schema,
            "temperature": self.temperature,
            "metadata": dict(sorted(self.metadata.items())),
        }


@dataclass(frozen=True, slots=True)
class LLMResponse:
    ok: bool
    provider: str
    model: str
    request_id: str
    parsed_json: dict[str, object] | None = None
    raw_text: str = ""
    error: str = ""
    response_id: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    cached: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "parsed_json": self.parsed_json,
            "raw_text": self.raw_text,
            "error": self.error,
            "response_id": self.response_id,
            "usage": self.usage.to_dict(),
            "cached": self.cached,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> LLMResponse:
        usage_payload = payload.get("usage")
        model = str(payload.get("model", ""))
        usage = (
            LLMUsage.from_mapping(
                usage_payload if isinstance(usage_payload, Mapping) else {},
                model=model,
            )
        )
        parsed = payload.get("parsed_json")
        return cls(
            ok=bool(payload.get("ok", False)),
            provider=str(payload.get("provider", "")),
            model=model,
            request_id=str(payload.get("request_id", "")),
            parsed_json=dict(parsed) if isinstance(parsed, Mapping) else None,
            raw_text=str(payload.get("raw_text", "")),
            error=str(payload.get("error", "")),
            response_id=str(payload.get("response_id", "")),
            usage=usage,
            cached=bool(payload.get("cached", False)),
        )


class JSONFileLLMCache:
    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)

    def get(self, cache_key: str) -> LLMResponse | None:
        path = self._path(cache_key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        response = LLMResponse.from_dict(payload)
        return LLMResponse(
            ok=response.ok,
            provider=response.provider,
            model=response.model,
            request_id=response.request_id,
            parsed_json=response.parsed_json,
            raw_text=response.raw_text,
            error=response.error,
            response_id=response.response_id,
            usage=response.usage,
            cached=True,
        )

    def put(self, cache_key: str, response: LLMResponse) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(response.to_dict())
        payload["cached"] = False
        path = self._path(cache_key)
        tmp_path = path.with_name(f".{path.name}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)

    def _path(self, cache_key: str) -> Path:
        return self.cache_dir / f"{cache_key}.json"


class OpenAIResponsesClient:
    def __init__(
        self,
        config: TridentAILLMConfig,
        *,
        api_key: str | None = None,
        transport: TransportFn | None = None,
        cache: JSONFileLLMCache | None = None,
        pricing: Mapping[str, tuple[float, float]] | None = None,
        responses_url: str = OPENAI_RESPONSES_URL,
        sleep: SleepFn | None = None,
    ) -> None:
        self.config = config
        self.api_key = api_key if api_key is not None else os.getenv(OPENAI_API_KEY_ENV, "")
        self.transport = transport or _urllib_post_json
        self.cache = cache
        self.pricing = pricing or OPENAI_PRICE_PER_1M_TOKENS
        self.responses_url = responses_url
        self.sleep = sleep or time.sleep

    def generate_json(self, request: LLMRequest) -> LLMResponse:
        if self.config.provider != "openai":
            return _failed_response(
                request,
                provider=self.config.provider,
                error="unsupported_provider",
            )
        if not self.api_key:
            return _failed_response(request, provider="openai", error="missing_api_key")

        cache_key = llm_request_cache_key(provider="openai", request=request)
        if self.config.cache_enabled and self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = openai_responses_payload(request)
        max_attempts = max(1, self.config.max_retries + 1)
        response_headers: dict[str, str] = {}
        payload: dict[str, object] = {}
        status_code = 0
        for attempt in range(1, max_attempts + 1):
            try:
                status_code, response_headers, payload = self.transport(
                    self.responses_url,
                    headers,
                    body,
                    self.config.timeout_seconds,
                )
            except Exception:
                if attempt < max_attempts:
                    self.sleep(_retry_delay_seconds(attempt))
                    continue
                return _failed_response(request, provider="openai", error="request_failed")

            if status_code < 200 or status_code >= 300:
                if _is_transient_http_error(status_code) and attempt < max_attempts:
                    self.sleep(_retry_delay_seconds(attempt))
                    continue
                return _failed_response(
                    request,
                    provider="openai",
                    error=f"http_error:{status_code}",
                    response_id=response_headers.get("x-request-id", ""),
                )
            break

        response = parse_openai_responses_payload(
            payload,
            request=request,
            pricing=self.pricing,
        )
        if self.config.cache_enabled and self.cache is not None and response.ok:
            self.cache.put(cache_key, response)
        return response


def openai_responses_payload(request: LLMRequest) -> dict[str, object]:
    return {
        "model": request.model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": request.system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": request.user_prompt}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": request.schema_name,
                "schema": request.schema,
                "strict": True,
            }
        },
        "temperature": request.temperature,
        "metadata": request.metadata,
    }


def parse_openai_responses_payload(
    payload: Mapping[str, object],
    *,
    request: LLMRequest,
    pricing: Mapping[str, tuple[float, float]] | None = None,
) -> LLMResponse:
    response_id = str(payload.get("id", ""))
    raw_text = extract_openai_output_text(payload)
    if not raw_text:
        return _failed_response(
            request,
            provider="openai",
            error="empty_output_text",
            response_id=response_id,
            usage=LLMUsage.from_mapping(
                payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {},
                model=request.model,
                pricing=pricing,
            ),
        )
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return _failed_response(
            request,
            provider="openai",
            error="response_json_parse_failed",
            raw_text=raw_text,
            response_id=response_id,
        )
    if not isinstance(parsed, dict):
        return _failed_response(
            request,
            provider="openai",
            error="response_json_not_object",
            raw_text=raw_text,
            response_id=response_id,
        )
    return LLMResponse(
        ok=True,
        provider="openai",
        model=request.model,
        request_id=request.request_id,
        parsed_json=parsed,
        raw_text=raw_text,
        response_id=response_id,
        usage=LLMUsage.from_mapping(
            payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {},
            model=request.model,
            pricing=pricing,
        ),
    )


def extract_openai_output_text(payload: Mapping[str, object]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text
    parts: list[str] = []
    output = payload.get("output", [])
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for content_item in content:
            if not isinstance(content_item, Mapping):
                continue
            text = content_item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def llm_request_cache_key(*, provider: str, request: LLMRequest) -> str:
    payload = {
        "provider": provider,
        "request": request.cache_payload(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


OPENAI_PRICE_PER_1M_TOKENS: dict[str, tuple[float, float]] = {
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.5": (5.00, 30.00),
}


def estimate_token_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: Mapping[str, tuple[float, float]] | None = None,
) -> float | None:
    price_table = pricing or OPENAI_PRICE_PER_1M_TOKENS
    prices = price_table.get(model)
    if prices is None:
        return None
    input_price, output_price = prices
    return round(
        (input_tokens * input_price + output_tokens * output_price) / 1_000_000.0,
        8,
    )


def _is_transient_http_error(status_code: int) -> bool:
    return status_code in TRANSIENT_HTTP_STATUS_CODES


def _retry_delay_seconds(attempt: int) -> float:
    return min(2.0, 0.25 * attempt)


def _failed_response(
    request: LLMRequest,
    *,
    provider: str,
    error: str,
    raw_text: str = "",
    response_id: str = "",
    usage: LLMUsage | None = None,
) -> LLMResponse:
    return LLMResponse(
        ok=False,
        provider=provider,
        model=request.model,
        request_id=request.request_id,
        raw_text=raw_text,
        error=error,
        response_id=response_id,
        usage=usage or LLMUsage(),
    )


def _urllib_post_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, object],
    timeout_seconds: float,
) -> tuple[int, dict[str, str], dict[str, object]]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, dict(response.headers.items()), payload
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        return exc.code, dict(exc.headers.items()), payload


def _int_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)
