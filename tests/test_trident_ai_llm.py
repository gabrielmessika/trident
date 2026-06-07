from __future__ import annotations

import tempfile
import unittest

from app.trident_ai import (
    JSONFileLLMCache,
    LLMRequest,
    OpenAIResponsesClient,
    TridentAILLMConfig,
    agent_trade_proposal_json_schema,
    estimate_token_cost_usd,
    openai_responses_payload,
    parse_openai_responses_payload,
)


def _request() -> LLMRequest:
    return LLMRequest(
        request_id="req_fixture_001",
        model="gpt-5.4-mini",
        system_prompt="Return JSON only.",
        user_prompt="Build a safe TRIDENT-AI proposal.",
        schema_name="trident_ai_trade_proposal",
        schema=agent_trade_proposal_json_schema(),
        temperature=0.1,
        metadata={"component": "trident_ai_test"},
    )


def _openai_payload(raw_text: str) -> dict[str, object]:
    return {
        "id": "resp_fixture_001",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": raw_text,
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
        },
    }


class TridentAILLMTests(unittest.TestCase):
    def test_agent_trade_proposal_schema_limits_initial_symbols(self) -> None:
        schema = agent_trade_proposal_json_schema()
        properties = schema["properties"]
        self.assertIsInstance(properties, dict)
        symbol_schema = properties["symbol"]
        self.assertIsInstance(symbol_schema, dict)
        self.assertEqual(symbol_schema["enum"], ["BTC", "ETH", "HYPE", "SOL"])
        self.assertFalse(schema["additionalProperties"])

    def test_builds_openai_responses_payload_with_json_schema(self) -> None:
        payload = openai_responses_payload(_request())

        self.assertEqual(payload["model"], "gpt-5.4-mini")
        text = payload["text"]
        self.assertIsInstance(text, dict)
        fmt = text["format"]
        self.assertIsInstance(fmt, dict)
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["name"], "trident_ai_trade_proposal")
        self.assertTrue(fmt["strict"])
        self.assertNotIn("tools", payload)

    def test_missing_api_key_fails_closed(self) -> None:
        client = OpenAIResponsesClient(TridentAILLMConfig(), api_key="")

        response = client.generate_json(_request())

        self.assertFalse(response.ok)
        self.assertEqual(response.error, "missing_api_key")

    def test_parses_openai_response_json_and_usage(self) -> None:
        response = parse_openai_responses_payload(
            _openai_payload('{"schema_version":"trident_ai_proposal_v1","action":"hold"}'),
            request=_request(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.response_id, "resp_fixture_001")
        self.assertEqual(response.parsed_json["action"], "hold")
        self.assertEqual(response.usage.input_tokens, 1000)
        self.assertEqual(response.usage.output_tokens, 200)
        self.assertEqual(response.usage.total_tokens, 1200)
        self.assertEqual(response.usage.estimated_cost_usd, 0.00165)

    def test_invalid_model_json_fails_closed(self) -> None:
        response = parse_openai_responses_payload(
            _openai_payload("not json"),
            request=_request(),
        )

        self.assertFalse(response.ok)
        self.assertEqual(response.error, "response_json_parse_failed")

    def test_http_error_fails_closed(self) -> None:
        def transport(url, headers, body, timeout_seconds):
            return 429, {"x-request-id": "req_429"}, {"error": {"message": "rate limited"}}

        client = OpenAIResponsesClient(
            TridentAILLMConfig(max_retries=0),
            api_key="sk-test",
            transport=transport,
        )

        response = client.generate_json(_request())

        self.assertFalse(response.ok)
        self.assertEqual(response.error, "http_error:429")
        self.assertEqual(response.response_id, "req_429")

    def test_transient_http_error_retries_once_and_parses_success(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def transport(url, headers, body, timeout_seconds):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 503, {"x-request-id": "req_503"}, {"error": {"message": "busy"}}
            return 200, {}, _openai_payload(
                '{"schema_version":"trident_ai_proposal_v1","action":"hold"}'
            )

        client = OpenAIResponsesClient(
            TridentAILLMConfig(max_retries=1),
            api_key="sk-test",
            transport=transport,
            sleep=sleeps.append,
        )

        response = client.generate_json(_request())

        self.assertTrue(response.ok)
        self.assertEqual(response.parsed_json["action"], "hold")
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [0.25])

    def test_file_cache_avoids_second_transport_call(self) -> None:
        calls = 0

        def transport(url, headers, body, timeout_seconds):
            nonlocal calls
            calls += 1
            return 200, {}, _openai_payload('{"schema_version":"trident_ai_proposal_v1","action":"hold"}')

        with tempfile.TemporaryDirectory() as directory:
            client = OpenAIResponsesClient(
                TridentAILLMConfig(cache_enabled=True),
                api_key="sk-test",
                transport=transport,
                cache=JSONFileLLMCache(directory),
            )

            first = client.generate_json(_request())
            second = client.generate_json(_request())

        self.assertTrue(first.ok)
        self.assertFalse(first.cached)
        self.assertTrue(second.ok)
        self.assertTrue(second.cached)
        self.assertEqual(calls, 1)

    def test_estimate_token_cost_unknown_model_returns_none(self) -> None:
        self.assertIsNone(
            estimate_token_cost_usd(
                model="unknown-model",
                input_tokens=100,
                output_tokens=100,
            )
        )


if __name__ == "__main__":
    unittest.main()
