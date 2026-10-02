"""Owner-bound favorites, lineups, actual call status and historical access."""

import asyncio
import unittest

import httpx

from app.llm import LLMRouter
from app.model_preferences import call_status
from app.persistence import Store
from tests import test_v3_api as api_helpers
from tests.test_v3_rules import fixture


class PreferenceTests(unittest.TestCase):
    def test_preferences_survive_recovery_and_are_isolated(self):
        with api_helpers.application() as (client, _):
            helper = api_helpers.V3APIIntegrationTests()
            session, auth = helper.session(client)
            _, outsider = helper.session(client)
            for count in (6, 9, 12):
                lineup = {
                    "id": f"lineup{count}",
                    "name": f"{count}人常用",
                    "player_count": count,
                    "seats": [{"id": 2, "model_key": "mock:rule-based-mock", "personality": "detective"}],
                }
                result = client.put(
                    "/api/models/preferences",
                    headers=auth,
                    json={"favorites": ["mock:rule-based-mock"], "lineups": [lineup]},
                )
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["lineups"][0]["player_count"], count)
            self.assertEqual(client.get("/api/models/preferences", headers=outsider).json()["favorites"], [])
            recovered = client.post("/api/session/recover", json={"code": session["recovery_code"]})
            self.assertEqual(recovered.status_code, 200, recovered.text)
            auth = {"Authorization": "Bearer " + recovered.json()["token"]}
            self.assertEqual(
                client.get("/api/models/preferences", headers=auth).json()["favorites"], ["mock:rule-based-mock"]
            )
            self.assertEqual(client.get("/api/models/preferences").status_code, 401)

    def test_secret_fields_invalid_targets_and_oversized_preferences_rejected(self):
        store = Store(":memory:")
        for body in (
            {"api_key": "secret"},
            {"favorites": ["bad value"]},
            {"favorites": ["openai:a"] * 201},
            {
                "lineups": [
                    {"id": "bad", "name": "bad", "player_count": 6, "seats": [{"id": 7, "model_key": "openai:a"}]}
                ]
            },
        ):
            with self.assertRaises(ValueError):
                store.save_model_preferences("owner", body)
        self.assertEqual(store.model_preferences("owner")["favorites"], [])
        store.close()

    def test_recent_calls_come_from_telemetry_and_purge_cannot_resurrect_private_output(self):
        store = Store(":memory:")
        g, _ = fixture()
        store.save(g)
        p = g.player(2)
        store.record_model(
            "call",
            {
                "room_id": g.room_id,
                "game_id": g.game_id,
                "agent_id": p.agent_id,
                "model_key": "openai:actual",
                "model": "actual",
                "success": True,
            },
        )
        recent = store.model_preferences("host")["recent"]
        self.assertEqual(recent[0]["status"], "success")
        self.assertEqual(recent[0]["key"], "openai:actual")
        self.assertGreater(recent[0]["checked_at"], 0)
        store.record_model("call", {"room_id": g.room_id, "model_key": "openai:cancel", "failure_reason": "cancelled"})
        self.assertEqual(len(store.model_preferences("host")["recent"]), 1)
        store.delete_room(g.room_id)
        store.record_model("call", {"room_id": g.room_id, "speech": "late private output"})
        self.assertEqual(store.model_report(g.room_id, g.game_id), {"calls": [], "outcomes": []})
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM model_telemetry").fetchone()[0], 0)
        store.close()

    def test_provider_error_status_never_exposes_error_body(self):
        for status, code, expected in (
            (403, "denied", "no_permission"),
            (429, "rate_limit", "rate_limited"),
            (429, "insufficient_quota", "quota_exhausted"),
            (404, "missing", "not_found"),
        ):
            response = httpx.Response(
                status,
                request=httpx.Request("POST", "https://example.invalid"),
                json={"error": {"code": code, "message": "secret-provider-text"}},
            )
            reason = LLMRouter._reason(httpx.HTTPStatusError("failed", request=response.request, response=response))
            self.assertNotIn("secret-provider-text", reason)
            self.assertEqual(call_status(False, reason), expected)

    def test_history_only_returns_owned_completed_games_and_remains_after_rematch(self):
        with api_helpers.application() as (client, _):
            helper = api_helpers.V3APIIntegrationTests()
            helper.stop_clock(client)
            _, auth = helper.session(client)
            _, other = helper.session(client)
            s = helper.post(client, "/api/rooms", auth)
            rid = s["room_id"]
            helper.post(client, f"/api/rooms/{rid}/start", auth)
            helper.prepare(client, rid, lambda room: room.engine.finish("good"))
            games = client.get("/api/history", headers=auth).json()["games"]
            self.assertEqual(len(games), 1)
            gid = games[0]["game_id"]
            self.assertIn("models", games[0])
            self.assertIn("role", games[0])
            self.assertEqual(client.get("/api/history", headers=other).json()["games"], [])
            path = f"/api/history/{rid}/{gid}"
            self.assertEqual(client.get(path, headers=other).status_code, 403)
            helper.post(client, f"/api/rooms/{rid}/rematch", auth)
            self.assertEqual(len(client.get(path, headers=auth).json()["players"]), 6)
            self.assertEqual(client.delete(f"/api/rooms/{rid}", headers=auth).status_code, 200)
            self.assertEqual(client.get(path, headers=auth).status_code, 404)
            self.assertEqual(client.get("/api/history", headers=auth).json()["games"], [])


class StreamTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_generation_metrics_and_usage_do_not_wait_for_presentation(self):
        import os
        from unittest.mock import patch

        from tests.test_model_routing import ENV

        with patch.dict(os.environ, ENV, clear=True):
            router = LLMRouter()

            async def native(provider, system, user):
                yield "第一句。"
                await asyncio.sleep(0.012)
                router._capture_usage({"usage": {"prompt_tokens": 20, "completion_tokens": 30}}, provider)
                yield "真实完整生成，统计不经过界面缓冲。"

            router._native_speech = native
            text = "".join([part async for part in router.speech_stream("dashscope:alpha", "s", "u", {})])
            call = router.records[-1]
            self.assertEqual(call["output_characters"], len(text))
            self.assertLess(call["first_token_ms"], call["generation_ms"])
            self.assertEqual(call["output_tokens"], 30)
            self.assertEqual(call["model"], "alpha")
            self.assertTrue(call["success"])
