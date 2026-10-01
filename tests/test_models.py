import os
import unittest
from unittest.mock import patch

import httpx

from app.llm import LLMResult, LLMRouter


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_retry_and_mock_fallback(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only", "AI_CHUNK_DELAY": "0"}, clear=True):
            router = LLMRouter()
            calls = []

            async def invalid(*args):
                calls.append(1)
                return LLMResult({}, "openai", "model")

            router._openai = invalid
            result = await router.ask_json(
                "openai", "system", "user", mock_context={"action": "vote", "options": [2], "player_id": 1}
            )
            self.assertEqual(len(calls), 2)
            self.assertIsNone(result.data["target"])
            self.assertIn("fallback", result.provider_used)
            self.assertEqual(result.routing["status"], "mock_fallback")

    async def test_speech_retry_fallback_and_partial_stream(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only", "AI_CHUNK_DELAY": "0"}, clear=True):
            router = LLMRouter()
            calls = []

            async def failed(*args):
                calls.append(1)
                raise httpx.ConnectError("Unavailable")
                yield ""

            router._native_speech = failed
            chunks = [
                c
                async for c in router.speech_stream(
                    "openai", "system", "user", {"personality": "detective", "player_id": 1, "alive": [1, 2]}
                )
            ]
            self.assertTrue(chunks)
            self.assertEqual(len(calls), 2)

            async def partial(*args):
                yield "公开发言片段"
                raise httpx.ReadError("Interrupted")

            router._native_speech = partial
            chunks = [c async for c in router.speech_stream("openai", "system", "user", {})]
            self.assertEqual(chunks, ["公开发言片段"])

    async def test_dashscope_json_contract_and_no_auth_retry(self):
        real_client = httpx.AsyncClient
        requests = []

        def handler(request):
            import json

            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "qwen-plus")
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertFalse(payload["enable_thinking"])
            requests.append(request)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": '{"target":3}'}}], "usage": {"total_tokens": 100}}
            )

        transport = httpx.MockTransport(handler)
        with (
            patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-only", "DASHSCOPE_MODEL": "qwen-plus"}, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kwargs: real_client(transport=transport)),
        ):
            router = LLMRouter()
            result = await router.ask_json("dashscope", "system", "user")
            self.assertEqual(result.provider_used, "dashscope")
            self.assertEqual(result.data, {"target": 3})
            self.assertEqual(router.activity["dashscope"]["tokens"], 100)
        requests = []

        def unauthorized(request):
            requests.append(request)
            return httpx.Response(401, json={"error": {"code": "invalid_api_key"}})

        transport = httpx.MockTransport(unauthorized)
        with (
            patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-only"}, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kwargs: real_client(transport=transport)),
        ):
            router = LLMRouter()
            result = await router.ask_json(
                "dashscope", "system", "user", mock_context={"action": "vote", "options": [2]}
            )
            self.assertEqual(len(requests), 1)
            self.assertIn("fallback", result.provider_used)
            self.assertEqual(router.activity["dashscope"]["last_error"], "HTTP 401")

    async def test_native_stream_only_public_text(self):
        streams = {
            "openai": 'data: {"type":"response.reasoning_summary_text.delta","delta":"秘密推理"}\n\ndata: {"type":"response.output_text.delta","delta":"公开发言"}\n\n',
            "anthropic": 'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"秘密推理"}}\n\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"公开发言"}}\n\n',
            "dashscope": 'data: {"choices":[{"delta":{"reasoning_content":"秘密推理"}}]}\n\ndata: {"choices":[{"delta":{"content":"公开发言"}}]}\n\ndata: [DONE]\n\n',
            "gemini": 'data: {"candidates":[{"content":{"parts":[{"thought":true,"text":"秘密推理"},{"text":"公开发言"}]}}]}\n\n',
        }
        real_client = httpx.AsyncClient
        for provider, sse in streams.items():
            transport = httpx.MockTransport(
                lambda request: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPENAI_API_KEY": "test-only",
                        "ANTHROPIC_API_KEY": "test-only",
                        "GEMINI_API_KEY": "test-only",
                        "DASHSCOPE_API_KEY": "test-only",
                    },
                    clear=True,
                ),
                patch("app.llm.httpx.AsyncClient", lambda **kwargs: real_client(transport=transport)),
            ):
                chunks = [c async for c in LLMRouter()._native_speech(provider, "system", "user")]
                self.assertEqual("".join(chunks), "公开发言")


if __name__ == "__main__":
    unittest.main()
