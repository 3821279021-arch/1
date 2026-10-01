import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.limits import Limits
from app.llm import LLMRouter
from app.model_registry import ModelAllocationError, ModelRegistry

ENV = {
    "DASHSCOPE_API_KEY": "test-only",
    "DASHSCOPE_MODEL": "alpha",
    "DASHSCOPE_MODELS": "alpha,beta,gamma,delta",
    "LLM_RETRY_DELAY": "0",
    "AI_CHUNK_DELAY": "0",
}


class RegistryTests(unittest.TestCase):
    def test_four_distinct_actual_models_and_locked_reservation(self):
        with patch.dict(os.environ, ENV, clear=True):
            registry = ModelRegistry()
            presets = [
                {"id": 2, "provider": "auto"},
                {"id": 3, "provider": "auto"},
                {"id": 4, "model_key": "dashscope:alpha", "model_locked": True},
                {"id": 5, "provider": "auto"},
            ]
            assigned = registry.allocate(presets)
            self.assertEqual(assigned[4], "dashscope:alpha")
            self.assertEqual(len(set(assigned.values())), 4)
            self.assertFalse(any(key.startswith("mock:") for key in assigned.values()))

    def test_strict_shortage_and_explicit_compatibility(self):
        with patch.dict(os.environ, {**ENV, "DASHSCOPE_MODELS": "beta"}, clear=True):
            registry = ModelRegistry()
            presets = [{"id": i, "provider": "auto"} for i in range(2, 6)]
            with self.assertRaisesRegex(ModelAllocationError, "可用独立模型不足"):
                registry.allocate(presets)
            assigned = registry.allocate(presets, unique=False)
            self.assertEqual(len(set(assigned.values())), 2)
            self.assertEqual(len(assigned), 4)
            with self.assertRaises(ModelAllocationError):
                registry.allocate(
                    [
                        {"id": 2, "model_key": "dashscope:alpha", "model_locked": True},
                        {"id": 3, "model_key": "dashscope:alpha", "model_locked": True},
                    ]
                )

    def test_allocation_requires_all_gameplay_capabilities_and_accepts_legacy_mock(self):
        registry = ModelRegistry(
            [
                {"provider": "dashscope", "model": "chat-only", "configured": True, "capabilities": ["chat"]},
                {
                    "provider": "dashscope",
                    "model": "complete",
                    "configured": True,
                    "capabilities": ["chat", "json", "stream"],
                },
            ]
        )
        self.assertEqual(registry.allocate([{"id": 2, "provider": "auto"}]), {2: "dashscope:complete"})
        with self.assertRaisesRegex(ModelAllocationError, "chat/json/stream"):
            registry.allocate([{"id": 2, "model_key": "dashscope:chat-only", "model_locked": True}])
        self.assertEqual(
            registry.allocate([{"id": 2, "model_key": "mock:mock", "provider": "mock"}]), {2: "mock:rule-based-mock"}
        )

    def test_provider_diversity_configuration_and_no_secrets(self):
        with patch.dict(os.environ, {**ENV, "OPENAI_API_KEY": "secret-marker", "OPENAI_MODEL": "gpt-a"}, clear=True):
            registry = ModelRegistry()
            assigned = registry.allocate([{"id": 2, "provider": "auto"}, {"id": 3, "provider": "auto"}])
            self.assertEqual({key.split(":")[0] for key in assigned.values()}, {"openai", "dashscope"})
            self.assertNotIn("secret-marker", json.dumps(registry.entries()))
            self.assertEqual(len(registry.available()), 5)
        with patch.dict(
            os.environ,
            {"AI_MODEL_REGISTRY": '[{"provider":"dashscope","model":"alpha","configured":true}]'},
            clear=True,
        ):
            self.assertFalse(ModelRegistry().get("dashscope:alpha").configured)

    def test_mock_practice_is_explicit_and_not_a_real_inventory_entry(self):
        with patch.dict(os.environ, {}, clear=True):
            registry = ModelRegistry()
            self.assertEqual(registry.available(), [])
            self.assertEqual(
                registry.allocate([{"id": 2, "provider": "mock"}, {"id": 3, "provider": "mock"}]),
                {2: "mock:rule-based-mock", 3: "mock:rule-based-mock"},
            )
            with self.assertRaises(ModelAllocationError):
                registry.allocate([{"id": 2, "provider": "auto"}])


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_primary_then_other_actual_model_same_scoped_prompt(self):
        real_client = httpx.AsyncClient
        received = []

        def handler(request):
            body = json.loads(request.content)
            received.append(body)
            if body["model"] == "alpha":
                raise httpx.ReadTimeout("Sensitive error contents must not be logged", request=request)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"target":3}'}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                },
            )

        with (
            patch.dict(os.environ, ENV, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))),
        ):
            router = LLMRouter()
            with router.request_scope(room_id="room", game_id="game", agent_id="agent", category="vote"):
                result = await router.ask_json(
                    "dashscope:alpha",
                    "legal role-scoped system",
                    "legal view",
                    mock_context={"action": "vote", "options": [3]},
                )
            self.assertEqual([body["model"] for body in received], ["alpha", "alpha", "beta"])
            self.assertTrue(all(body["messages"] == received[0]["messages"] for body in received))
            self.assertEqual(result.provider_used, "dashscope")
            self.assertEqual(result.model_used, "beta")
            self.assertEqual(router.execution("agent")["status"], "switched")
            self.assertEqual(router.execution("agent")["input_tokens"], 20)
            self.assertEqual(router.execution("agent")["output_tokens"], 5)
            self.assertEqual(router.records[-1]["room_id"], "room")
            self.assertNotIn("Sensitive", json.dumps(list(router.records)))
            self.assertNotIn("legal role", json.dumps(list(router.records)))
            self.assertEqual(router.activity["dashscope"]["tokens"], 25)

    async def test_private_skill_health_stays_private_while_internal_routing_changes(self):
        real_client = httpx.AsyncClient

        def handler(request):
            model = json.loads(request.content)["model"]
            if model == "alpha":
                return httpx.Response(500)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"target":3}'}}]})

        with (
            patch.dict(os.environ, ENV, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))),
        ):
            router = LLMRouter()
            before = router.model_status()
            provider_before = router.status()
            with router.request_scope(agent_id="secret-seat", category="seer_inspect"):
                result = await router.ask_json(
                    "dashscope:alpha", "private-role", "view", mock_context={"action": "seer_inspect", "options": [3]}
                )
            self.assertEqual(result.model_used, "beta")
            self.assertFalse(router.registry.get("dashscope:alpha").healthy)
            self.assertNotIn("dashscope:alpha", {entry.key for entry in router.registry.available()})
            self.assertEqual(router.model_status(), before)
            self.assertEqual(router.status(), provider_before)
            for entry in list(router.registry.available()):
                router.registry.failed(entry.key, "private HTTP error", disclose=False)
            self.assertEqual(router.registry.available(), [])
            self.assertEqual(router.status(), provider_before)
            router.registry.failed("dashscope:alpha", "HTTP 500", disclose=True)
            self.assertFalse(router.registry.get("dashscope:alpha").public()["healthy"])
            router.registry.succeeded("dashscope:alpha", disclose=False)
            self.assertTrue(router.registry.get("dashscope:alpha").healthy)
            self.assertFalse(router.registry.get("dashscope:alpha").public()["healthy"])

    async def test_generator_close_from_another_context_settles_budget_and_closes_native(self):
        with patch.dict(os.environ, ENV, clear=True):
            router = LLMRouter()
            limits = Limits(config={"game_max_requests": 1})
            closed = []

            async def native(provider, system, user):
                try:
                    self.assertEqual(router._model(provider), "beta")
                    router._capture_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 1}}, provider)
                    yield "公开片段。"
                    await asyncio.Event().wait()
                finally:
                    closed.append(True)

            router._native_speech = native
            with router.request_scope(room_id="room", game_id="game", agent_id="A", category="speech", guard=limits):
                stream = router.speech_stream("dashscope:beta", "s", "u", {})
                self.assertEqual(await anext(stream), "公开片段。")
                self.assertIsNone(router._selection.get())
                self.assertIsNone(router._usage.get())
            # An async-generator finalizer may run after its request context has ended.
            await asyncio.create_task(stream.aclose())
            self.assertEqual(closed, [True])
            state = limits.export_state()
            self.assertEqual(state["pending"], {})
            usage = limits.snapshot("room", "game")["game"]
            self.assertEqual(usage["requests"], 1)
            self.assertEqual(usage["tokens"], 360)
            self.assertEqual(router.outcome_records[-1]["agent_id"], "A")
            self.assertEqual(router.outcome_records[-1]["failure_reason"], "stream_closed")

    async def test_limits_budget_stops_real_requests_and_outcome_is_explicit(self):
        real_client = httpx.AsyncClient
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(500)

        with (
            patch.dict(os.environ, ENV, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))),
        ):
            router = LLMRouter()
            limits = Limits(config={"game_max_requests": 1})
            with router.request_scope(room_id="room", game_id="game", agent_id="A", category="vote", guard=limits):
                result = await router.ask_json(
                    "dashscope:alpha", "s", "u", mock_context={"action": "vote", "options": [3]}
                )
            self.assertEqual(len(calls), 1)
            self.assertIsNone(result.data["target"])
            self.assertEqual(result.routing["status"], "budget_exhausted")
            self.assertEqual(router.outcome_records[-1]["failure_reason"], "limit:game_request_budget")
            self.assertEqual(router.outcome_records[-1]["category"], "vote")
            self.assertEqual(limits.export_state()["pending"], {})

    async def test_safe_http_failure_logs_exclude_headers_body_and_private_context(self):
        real_client = httpx.AsyncClient

        def handler(request):
            return httpx.Response(401, json={"error": "test-only and private-role-secret"})

        with (
            patch.dict(os.environ, ENV, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))),
        ):
            router = LLMRouter()
            with self.assertLogs("werewolf.models", level="INFO") as logs:
                await router.ask_json(
                    "dashscope:alpha",
                    "private-role-secret",
                    "private-memory-secret",
                    mock_context={"action": "vote", "options": [3]},
                )
            output = "\n".join(logs.output)
            self.assertNotIn("test-only", output)
            self.assertNotIn("Authorization", output)
            self.assertNotIn("private-role-secret", output)
            self.assertNotIn("private-memory-secret", output)
            self.assertIn("HTTP 401", output)
            self.assertIn("mock_fallback", output)

    async def test_invalid_action_format_routes_before_mock_and_all_fail_is_explicit(self):
        real_client = httpx.AsyncClient
        received = []

        def handler(request):
            model = json.loads(request.content)["model"]
            received.append(model)
            content = '{"target":99}' if model == "alpha" else '{"target":3}'
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        with (
            patch.dict(os.environ, ENV, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))),
        ):
            router = LLMRouter()
            result = await router.ask_json("dashscope:alpha", "s", "u", mock_context={"action": "vote", "options": [3]})
            self.assertEqual(result.model_used, "beta")
            self.assertEqual(received, ["alpha", "alpha", "beta"])

        def failed(request):
            return httpx.Response(429)

        with (
            patch.dict(os.environ, ENV, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(failed))),
        ):
            router = LLMRouter()
            result = await router.ask_json("dashscope:alpha", "s", "u", mock_context={"action": "vote", "options": [3]})
            self.assertIsNone(result.data["target"])
            self.assertEqual(len(router.records), 8)
            self.assertEqual(result.routing["status"], "mock_fallback")
            self.assertEqual(result.routing["failure_reason"], "HTTP 429")

    async def test_guard_counts_each_attempt_denial_prevents_backup_http(self):
        real_client = httpx.AsyncClient
        calls = []

        class Guard:
            def __init__(self):
                self.tickets = []
                self.records = []

            def before_call(self, meta):
                if len(self.tickets) == 2:
                    raise RuntimeError("No more budget")
                self.tickets.append(meta)
                return str(len(self.tickets))

            def after_call(self, ticket, record):
                self.records.append((ticket, record))

        def failed(request):
            calls.append(json.loads(request.content)["model"])
            return httpx.Response(500)

        guard = Guard()
        with (
            patch.dict(os.environ, ENV, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(failed))),
        ):
            router = LLMRouter()
            with router.request_scope(
                room_id="room", game_id="game", agent_id="seat-agent", category="vote", guard=guard
            ):
                result = await router.ask_json(
                    "dashscope:alpha", "123", "456", mock_context={"action": "vote", "options": [3]}
                )
            self.assertEqual(calls, ["alpha", "alpha"])
            self.assertEqual(len(guard.records), 2)
            estimate = guard.tickets[0]["estimated_input_tokens"]
            self.assertGreater(estimate, 0)
            self.assertLess(estimate, 134)  # Provider token estimate replaces byte counting.
            self.assertEqual(result.routing["status"], "budget_exhausted")
            self.assertEqual(router.execution("seat-agent")["model_key"], "mock:rule-based-mock")

    async def test_same_provider_concurrent_actual_models_do_not_cross_contexts(self):
        real_client = httpx.AsyncClient
        received = []

        async def handler(request):
            body = json.loads(request.content)
            await asyncio.sleep(0)
            received.append(body)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"target":3}'}}]})

        with (
            patch.dict(os.environ, ENV, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))),
        ):
            router = LLMRouter()

            async def ask(model, identity):
                with router.request_scope(agent_id=identity):
                    return await router.ask_json(
                        f"dashscope:{model}",
                        f"private-{identity}",
                        identity,
                        mock_context={"action": "vote", "options": [3]},
                    )

            alpha, beta = await asyncio.gather(ask("alpha", "A"), ask("beta", "B"))
            self.assertEqual((alpha.model_used, beta.model_used), ("alpha", "beta"))
            for body in received:
                self.assertEqual(
                    body["messages"][0]["content"], "private-A" if body["model"] == "alpha" else "private-B"
                )
            self.assertEqual(router.execution("A")["model_key"], "dashscope:alpha")
            self.assertEqual(router.execution("B")["model_key"], "dashscope:beta")

    async def test_stream_before_first_text_switches_partial_never_repeats(self):
        with patch.dict(os.environ, ENV, clear=True):
            router = LLMRouter()
            used = []

            async def stream(provider, system, user):
                model = router._model(provider)
                used.append(model)
                if model == "alpha":
                    raise httpx.ReadTimeout("Unavailable")
                yield "完整公开发言。"

            router._native_speech = stream
            chunks = [part async for part in router.speech_stream("dashscope:alpha", "s", "u", {})]
            self.assertEqual(chunks, ["完整公开发言。"])
            self.assertEqual(used, ["alpha", "alpha", "beta"])
            self.assertEqual(router.execution()["model_key"], "dashscope:beta")
            used.clear()

            async def partial(provider, system, user):
                used.append(router._model(provider))
                yield "已输出的句子。"
                raise httpx.ReadError("Interrupted")

            router._native_speech = partial
            chunks = [part async for part in router.speech_stream("dashscope:alpha", "s", "u", {})]
            self.assertEqual(chunks, ["已输出的句子。"])
            self.assertEqual(used, ["alpha"])
            self.assertEqual(router.execution()["status"], "partial")

    async def test_anthropic_incremental_usage_partial_does_not_refund_unfinished_output(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-only", "LLM_RETRY_DELAY": "0"}, clear=True):
            router = LLMRouter()
            settled = []

            class Guard:
                def before_call(self, meta):
                    return "ticket"

                def after_call(self, ticket, record):
                    settled.append(record)

            async def complete(*args):
                router._capture_usage({"usage": {"input_tokens": 10, "output_tokens": 1}}, "anthropic")
                yield "公开内容。"
                router._capture_usage({"usage": {"output_tokens": 40}}, "anthropic")

            router._native_speech = complete
            with router.request_scope(guard=Guard()):
                self.assertEqual([c async for c in router.speech_stream("anthropic", "s", "u", {})], ["公开内容。"])
            self.assertEqual(settled[0]["total_tokens"], 50)

            async def interrupted(*args):
                router._capture_usage({"usage": {"input_tokens": 10, "output_tokens": 1}}, "anthropic")
                yield "公开片段。"
                raise httpx.ReadError("Interrupted before final usage")

            router._native_speech = interrupted
            with router.request_scope(guard=Guard()):
                self.assertEqual([c async for c in router.speech_stream("anthropic", "s", "u", {})], ["公开片段。"])
            self.assertEqual(settled[-1]["input_tokens"], 10)
            self.assertNotIn("output_tokens", settled[-1])
            self.assertNotIn("total_tokens", settled[-1])

    async def test_default_price_estimates_are_logged_with_real_usage(self):
        real_client = httpx.AsyncClient

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"target":3}'}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                },
            )

        with (
            patch.dict(
                os.environ, {**ENV, "AI_INPUT_PRICE_PER_MILLION": "2", "AI_OUTPUT_PRICE_PER_MILLION": "4"}, clear=True
            ),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))),
        ):
            router = LLMRouter()
            await router.ask_json("dashscope:alpha", "s", "u", mock_context={"action": "vote", "options": [3]})
            self.assertEqual(router.records[-1]["estimated_cost"], 0.00028)
            self.assertFalse(router.records[-1]["usage_estimated"])

    async def test_total_only_usage_prices_unknown_direction_conservatively(self):
        real_client = httpx.AsyncClient

        def handler(request):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": '{"target":3}'}}], "usage": {"total_tokens": 100}}
            )

        with (
            patch.dict(
                os.environ, {**ENV, "AI_MODEL_PRICES": '{"dashscope:alpha":{"input":1,"output":4}}'}, clear=True
            ),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))),
        ):
            router = LLMRouter()
            await router.ask_json("dashscope:alpha", "s", "u", mock_context={"action": "vote", "options": [3]})
            self.assertEqual(router.records[-1]["estimated_cost"], 0.0004)
            self.assertNotIn("input_tokens", router.records[-1])
            self.assertNotIn("output_tokens", router.records[-1])

    async def test_cancelled_attempt_settles_reserved_budget(self):
        with patch.dict(os.environ, ENV, clear=True):
            router = LLMRouter()
            entered = asyncio.Event()
            settled = []

            class Guard:
                def before_call(self, meta):
                    return "ticket"

                def after_call(self, ticket, record):
                    settled.append((ticket, record))

            async def blocked(*args):
                entered.set()
                await asyncio.Event().wait()

            router._dashscope = blocked

            async def run():
                with router.request_scope(guard=Guard(), agent_id="A"):
                    await router.ask_json("dashscope:alpha", "s", "u")

            task = asyncio.create_task(run())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(len(settled), 1)
            self.assertEqual(settled[0][1]["fallback_reason"], "cancelled")


if __name__ == "__main__":
    unittest.main()
