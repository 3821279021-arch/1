"""V3 credential isolation and native routing tests use mock HTTP, never live keys."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet

from app.credentials import (
    CredentialError,
    CredentialService,
    check_public_endpoint,
    normalize_base_url,
    validate_model_options,
)
from app.llm import LLMRouter
from app.model_registry import ModelEntry
from app.providers import SecretStreamFilter, provider_request


class VaultTests(unittest.TestCase):
    def test_persistent_ciphertext_owner_isolation_replace_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vault.sqlite3"
            vault = CredentialService(path)
            key = "sk-owner-secret-12345678"
            saved = vault.create("owner", "openai", key)
            self.assertNotIn(key, json.dumps(saved))
            self.assertNotIn(key.encode(), path.read_bytes())
            self.assertEqual(vault.list("someone-else"), [])
            for operation in (
                lambda: vault.get("someone-else", saved["id"]),
                lambda: vault.replace("someone-else", saved["id"], "bad-key"),
                lambda: vault.delete("someone-else", saved["id"]),
            ):
                with self.assertRaises(CredentialError) as caught:
                    operation()
                self.assertEqual(caught.exception.status_code, 404)
            self.assertTrue(vault.route_valid("owner", saved["id"], 1))
            replacement = vault.replace("owner", saved["id"], "sk-replacement-87654321")
            self.assertEqual(replacement["revision"], 2)
            self.assertFalse(vault.route_valid("owner", saved["id"], 1))
            vault.close()
            restored = CredentialService(path)
            self.assertEqual(restored.resolve("owner", saved["id"]).secret, "sk-replacement-87654321")
            restored.delete("owner", saved["id"])
            self.assertFalse(restored.route_valid("owner", saved["id"], 2))
            restored.close()

    def test_temporary_room_scope_session_scope_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vault.sqlite3"
            vault = CredentialService(path)
            room = vault.create("owner", "gemini", "room-key-1234", temporary=True, scope_id="room-a")
            session = vault.create("owner", "gemini", "session-key-1234", temporary=True, scope_id="session:owner")
            self.assertEqual([item["id"] for item in vault.list("owner")], [session["id"]])
            self.assertEqual(len(vault.list("owner", scope_id="room-a")), 2)
            self.assertEqual(vault.get("owner", session["id"], scope_id="room-b")["id"], session["id"])
            with self.assertRaises(CredentialError):
                vault.get("owner", room["id"], scope_id="room-b")
            self.assertNotIn(b"room-key-1234", path.read_bytes())
            vault.clear_scope("room-a")
            with self.assertRaises(CredentialError):
                vault.get("owner", room["id"], scope_id="room-a")
            vault.close()
            restarted = CredentialService(path)
            self.assertEqual(restarted.list("owner", scope_id="room-a"), [])
            restarted.close()

    def test_invalid_urls_keys_and_model_ids_rejected(self):
        for url in (
            "http://example.com",
            "https://127.0.0.1",
            "https://10.0.0.1/v1",
            "https://[::1]/v1",
            "https://localhost/v1",
            "https://user:password@example.com",
            "https://example.com:8080",
            "https://example.com/?key=secret",
        ):
            with self.subTest(url=url), self.assertRaises(CredentialError):
                normalize_base_url("openai-compatible", url)
        vault = CredentialService(":memory:")
        with self.assertRaises(CredentialError):
            vault.create("owner", "openai", "key\r\nAuthorization: injected")
        with self.assertRaises(CredentialError):
            vault.create("owner", "openai", "key", temporary=True)
        saved = vault.create("owner", "openai", "key-123456")
        self.assertEqual(
            vault.validate_route("owner", saved["id"], "future-model-2030")["model_id"], "future-model-2030"
        )
        with self.assertRaises(CredentialError):
            vault.validate_route("owner", saved["id"], "../../secrets")
        vault.close()

    def test_wrong_encryption_key_has_safe_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vault.sqlite3"
            vault = CredentialService(path, Fernet.generate_key())
            saved = vault.create("owner", "openai", "sk-secret-123456")
            vault.close()
            another = CredentialService(path, Fernet.generate_key())
            with self.assertRaises(CredentialError) as caught:
                another.resolve("owner", saved["id"])
            self.assertEqual(caught.exception.status_code, 409)
            self.assertNotIn("sk-secret", str(caught.exception))
            another.close()

    def test_safe_option_validation_and_bad_port_error(self):
        for options in (
            {"temperature": float("nan")},
            {"temperature": True},
            {"reasoning_effort": []},
            {"max_output_tokens": 100000},
            {"headers": {"Authorization": "secret"}},
            {"thinking_budget": -1},
        ):
            with self.subTest(options=options), self.assertRaises(CredentialError):
                validate_model_options(options)
        with self.assertRaises(CredentialError) as caught:
            normalize_base_url("openai-compatible", "https://example.com:raw-secret-port")
        self.assertNotIn("raw-secret-port", str(caught.exception))

    def test_cross_chunk_secret_redaction(self):
        redactor = SecretStreamFilter("sk-live-secret-1234")
        output = redactor.feed("安全文本 sk-live-") + redactor.feed("secret-1234 后续") + redactor.finish()
        self.assertEqual(output, "安全文本 •••••• 后续")


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_dynamic_catalog_all_native_providers_and_compatible(self):
        vault = CredentialService(":memory:")
        calls = []

        def handle(request):
            calls.append(request)
            if "generativelanguage" in request.url.host:
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {
                                "name": "models/gemini-future-2030",
                                "displayName": "Future",
                                "supportedGenerationMethods": ["generateContent"],
                            },
                            {"name": "models/embedding-only", "supportedGenerationMethods": ["embedContent"]},
                        ]
                    },
                )
            return httpx.Response(200, json={"data": [{"id": "new-account-model-2030"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            for provider in ("openai", "anthropic", "gemini", "dashscope", "openai-compatible"):
                saved = vault.create(
                    "owner",
                    provider,
                    f"secret-{provider}-1234",
                    base_url="https://api.openai.com/v1" if provider == "openai-compatible" else None,
                )
                result = await vault.discover("owner", saved["id"], client=client)
                self.assertTrue(result["verified"])
                self.assertEqual(len(result["models"]), 1)
                self.assertTrue(result["credential"]["last_verified_at"])
                self.assertNotIn(f"secret-{provider}", json.dumps(result))
            self.assertEqual(str(calls[3].url), "https://dashscope.aliyuncs.com/compatible-mode/v1/models")
            self.assertIn("x-api-key", calls[1].headers)
            self.assertIn("x-goog-api-key", calls[2].headers)
        vault.close()

    async def test_manual_probe_unsupported_lists_and_safe_failure(self):
        vault = CredentialService(":memory:")
        saved = vault.create("owner", "openai-compatible", "sk-secret-abcdef", base_url="https://api.openai.com/v1")

        def handle(request):
            if request.method == "GET":
                return httpx.Response(404, json={"error": "key sk-secret-abcdef and internal stack"})
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            discovery = await vault.discover("owner", saved["id"], client=client)
            self.assertFalse(discovery["verified"])
            self.assertTrue(discovery["manual_entry_allowed"])
            self.assertNotIn("sk-secret-abcdef", json.dumps(discovery))
            tested = await vault.test("owner", saved["id"], model_id="manual-id", client=client)
            self.assertTrue(tested["verified"])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "sk-secret-abcdef"}))
        ) as client:
            result = await vault.test("owner", saved["id"], model_id="manual-id", client=client)
            self.assertFalse(result["verified"])
            self.assertNotIn("sk-secret-abcdef", json.dumps(result))
        vault.close()

    async def test_private_dns_answer_blocked(self):
        loop = type("FakeLoop", (), {"getaddrinfo": AsyncMock(return_value=[(2, 1, 6, "", ("192.168.0.3", 443))])})()
        with patch("app.credentials.asyncio.get_running_loop", return_value=loop):
            with self.assertRaises(CredentialError):
                await check_public_endpoint("https://models.example.com/v1")

    async def test_custom_dns_is_pinned_and_redirects_are_not_followed(self):
        vault = CredentialService(":memory:")
        saved = vault.create(
            "owner", "openai-compatible", "secret-custom-1234", base_url="https://custom.example.com/v1"
        )
        loop = type("FakeLoop", (), {"getaddrinfo": AsyncMock(return_value=[(2, 1, 6, "", ("8.8.8.8", 443))])})()
        requests = []

        def handle(request):
            requests.append(request)
            self.assertEqual(request.url.host, "8.8.8.8")
            self.assertEqual(request.headers["Host"], "custom.example.com")
            self.assertEqual(request.extensions["sni_hostname"], "custom.example.com")
            return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
            with patch("app.credentials.asyncio.get_running_loop", return_value=loop):
                result = await vault.discover("owner", saved["id"], client=client)
        self.assertFalse(result["verified"])
        self.assertEqual(len(requests), 1)
        vault.close()

    async def test_refresh_does_not_verify_replaced_key(self):
        vault = CredentialService(":memory:")
        saved = vault.create("owner", "openai", "old-secret-1234")

        async def handle(request):
            vault.replace("owner", saved["id"], "new-secret-5678")
            return httpx.Response(200, json={"data": [{"id": "old-key-model"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await vault.discover("owner", saved["id"], client=client)
        self.assertFalse(result["verified"])
        self.assertEqual(vault.get("owner", saved["id"])["status"], "unverified")
        vault.close()


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_options_native_mapping_and_budget_reservation(self):
        vault, router = CredentialService(":memory:"), LLMRouter()
        router.credentials = vault
        for provider in ("openai", "anthropic", "gemini", "dashscope", "openai-compatible"):
            saved = vault.create(
                "owner",
                provider,
                "options-secret-1234",
                base_url="https://api.openai.com/v1" if provider == "openai-compatible" else None,
            )
            credential = vault.resolve("owner", saved["id"])
            options = {"temperature": 0.4, "reasoning_effort": "low", "max_output_tokens": 4096}
            _, _, payload = provider_request(credential, "future-model", "system", "view", parameters=options)
            if provider == "openai":
                self.assertEqual(payload["reasoning"], {"effort": "low"})
            elif provider == "anthropic":
                self.assertEqual(payload["thinking"]["budget_tokens"], 2048)
                self.assertEqual(payload["max_tokens"], 6144)
            elif provider == "gemini":
                self.assertFalse(payload["generationConfig"]["thinkingConfig"]["includeThoughts"])
            elif provider == "dashscope":
                self.assertTrue(payload["parameters"]["enable_thinking"])
            else:
                self.assertEqual(payload["reasoning_effort"], "low")
            with router.request_scope(model_options=options):
                _, _, metadata = await router._before_call(
                    ModelEntry(provider, "future-model", True),
                    provider + ":future-model",
                    {"action": "vote", "options": [2]},
                    "system",
                    "view",
                    False,
                )
            self.assertEqual(metadata["max_output_tokens"], 6144 if provider == "anthropic" else 4096)
        await router.close()
        vault.close()

    async def test_failed_real_routes_skip_actions_practice_still_plays(self):
        router = LLMRouter()
        for action in ("vote", "wolf_kill", "seer_inspect", "guard_protect", "hunter_shoot", "wolf_beauty_charm"):
            ctx = {"action": action, "alive": [1, 2, 3], "player_id": 1, "options": [2, 3]}
            self.assertIsNone(router._mock(ctx, error="HTTP 401").data["target"])
            self.assertIn(router._mock(ctx).data["target"], [2, 3])
        self.assertEqual(router._mock({"action": "witch"}, error="HTTP 401").data["save"], False)
        self.assertIn("模型连接失败", router._mock({"action": "speech"}, error="HTTP 401").data["speech"])
        await router.close()

    async def test_concurrent_byok_never_uses_platform_keys(self):
        vault = CredentialService(":memory:")
        first = vault.create("owner", "openai-compatible", "key-first-1234", base_url="https://api.openai.com/v1")
        second = vault.create("owner", "openai-compatible", "key-second-5678", base_url="https://api.openai.com/v1")
        seen = []

        async def handle(request):
            body = json.loads(request.content)
            seen.append((request.headers["Authorization"], body["model"], body["messages"]))
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"target":2}'}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                },
            )

        router = LLMRouter()
        router.credentials = vault
        router._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))

        async def run(credential, model):
            with router.request_scope(**vault.validate_route("owner", credential["id"], model)):
                return await router.ask_json(
                    "openai-compatible:" + model,
                    "system",
                    "legal view",
                    mock_context={"action": "vote", "options": [2]},
                )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "platform-must-not-be-used"}):
            a, b = await asyncio.gather(run(first, "future-a"), run(second, "future-b"))
        self.assertEqual(
            {(key, model) for key, model, _ in seen},
            {("Bearer key-first-1234", "future-a"), ("Bearer key-second-5678", "future-b")},
        )
        self.assertEqual((a.model, b.model), ("future-a", "future-b"))
        self.assertEqual(a.input_tokens, 12)
        self.assertNotIn("key-first", json.dumps(list(router.records)))
        self.assertNotIn("credential_id", json.dumps(seen))
        await router.close()
        vault.close()

    async def test_all_native_generate_protocols(self):
        expected = {
            "openai": "/v1/responses",
            "anthropic": "/v1/messages",
            "gemini": "/v1beta/models/future-model:generateContent",
            "dashscope": "/api/v1/services/aigc/text-generation/generation",
        }
        vault, router = CredentialService(":memory:"), LLMRouter()
        router.credentials = vault

        def handle(request):
            body = json.loads(request.content)
            self.assertEqual(request.url.path, expected[current])
            if current == "openai":
                return httpx.Response(
                    200,
                    json={
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"target":2}'}]}]
                    },
                )
            if current == "anthropic":
                return httpx.Response(
                    200, json={"content": [{"type": "tool_use", "name": "game_action", "input": {"target": 2}}]}
                )
            if current == "gemini":
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"target":2}'}]}}]})
            self.assertIn("parameters", body)
            return httpx.Response(200, json={"output": {"choices": [{"message": {"content": '{"target":2}'}}]}})

        router._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        for current in expected:
            saved = vault.create("owner", current, "secret-123456")
            with router.request_scope(**vault.validate_route("owner", saved["id"], "future-model")):
                result = await router.ask_json(
                    current, "system", "view", mock_context={"action": "guard_protect", "options": [2]}
                )
            self.assertEqual(result.provider, current)
            self.assertEqual(result.data["target"], 2)
        await router.close()
        vault.close()

    async def test_native_stream_protocols_exclude_thoughts(self):
        vault, router = CredentialService(":memory:"), LLMRouter()
        router.credentials = vault

        def handle(request):
            if current == "openai":
                events = [
                    {"type": "response.reasoning_text.delta", "delta": "private"},
                    {"type": "response.output_text.delta", "delta": "公开"},
                ]
            elif current == "anthropic":
                events = [
                    {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "private"}},
                    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "公开"}},
                ]
            elif current == "gemini":
                events = [
                    {"candidates": [{"content": {"parts": [{"thought": True, "text": "private"}, {"text": "公开"}]}}]}
                ]
            else:
                self.assertEqual(request.headers["X-DashScope-SSE"], "enable")
                self.assertTrue(json.loads(request.content)["parameters"]["incremental_output"])
                events = [{"output": {"choices": [{"message": {"reasoning_content": "private", "content": "公开"}}]}}]
            return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))

        router._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        for current in ("openai", "anthropic", "gemini", "dashscope"):
            saved = vault.create("owner", current, "native-stream-secret-1234")
            with router.request_scope(**vault.validate_route("owner", saved["id"], "future-model")):
                text = "".join([chunk async for chunk in router.speech_stream(current, "system", "view", {})])
            self.assertEqual(text, "公开")
        await router.close()
        vault.close()

    async def test_deleted_bound_key_only_mock_with_explicit_reason(self):
        vault, router = CredentialService(":memory:"), LLMRouter()
        router.credentials = vault
        saved = vault.create("owner", "openai", "deleted-secret-1234")
        route = vault.validate_route("owner", saved["id"], "future-model")
        vault.delete("owner", saved["id"])
        router._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: self.fail("Deleted BYOK must never fall back to platform HTTP")
            )
        )
        with router.request_scope(**route):
            result = await router.ask_json("openai", "system", "view", mock_context={"action": "vote", "options": [2]})
        self.assertEqual(result.routing["failure_reason"], "credential_unavailable")
        self.assertEqual(result.routing["status"], "mock_fallback")
        await router.close()
        vault.close()

    async def test_compatible_stream_hides_reasoning_and_uses_bound_model(self):
        vault, router = CredentialService(":memory:"), LLMRouter()
        router.credentials = vault
        saved = vault.create("owner", "openai-compatible", "stream-secret-1234", base_url="https://api.openai.com/v1")

        def handle(request):
            self.assertEqual(json.loads(request.content)["model"], "future-stream")
            self.assertEqual(request.headers["Authorization"], "Bearer stream-secret-1234")
            events = [
                {"choices": [{"delta": {"reasoning_content": "hidden chain"}}]},
                {"choices": [{"delta": {"content": "公开发言"}}]},
                {"usage": {"prompt_tokens": 7, "completion_tokens": 3}, "choices": []},
            ]
            return httpx.Response(
                200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
            )

        router._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        ctx = {"_model_route": vault.validate_route("owner", saved["id"], "future-stream")}
        chunks = [part async for part in router.speech_stream("openai-compatible", "system", "view", ctx)]
        self.assertEqual(chunks, ["公开发言"])
        self.assertEqual(router.execution()["output_tokens"], 3)
        await router.close()
        vault.close()

    async def test_replaced_temporary_key_rejects_old_in_flight_result(self):
        vault, router = CredentialService(":memory:"), LLMRouter()
        router.credentials = vault
        saved = vault.create(
            "owner",
            "openai-compatible",
            "temp-old-1234",
            base_url="https://api.openai.com/v1",
            temporary=True,
            scope_id="room-a",
        )

        def handle(request):
            vault.replace("owner", saved["id"], "temp-new-5678", scope_id="room-a")
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"target":2}'}}]})

        router._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        router.retries = 0
        with router.request_scope(**vault.validate_route("owner", saved["id"], "future-model", scope_id="room-a")):
            result = await router.ask_json(
                "openai-compatible", "system", "view", mock_context={"action": "vote", "options": [2]}
            )
        self.assertEqual(result.routing["status"], "mock_fallback")
        await router.close()
        vault.close()
