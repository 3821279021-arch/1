"""HTTP limits, safe status, strict start and command replay contracts."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class APIUpgradeTests(unittest.TestCase):
    def environment(self, folder, **extra):
        return patch.dict(
            os.environ,
            {
                "DATABASE_PATH": folder + "/api.sqlite",
                "GAME_TIME_SCALE": "1",
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "RATE_SESSION_PER_MINUTE": "2",
                "RATE_ROOM_PER_HOUR": "1",
                **extra,
            },
            clear=True,
        )

    def test_http_rates_and_create_replay_without_extra_quota(self):
        with tempfile.TemporaryDirectory() as folder, self.environment(folder), TestClient(app) as c:
            session = c.post("/api/session").json()
            headers = {"Authorization": "Bearer " + session["token"]}
            self.assertEqual(c.post("/api/session").status_code, 200)
            limited = c.post("/api/session")
            self.assertEqual(limited.status_code, 429)
            self.assertIn("Retry-After", limited.headers)
            body = {"action_id": "stable-create-uuid", "name": "甲"}
            first = c.post("/api/rooms", headers=headers, json=body)
            replay = c.post("/api/rooms", headers=headers, json=body)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["room_id"], replay.json()["room_id"])
            self.assertEqual(c.post("/api/rooms", headers=headers, json={"name": "再建"}).status_code, 429)

    def test_strict_independent_models_insufficient_never_starts_or_calls(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            self.environment(folder, DASHSCOPE_API_KEY="sentinel-test-key", DASHSCOPE_MODELS="qwen-plus,qwen-turbo"),
            TestClient(app) as c,
        ):
            session = c.post("/api/session").json()
            headers = {"Authorization": "Bearer " + session["token"]}
            rid = c.post("/api/rooms", headers=headers, json={"seat": 1}).json()["room_id"]
            result = c.post(
                f"/api/rooms/{rid}/configure",
                headers=headers,
                json={"seats": [{"id": pid, "provider": "auto"} for pid in range(2, 7)]},
            )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(len(result.json()["players"]), 1)
            blocked = c.post(f"/api/rooms/{rid}/start", headers=headers, json={"action_id": "start-strict-uuid"})
            self.assertEqual(blocked.status_code, 400)
            self.assertIn("独立模型不足", blocked.json()["detail"])
            current = c.get(f"/api/rooms/{rid}", headers=headers).json()
            self.assertEqual(current["phase"], "lobby")
            self.assertTrue(current["unique_model_per_ai_seat"])
            self.assertEqual(app.state.manager.router.activity["dashscope"]["requests"], 0)
            health = c.get("/api/health").json()
            self.assertNotIn("sentinel-test-key", json.dumps([health, current]))
            self.assertTrue(health["providers"]["dashscope"]["configured"])
            self.assertFalse(health["providers"]["openai"]["configured"])

    def test_close_replay_and_no_pending_action_after_close(self):
        with tempfile.TemporaryDirectory() as folder, self.environment(folder), TestClient(app) as c:
            session = c.post("/api/session").json()
            headers = {"Authorization": "Bearer " + session["token"]}
            rid = c.post("/api/rooms", headers=headers, json={}).json()["room_id"]
            self.assertEqual(c.post(f"/api/rooms/{rid}/start", headers=headers).status_code, 200)
            first = c.post(f"/api/rooms/{rid}/close", headers=headers, json={"action_id": "close-command-uuid"}).json()
            second = c.post(f"/api/rooms/{rid}/close", headers=headers, json={"action_id": "close-command-uuid"}).json()
            self.assertEqual(first["lifecycle"], "ARCHIVED")
            self.assertEqual(first["state_revision"], second["state_revision"])
            self.assertTrue(second["action_ack"]["replayed"])
            self.assertIsNone(second["pending_action"])
            self.assertEqual(c.get("/api/rooms", headers=headers).json(), [])
