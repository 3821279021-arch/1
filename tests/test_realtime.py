import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.ai import memory_from
from app.llm import LLMRouter, extract_json
from app.main import app
from app.persistence import Store
from app.rooms import RoomManager
from app.scope import InformationScope


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(
            os.environ, {"AI_CHUNK_DELAY": "0.001", "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": ""}
        )
        self.env.start()
        self.store = Store(":memory:")
        self.manager = RoomManager(self.store, LLMRouter(), time_scale=0.007, ai_pause=0.001)

    async def asyncTearDown(self):
        await self.manager.close()
        self.store.close()
        self.env.stop()

    async def create(self):
        s = await self.manager.create("owner", "真人", 1, "测试", "fast")
        await self.manager.command(s["room_id"], "owner", "pet_config", {"control_mode": "autopilot"})
        await self.manager.command(s["room_id"], "owner", "start", {})
        return self.manager.require(s["room_id"], "owner")

    async def test_full_mock_game_no_browser_driving(self):
        room = await self.create()
        # Keep the opening from randomly ending before the speech/vote checks.
        for p, role in zip(room.game.players, ["witch", "wolf", "wolf", "villager", "villager", "seer"]):
            p.role = role
            p.role_state = {"antidote": True, "poison": False} if role == "witch" else {}
        room.game.witch_poison = False
        connection = await self.manager.subscribe(room.game.room_id, "owner")
        self.manager.runner = asyncio.create_task(self.manager.run())
        packets = []
        limit = time.monotonic() + 20
        while not room.game.game_over and time.monotonic() < limit:
            try:
                packet = await asyncio.wait_for(connection.queue.get(), 0.2)
                packets.append(packet)
            except asyncio.TimeoutError:
                pass
        while not connection.queue.empty():
            packets.append(connection.queue.get_nowait())
        self.assertTrue(room.game.game_over)
        self.assertIn(room.game.winner, {"wolves", "good", "draw"})
        kinds = [p["type"] for p in packets]
        self.assertIn("speech_chunk", kinds)
        self.assertIn("speech_finished", kinds)
        self.assertIn("vote_result", kinds)
        turns = {
            p["data"]["current_turn_player_id"]
            for p in packets
            if p["type"] == "state_snapshot" and p["data"]["phase"] == "day_speech"
        }
        self.assertGreater(len(turns), 1)
        self.assertTrue(any(e["kind"] == "vote" for e in room.game.events))

    async def test_pet_chat_does_not_hold_game_clock(self):
        room = await self.create()
        room.engine.enter("day_speech", 2)
        room.game.speech_queue = [3, 4, 5, 6, 1]
        original = self.manager.ai.pet_reply
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(*args):
            started.set()
            await release.wait()
            return await original(*args)

        self.manager.ai.pet_reply = slow
        chat = asyncio.create_task(self.manager.pet_chat(room.game.room_id, "owner", "给我发言草稿"))
        await started.wait()
        sequence = room.game.turn_sequence
        self.manager.runner = asyncio.create_task(self.manager.run())
        limit = time.monotonic() + 2
        while room.game.turn_sequence == sequence and time.monotonic() < limit:
            await asyncio.sleep(0.01)
        self.assertGreater(room.game.turn_sequence, sequence)
        release.set()
        result = await chat
        self.assertIn("草稿", result["pet"]["private_chat_history"][-1]["text"])

    async def test_reconnect_snapshot_original_deadline(self):
        room = await self.create()
        deadline = room.game.turn_deadline
        c1 = await self.manager.subscribe(room.game.room_id, "owner")
        s1 = (await c1.queue.get())["data"]
        room.connections.discard(c1)
        c2 = await self.manager.subscribe(room.game.room_id, "owner")
        s2 = (await c2.queue.get())["data"]
        self.assertEqual(s1["turn_deadline"], deadline)
        self.assertEqual(s2["turn_deadline"], deadline)
        self.assertEqual(s1["turn_sequence"], s2["turn_sequence"])

    async def test_cancel_inflight_pet_control(self):
        room = await self.create()
        room.engine.enter("day_speech", 1)
        room.game.speech_queue = [2, 3, 4, 5, 6]
        begun = asyncio.Event()
        release = asyncio.Event()

        async def blocked(*args):
            begun.set()
            await release.wait()
            yield "不应发出来"

        self.manager.ai.speak = blocked
        self.manager.launch_jobs(room)
        await begun.wait()
        await self.manager.command(room.game.room_id, "owner", "pet_config", {"control_mode": "copilot"})
        release.set()
        await asyncio.sleep(0.01)
        self.assertEqual(room.game.current_speech, "")
        self.assertIsNotNone(room.engine.action_for(1))
        self.assertEqual(room.game.current_turn_player_id, 1)

    async def test_one_shot_authorization_consumed(self):
        room = await self.create()
        room.engine.configure_pet("owner", {"control_mode": "copilot"})
        room.engine.enter("day_speech", 1)
        room.game.speech_queue = [2, 3, 4, 5, 6]
        room.engine.configure_pet("owner", {"delegate_next": True})
        task = asyncio.create_task(self.manager.actor(room, 1, room.game.turn_sequence))
        await task
        self.assertFalse(room.game.pets["owner"].delegate_next)
        self.assertEqual(room.game.current_turn_player_id, 2)
        self.assertFalse(self.manager.can_control(room, 1))

    async def test_malformed_model_action_becomes_legal(self):
        room = await self.create()
        room.engine.enter("day_vote")

        async def bad(*args, **kwargs):
            from app.llm import LLMResult

            return LLMResult({"target": 999}, "broken", "model")

        self.manager.router.ask_json = bad
        view = InformationScope.ai_view(room.game, 1)
        proposal = await self.manager.ai.propose(view, "openai", "detective", memory_from(view))
        room.engine.apply(1, proposal)
        self.assertIn(room.game.votes["1"], [None, *view["pending_action"]["options"]])
        self.assertEqual(self.manager.ai.decision_records[-1]["source"], "rule_fallback")


class APITests(unittest.TestCase):
    def test_multiplayer_auth_ws_and_restart(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"DATABASE_PATH": folder + "/rooms.sqlite", "GAME_TIME_SCALE": "1"}),
        ):
            with TestClient(app) as client:
                a = client.post("/api/session").json()
                b = client.post("/api/session").json()
                c = client.post("/api/session").json()
                ha = {"Authorization": "Bearer " + a["token"]}
                hb = {"Authorization": "Bearer " + b["token"]}
                hc = {"Authorization": "Bearer " + c["token"]}
                room = client.post("/api/rooms", headers=ha, json={"name": "甲", "seat": 1}).json()
                rid = room["room_id"]
                self.assertEqual(client.get(f"/api/rooms/{rid}", headers=hb).status_code, 403)
                joined = client.post(f"/api/rooms/{rid}/join", headers=hb, json={"name": "乙", "seat": 2})
                self.assertEqual(joined.status_code, 200)
                self.assertEqual(client.post(f"/api/rooms/{rid}/start", headers=hb).status_code, 400)
                self.assertEqual(client.get("/api/rooms").status_code, 401)
                self.assertEqual(client.get("/api/rooms", headers=hc).json(), [])
                with client.websocket_connect(f"/ws/{rid}") as ws:
                    ws.send_json({"token": a["token"]})
                    self.assertEqual(ws.receive_json()["type"], "state_snapshot")
                    client.post(f"/api/rooms/{rid}/start", headers=ha)
                    events = []
                    for _ in range(5):
                        packet = ws.receive_json()
                        events.append(packet)
                        if packet["type"] == "state_snapshot":
                            break
                    self.assertIn("phase_changed", [p["type"] for p in events])
                s = client.get(f"/api/rooms/{rid}", headers=ha).json()
                self.assertEqual(len(s["players"]), 6)
                self.assertFalse(any(p["role"] for p in s["players"] if not p["is_you"]))
                self.assertNotIn("owner_id", str(s))
                self.assertEqual(client.post("/api/game/reveal").status_code, 403)
            with TestClient(app) as client:
                restored = client.get(f"/api/rooms/{rid}", headers=ha).json()
                self.assertEqual(restored["game_id"], s["game_id"])
                self.assertEqual(restored["turn_deadline"], s["turn_deadline"])

    def test_json_extraction(self):
        for text in ["", "[]", "not json"]:
            self.assertEqual(extract_json(text), {})
        self.assertEqual(extract_json('```json\n{"target": 3}\n```'), {"target": 3})


if __name__ == "__main__":
    unittest.main()
