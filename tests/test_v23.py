"""V2.3 regression: bounded private memory, durable sessions and room security."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.ai import AIOrchestrator, memory_from
from app.game import WerewolfGame
from app.llm import LLMRouter
from app.main import app
from app.memory import MEMORY_LIMITS, new_memory, update_memory
from app.persistence import Store
from app.rules import RuleEngine
from app.scope import InformationScope
from app.strategy import GameBeliefState
from app.tokens import estimate_tokens


def speech(seq, pid, text, day=1):
    return {
        "type": "speech",
        "event_id": f"s:{seq}",
        "day": day,
        "audience": "public",
        "data": {"player_id": pid, "speech": text},
    }


def game_view():
    game = WerewolfGame("v23-test", "owner")
    engine = RuleEngine(game)
    engine.join("owner", "甲", 1)
    engine.start("owner")
    for player, role in zip(game.players, ["villager", "wolf", "seer", "witch", "villager", "wolf"]):
        player.role = role
    engine.enter("day_vote")
    return game, engine, InformationScope.ai_view(game, 1)


class MemoryAndStrategyTests(unittest.TestCase):
    def test_rule_engine_runs_22_complete_days_with_bounded_memories(self):
        game = WerewolfGame("long-rounds", "owner")
        engine = RuleEngine(game)
        engine.join("owner", "玩家", 1)
        clock = 1000.0
        engine.start("owner", now=clock)
        actions = 0
        days = set()
        ai = AIOrchestrator(LLMRouter())
        sizes = []
        while game.day <= 22 and not game.game_over:
            days.add(game.day)
            pending = next(((p, engine.action_for(p.id)) for p in game.players if engine.action_for(p.id)), None)
            if pending:
                player, action = pending
                payload = {
                    "action": action["type"],
                    "game_id": game.game_id,
                    "turn_sequence": game.turn_sequence,
                    "turn_id": game.turn_id,
                    "target": None,
                    "save": False,
                    "poison_target": None,
                }
                if action["type"] == "speech":
                    payload["speech"] = "我是村民，怀疑3号，支持4号。先核对公开票型。"
                    view = InformationScope.ai_view(game, player.id)
                    _, prompt, _ = ai.context(view, player.personality, memory_from(view))
                    sizes.append(estimate_tokens("dashscope", "qwen-plus", prompt))
                engine.apply(player.id, payload, now=clock)
                actions += 1
            else:
                clock = game.turn_deadline + 0.01
                engine.tick(now=clock)
        self.assertEqual(len(days), 22)
        self.assertEqual(len(game.alive_players()), 6)
        self.assertGreater(actions, 300)
        self.assertLessEqual(max(sizes), 6000)
        for player in game.players:
            for key, limit in MEMORY_LIMITS.items():
                self.assertLessEqual(len(player.memory[key]), limit)

    def test_natural_hints_remain_unconfirmed_and_have_provenance(self):
        memory = update_memory(new_memory(), speech(1, 2, "2号怎么看都不太对，4大概率进狼坑，2、5至少出一狼。"), 1)
        self.assertEqual(len(memory["semantic_hints"]), 3)
        self.assertTrue(
            all(not hint["confirmed"] and hint["source_player_id"] == 2 for hint in memory["semantic_hints"])
        )
        self.assertFalse(memory["facts"])

    def test_wolf_private_claims_do_not_become_public_expression_facts(self):
        game, _, _ = game_view()
        view = InformationScope.ai_view(game, 2)
        memory = new_memory()
        for seq, text in [(1, "我是村民。"), (2, "我是女巫。"), (3, "我是预言家。")]:
            event = speech(seq, 6, text)
            event["audience"] = "wolves"
            memory = update_memory(memory, event, 2)
        memory = update_memory(memory, speech(4, 3, "我是预言家。"), 2)
        state = GameBeliefState.from_view(view, memory)
        self.assertTrue(all(c["visibility"] == "wolves" for c in memory["contradictions"]))
        self.assertTrue(all("私有信息" in point for point in state.evidence[6]))
        self.assertFalse(any("多个seer" in point for points in state.evidence.values() for point in points))
        plan = state.speech_plan(2, game.alive_ids())
        self.assertFalse(any("私有信息" in point for point in plan["points"]))

    def test_all_collections_bounded_and_early_role_changes_survive_summary(self):
        memory = new_memory()
        for seq in range(1, 601):
            day = (seq - 1) // 20 + 1
            memory = update_memory(memory, speech(seq, 2, "我是预言家，查杀3号。支持4号，更怀疑5号。", day), 1)
            memory = update_memory(
                memory,
                {
                    "type": "vote",
                    "event_id": f"v:{seq}",
                    "day": day,
                    "audience": "public",
                    "data": {"votes": {"1": 3, "2": 3, "4": 5}, "text": "投票结束"},
                },
                1,
            )
        for key, limit in MEMORY_LIMITS.items():
            self.assertLessEqual(len(memory[key]), limit, key)
        self.assertEqual(memory["summary"]["role_claims"]["2"]["first"]["event_id"], "s:1")
        self.assertFalse(memory["summary"]["role_claims"]["2"]["first"]["confirmed"])
        self.assertEqual(memory["daily_summaries"][-1]["day"], 29)
        self.assertTrue(memory["daily_summaries"][-1]["vote_patterns"])
        changed = update_memory(memory, speech(601, 2, "我是女巫。", 31), 1)
        self.assertTrue(any("身份声称" in item["summary"] for item in changed["contradictions"]))

    def test_prompt_size_stabilizes_over_30_days(self):
        _, _, view = game_view()
        memory = new_memory()
        sizes = []
        ai = AIOrchestrator(LLMRouter())
        for day in range(1, 31):
            for offset in range(18):
                seq = day * 100 + offset
                memory = update_memory(
                    memory, speech(seq, offset % 6 + 1, "我是村民，怀疑3号，支持4号，请核对公开票型。", day), 1
                )
            view["day"] = day
            _, user, _ = ai.context(view, "detective", memory)
            sizes.append(estimate_tokens("dashscope", "qwen-plus", user))
        self.assertLessEqual(max(sizes), 6000)
        self.assertLess(max(sizes[-10:]) - min(sizes[-10:]), 800)
        self.assertLess(sizes[-1], sizes[9] * 1.3)

    def test_daily_summary_keeps_private_provenance(self):
        memory = update_memory(
            new_memory(),
            {
                "type": "private_note",
                "event_id": "check",
                "day": 1,
                "audience": "player",
                "player_id": 1,
                "data": {"note": "查验：3号是狼人"},
            },
            1,
        )
        memory = update_memory(memory, speech(2, 2, "我怀疑4号。", 2), 1)
        fact = memory["daily_summaries"][0]["confirmed_facts"][0]
        self.assertEqual(fact["visibility"], "private")
        self.assertTrue(fact["confirmed"])
        outsider = update_memory(
            new_memory(),
            {
                "type": "private_note",
                "event_id": "check",
                "audience": "player",
                "player_id": 1,
                "data": {"note": "查验：3号是狼人"},
            },
            2,
        )
        self.assertFalse(outsider["facts"])

    def test_heuristic_claim_conflict_vote_and_time_decay(self):
        _, _, view = game_view()
        memory = update_memory(new_memory(), speech(1, 2, "我是预言家。"), 1)
        memory = update_memory(memory, speech(2, 3, "我是预言家。"), 1)
        state = GameBeliefState.from_view(view, memory)
        self.assertGreater(state.alignment_probabilities[2], 0.33)
        self.assertLess(state.credibility[2], 0.6)
        memory = update_memory(
            memory, {"type": "vote", "event_id": "votes", "audience": "public", "data": {"votes": {"1": 5, "2": 5}}}, 1
        )
        new = GameBeliefState.from_view(view, memory)
        self.assertGreater(new.alignment_probabilities[5], state.alignment_probabilities[5])
        old_view = {**view, "day": 10}
        old = GameBeliefState.from_view(old_view, memory)
        self.assertLess(old.alignment_probabilities[5], new.alignment_probabilities[5])

    def test_private_check_overrides_accusations_and_wolf_plan_is_scoped(self):
        game, engine, view = game_view()
        memory = update_memory(
            new_memory(),
            {
                "type": "private_note",
                "event_id": "check",
                "day": 1,
                "audience": "player",
                "player_id": 1,
                "data": {"note": "查验：3号是好人"},
            },
            1,
        )
        for seq in range(10):
            memory = update_memory(memory, speech(seq, 2, "怀疑3号。"), 1)
        state = GameBeliefState.from_view(view, memory)
        self.assertEqual(state.alignment_probabilities[3], 0.01)
        for p in game.players:
            p.role = "villager"
        game.player(2).role = game.player(4).role = "wolf"
        good = GameBeliefState.from_view(InformationScope.ai_view(game, 1), game.player(1).memory)
        wolf = GameBeliefState.from_view(InformationScope.ai_view(game, 2), game.player(2).memory)
        self.assertIsNone(good.wolf_plan)
        self.assertIsNotNone(wolf.wolf_plan)
        self.assertEqual(wolf.relationships[4], "狼队")
        self.assertNotIn("4号是狼人", json.dumps(good.dump(), ensure_ascii=False))


class ProviderInfrastructureTests(unittest.IsolatedAsyncioTestCase):
    def test_gemini_reasoning_usage_is_accounted_as_output(self):
        router = LLMRouter()
        usage = {}
        token = router._usage.set(usage)
        try:
            router._capture_usage(
                {
                    "usageMetadata": {
                        "promptTokenCount": 100,
                        "candidatesTokenCount": 20,
                        "thoughtsTokenCount": 50,
                        "totalTokenCount": 170,
                    }
                },
                "gemini",
            )
        finally:
            router._usage.reset(token)
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 70)

    async def test_usage_report_survives_store_restart(self):
        from app.rooms import RoomManager

        real = httpx.AsyncClient
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"target":3}'}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
                },
            )
        )
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test"}, clear=True),
            patch("app.llm.httpx.AsyncClient", lambda **kw: real(transport=transport)),
        ):
            store = Store(folder + "/db.sqlite")
            router = LLMRouter()
            manager = RoomManager(store, router)
            with router.request_scope(room_id="r", game_id="g", agent_id="a", day=1, phase="day_vote"):
                await router.ask_json("dashscope", "s", "u", mock_context={"action": "vote", "options": [3]})
            await manager.close()
            store.close()
            reopened = Store(folder + "/db.sqlite")
            report = LLMRouter().cost_report("r", "g", reopened.model_report("r", "g"))
            self.assertTrue(report["durable"])
            self.assertEqual(report["calls"], 1)
            self.assertEqual(next(iter(report["groups"].values()))["input_tokens"], 50)
            reopened.close()

    async def test_storage_write_does_not_block_event_loop(self):
        from app.rooms import RoomManager

        class SlowStore(Store):
            def save(self, *args, **kwargs):
                time.sleep(0.05)
                return super().save(*args, **kwargs)

        store = SlowStore(":memory:")
        manager = RoomManager(store, LLMRouter())
        ticks = []

        async def ticker():
            for _ in range(5):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.005)

        await asyncio.gather(manager.create("owner", "玩家", 1, "测试", "standard"), ticker())
        self.assertLess(ticks[-1] - ticks[0], 0.045)
        await manager.close()
        store.close()

    async def test_shared_client_schema_repair_usage_and_close(self):
        requests = []
        clients = []
        real = httpx.AsyncClient

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            result = '{"target":99}' if len(requests) == 1 else '{"target":3}'
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": result}}],
                    "usage": {"prompt_tokens": 80, "completion_tokens": 8, "total_tokens": 88},
                },
            )

        def factory(**kw):
            client = real(transport=httpx.MockTransport(handler))
            clients.append(client)
            return client

        with (
            patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test", "LLM_RETRY_DELAY": "0"}, clear=True),
            patch("app.llm.httpx.AsyncClient", factory),
        ):
            router = LLMRouter()
            for _ in range(2):
                with router.request_scope(room_id="r", game_id="g", agent_id="a", day=2, phase="day_vote"):
                    result = await router.ask_json(
                        "dashscope", "s", "u", mock_context={"action": "vote", "options": [3]}
                    )
            self.assertEqual(len(clients), 1)
            self.assertIn("上次输出无效", requests[1]["messages"][1]["content"])
            self.assertEqual(result.input_tokens, 80)
            self.assertEqual(result.output_tokens, 8)
            self.assertIn("estimated_actual_ratio", router.records[-1])
            self.assertEqual(router.records[-1]["day"], 2)
            report = router.cost_report("r", "g")
            self.assertEqual(report["calls"], 3)
            self.assertEqual(report["usage_samples"], 3)
            await router.close()
            self.assertTrue(clients[0].is_closed)

    async def test_native_schema_for_openai_anthropic_and_gemini(self):
        real = httpx.AsyncClient
        for provider in ["openai", "anthropic", "gemini"]:
            payloads = []

            def handler(request):
                payloads.append(json.loads(request.content))
                if provider == "openai":
                    body = {
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"target":3}'}]}],
                        "usage": {"input_tokens": 20, "output_tokens": 5},
                    }
                elif provider == "anthropic":
                    body = {
                        "content": [{"type": "tool_use", "name": "game_action", "input": {"target": 3}}],
                        "usage": {"input_tokens": 20, "output_tokens": 5},
                    }
                else:
                    body = {
                        "candidates": [{"content": {"parts": [{"text": '{"target":3}'}]}}],
                        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 5},
                    }
                return httpx.Response(200, json=body)

            with (
                patch.dict(os.environ, {f"{provider.upper()}_API_KEY": "test"}, clear=True),
                patch("app.llm.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler))),
            ):
                router = LLMRouter()
                result = await router.ask_json(provider, "s", "u", mock_context={"action": "vote", "options": [3]})
                self.assertEqual(result.data, {"target": 3})
                self.assertEqual(result.input_tokens, 20)
                native = payloads[0]
                schema = (
                    native["text"]["format"]["schema"]
                    if provider == "openai"
                    else native["tools"][0]["input_schema"]
                    if provider == "anthropic"
                    else native["generationConfig"]["responseJsonSchema"]
                )
                self.assertEqual(schema["required"], ["target"])
                self.assertFalse(schema["additionalProperties"])
                await router.close()

    async def test_unconfigured_model_uses_disclosed_legal_practice_fallback(self):
        _, _, view = game_view()
        memory = memory_from(view)
        router = LLMRouter()
        ai = AIOrchestrator(router)
        _, _, ctx = ai.context(view, "detective", memory)
        first = await ai.propose(view, "mock", "detective", memory)
        second = await ai.propose(view, "openai", "detective", memory)
        self.assertNotIn("decision", ctx)
        self.assertIn(first["target"], [None, *ctx["options"]])
        self.assertIsNone(second["target"])
        self.assertEqual(router.execution()["status"], "mock_fallback")

    def test_chinese_estimation_is_not_utf8_and_no_prompt_logging(self):
        text = "请核对狼人杀的公开发言与投票记录。" * 100
        for provider in ["dashscope", "anthropic", "gemini"]:
            estimated = estimate_tokens(provider, "test", text)
            self.assertGreater(estimated, len(text) * 0.8)
            self.assertLess(estimated, len(text.encode()) / 2)


class PersistenceTests(unittest.TestCase):
    def test_legacy_session_schema_migrates_and_recovery_rotates_credentials(self):
        store = Store(":memory:")
        owner, token = store.identity()
        code = store.recovery_code(owner)
        restored = store.recover(code)
        self.assertEqual(restored["owner_id"], owner)
        self.assertIsNone(store.authenticate(token))
        self.assertEqual(store.authenticate(restored["token"]), owner)
        with self.assertRaises(ValueError):
            store.recover(code)
        row = store.db.execute("SELECT recovery_hash FROM identities").fetchone()[0]
        self.assertNotIn(restored["recovery_code"], row)
        store.revoke(owner)
        self.assertIsNone(store.authenticate(restored["token"]))
        self.assertEqual(store.recover(restored["recovery_code"])["owner_id"], owner)
        store.close()

    def test_archive_purges_snapshots_events_memory_and_keeps_anonymous_stats(self):
        store = Store(":memory:")
        game, engine, _ = game_view()
        game.lifecycle = "ARCHIVED"
        game.archived_at = time.time() - 20 * 86400
        store.save(game, engine.outbox)
        self.assertGreater(store.db.execute("SELECT COUNT(*) FROM room_events").fetchone()[0], 0)
        self.assertEqual(store.cleanup_expired_rooms(now=time.time(), retention_days=14), [game.room_id])
        for table in ["rooms", "room_events", "player_memory"]:
            self.assertEqual(store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        stats = store.db.execute("SELECT statistics FROM room_statistics").fetchone()[0]
        self.assertNotIn("owner", stats)
        self.assertNotIn("甲", stats)
        store.close()


class MultiplayerAPITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": self.folder.name + "/test.sqlite",
                "AI_CHUNK_DELAY": "0",
                "AI_TURN_PAUSE": "0",
                "GAME_TIME_SCALE": "1",
                "RATE_SESSION_PER_MINUTE": "100",
            },
            clear=True,
        )
        self.env.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.host = self.client.post("/api/session").json()
        self.friend = self.client.post("/api/session").json()
        self.h = {"Authorization": "Bearer " + self.host["token"]}
        self.f = {"Authorization": "Bearer " + self.friend["token"]}
        created = self.client.post("/api/rooms", headers=self.h, json={}).json()
        self.room = created["room_id"]
        self.host_seat = created["self"]["id"]
        self.friend_seat = next(seat for seat in range(1, 7) if seat != self.host_seat)
        self.base = f"/api/rooms/{self.room}"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.folder.cleanup()

    def test_lock_password_kick_reopen_and_private_snapshot(self):
        self.assertEqual(self.client.post(self.base + "/lock", headers=self.h, json={"locked": True}).status_code, 200)
        self.assertEqual(
            self.client.post(self.base + "/join", headers=self.f, json={"seat": self.friend_seat}).status_code, 403
        )
        self.client.post(self.base + "/lock", headers=self.h, json={"locked": False})
        self.client.post(self.base + "/password", headers=self.h, json={"password": "secret-room-password"})
        wrong = self.client.post(
            self.base + "/join", headers=self.f, json={"seat": self.friend_seat, "password": "wrong"}
        )
        self.assertEqual(wrong.status_code, 403)
        joined = self.client.post(
            self.base + "/join", headers=self.f, json={"seat": self.friend_seat, "password": "secret-room-password"}
        )
        self.assertEqual(joined.status_code, 200)
        self.assertNotIn("secret-room-password", joined.text)
        self.assertNotIn("password_hash", joined.text)
        self.assertEqual(self.client.post(self.base + "/kick", headers=self.f, json={"seat": 1}).status_code, 403)
        self.assertEqual(
            self.client.post(
                self.base + "/kick", headers=self.h, json={"seat": self.friend_seat, "action_id": "kick-uuid-23"}
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get(self.base, headers=self.f).status_code, 403)
        self.assertEqual(
            self.client.post(
                self.base + "/join", headers=self.f, json={"seat": self.friend_seat, "password": "secret-room-password"}
            ).status_code,
            403,
        )
        self.client.post(self.base + "/reopen", headers=self.h, json={"seat": self.friend_seat})
        self.assertEqual(
            self.client.post(
                self.base + "/join", headers=self.f, json={"seat": self.friend_seat, "password": "secret-room-password"}
            ).status_code,
            200,
        )

    def test_recovery_keeps_seat_invalidates_old_token_and_code(self):
        recovered = self.client.post("/api/session/recover", json={"code": self.host["recovery_code"]}).json()
        self.assertEqual(recovered["owner_id"], self.host["owner_id"])
        self.assertEqual(self.client.get(self.base, headers=self.h).status_code, 401)
        new_headers = {"Authorization": "Bearer " + recovered["token"]}
        restored = self.client.get(self.base, headers=new_headers)
        self.assertEqual(restored.json()["self"]["id"], self.host_seat)
        self.assertTrue(restored.json()["is_host"])
        self.assertEqual(
            self.client.post("/api/session/recover", json={"code": self.host["recovery_code"]}).status_code, 400
        )
        self.assertEqual(self.client.post("/api/session/revoke", headers=new_headers).status_code, 200)
        self.assertEqual(self.client.get(self.base, headers=new_headers).status_code, 401)

    def test_security_headers_and_room_session_rates(self):
        page = self.client.get("/")
        for name in ["content-security-policy", "x-content-type-options", "referrer-policy", "permissions-policy"]:
            self.assertIn(name, page.headers)
        self.assertEqual(self.client.get(self.base, headers=self.h).headers["cache-control"], "no-store")
        app.state.manager.limits.config["join_per_minute"] = 1
        self.assertEqual(
            self.client.post(self.base + "/join", headers=self.f, json={"seat": self.friend_seat}).status_code, 200
        )
        stranger = self.client.post("/api/session").json()
        blocked = self.client.post(
            self.base + "/join", headers={"Authorization": "Bearer " + stranger["token"]}, json={"seat": 3}
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)

    def test_websocket_resnapshot_cursor_and_revoke_disconnect(self):
        with self.client.websocket_connect("/ws/" + self.room) as ws:
            ws.send_json({"token": self.host["token"], "last_event_id": "older-event"})
            snap = ws.receive_json()
            self.assertEqual(snap["type"], "state_snapshot")
            self.assertEqual(snap["sync"]["mode"], "authoritative_snapshot")
            self.assertEqual(snap["sync"]["last_event_id"], "older-event")
            self.client.post("/api/session/revoke", headers=self.h)
            from starlette.websockets import WebSocketDisconnect

            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()

    def test_cost_details_unavailable_while_roles_hidden(self):
        self.client.post(self.base + "/start", headers=self.h, json={})
        report = self.client.get(self.base + "/cost-report", headers=self.h)
        self.assertFalse(report.json()["details_available"])
        self.assertNotIn("records", report.json())
        self.client.post(self.base + "/close", headers=self.h, json={})
        closed = self.client.get(self.base + "/cost-report", headers=self.h)
        # Closed unfinished games keep identities hidden, including model call timing.
        self.assertFalse(closed.json()["details_available"])


if __name__ == "__main__":
    unittest.main()
