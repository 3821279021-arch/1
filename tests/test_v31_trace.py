"""Research artifacts retain private events without exposing secrets publicly."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema
from fastapi.testclient import TestClient

from app.game import WerewolfGame
from app.llm import LLMResult, LLMRouter
from app.main import app
from app.persistence import Store
from app.trace import TraceWriter, redact

ROOT = Path(__file__).resolve().parents[1]


class TraceTests(unittest.TestCase):
    def test_recursive_redaction_and_hidden_reasoning(self):
        value = {
            "api_key": "arbitrary-secret",
            "known-secret": "dictionary key must also be redacted",
            "Authorization": "Bearer token",
            "nested": {
                "session_secret": "session",
                "recovery_code": "recover",
                "reasoning_content": "hidden",
                "text": "known-secret sk-secret-key-1234 Bearer arbitrary",
            },
            "parameters": {"reasoning_effort": "low"},
        }
        clean = json.dumps(redact(value, ["known-secret"]))
        for secret in (
            "arbitrary-secret",
            "session_secret",
            "recovery_code",
            "hidden",
            "known-secret",
            "sk-secret-key-1234",
            "Bearer arbitrary",
        ):
            self.assertNotIn(secret, clean)
        self.assertIn("reasoning_effort", clean)

    def test_trace_schema_and_stream_flush(self):
        schema = json.loads((ROOT / "schemas/game-trace-event.schema.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "game.jsonl"
            game = WerewolfGame("r", "h", experiment_id="exp", game_seed=19)
            writer = TraceWriter(path, game, secrets=["provider-secret"])
            writer.write("model_output", {"output": "provider-secret", "api_key": "sk-api-key-test"})
            row = json.loads(path.read_text())
            jsonschema.validate(row, schema)
            self.assertEqual(row["game_seed"], 19)
            self.assertNotIn("provider-secret", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            writer.close()

    def test_original_host_only_export_and_public_replay_boundary(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(
                os.environ,
                {
                    "DATABASE_PATH": folder + "/export.sqlite",
                    "RATE_SESSION_PER_MINUTE": "0",
                    "RATE_ROOM_PER_HOUR": "0",
                    "GAME_TIME_SCALE": "1",
                    "AI_CHUNK_DELAY": "0",
                    "AI_TURN_PAUSE": "0",
                },
            ),
            TestClient(app) as client,
        ):
            host = client.post("/api/session").json()
            stranger = client.post("/api/session").json()
            headers = {"Authorization": "Bearer " + host["token"]}
            room = client.post("/api/rooms", headers=headers, json={}).json()
            rid = room["room_id"]
            manager = app.state.manager
            runtime = manager.get(rid)
            game = runtime.game
            runtime.engine.start(host["owner_id"], now=100)
            runtime.engine.emit("private_marker", {"text": "PRIVATE_ROLE_CHECK", "api_key": "forbidden"}, "player", 1)
            runtime.engine.finish("good", now=101)
            manager.store.save(game, runtime.engine.outbox)
            route = f"/api/rooms/{rid}/games/{game.game_id}/export"
            self.assertEqual(client.get(route).status_code, 401)
            self.assertEqual(
                client.get(route, headers={"Authorization": "Bearer " + stranger["token"]}).status_code, 403
            )
            response = client.get(route, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertIn("PRIVATE_ROLE_CHECK", response.text)
            self.assertNotIn("forbidden", response.text)
            self.assertNotIn(host["token"], response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            public = manager.store.public_replay(rid, game.game_id)
            self.assertNotIn("PRIVATE_ROLE_CHECK", json.dumps(public))
            self.assertEqual(client.get(f"/api/rooms/{rid}/games/missing/export", headers=headers).status_code, 404)

    def test_legacy_database_additive_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "old.sqlite")
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE identities(token_hash TEXT PRIMARY KEY,owner_id TEXT UNIQUE NOT NULL,created_at REAL NOT NULL)"
            )
            db.execute("INSERT INTO identities VALUES('old-token','old-owner',0)")
            db.commit()
            db.close()
            store = Store(path)
            self.assertEqual(store.db.execute("SELECT owner_id FROM identities").fetchone()[0], "old-owner")
            tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master")}
            self.assertIn("experiment_games", tables)
            self.assertIn("experiments", tables)
            store.close()


class ModelTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_parameters_raw_output_and_invalid_attempts_are_recorded(self):
        with tempfile.TemporaryDirectory() as folder:
            game = WerewolfGame("model-trace", "host", experiment_id="trace", game_seed=9)
            path = Path(folder) / "model.jsonl"
            writer = TraceWriter(path, game, secrets=["provider-secret-value"])
            router = LLMRouter()
            router.retries = 0
            router.registry.register("openai", "trace-test", configured=True)
            router.trace_sink = lambda kind, payload: writer.write(kind, payload, "research_private")

            async def invalid(system, user):
                return LLMResult(
                    {"target": 99},
                    "openai",
                    "trace-test",
                    '{"target":99,"reasoning_content":"hidden-chain","text":"provider-secret-value"}',
                )

            router.adapters["openai"].generate = invalid
            with router.request_scope(
                room_id=game.room_id,
                game_id=game.game_id,
                agent_id="actor",
                model_parameters={"temperature": 0.3, "top_p": 0.9},
            ):
                result = await router.ask_json(
                    "openai:trace-test",
                    "rules",
                    "data",
                    mock_context={"action": "vote", "options": [2]},
                    validator=lambda data: data.get("target") == 2,
                )
            await router.close()
            writer.close()
            self.assertEqual(result.routing["status"], "mock_fallback")
            self.assertTrue(router.records[0]["invalid_output"])
            self.assertEqual(router.records[0]["model_parameters"], {"temperature": 0.3, "top_p": 0.9})
            content = path.read_text()
            self.assertNotIn("provider-secret-value", content)
            self.assertNotIn("hidden-chain", content)
            rows = [json.loads(line) for line in content.splitlines()]
            self.assertIn("model_prompt", {row["type"] for row in rows})
            self.assertIn("model_output", {row["type"] for row in rows})
