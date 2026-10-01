"""Regression checks for lobby, scoped delivery, streaming and room lifecycle."""

import asyncio
import json
import os
import time
import unittest
from unittest.mock import patch

from app.limits import RateLimitError
from app.llm import LLMRouter
from app.persistence import Store
from app.rooms import RoomManager


def drain(connection):
    packets = []
    while not connection.queue.empty():
        packets.append(connection.queue.get_nowait())
    return packets


class CoreV21Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "AI_CHUNK_DELAY": "0",
                "RATE_ROOM_PER_HOUR": "100",
                "MAX_ACTIVE_ROOMS": "100",
                "AI_GAME_MAX_REQUESTS": "300",
                "AI_GAME_TOKEN_BUDGET": "250000",
            },
        )
        self.environment.start()
        self.store = Store(":memory:")
        self.manager = RoomManager(self.store, LLMRouter(), time_scale=10, ai_pause=0)
        self.managers = [self.manager]

    async def asyncTearDown(self):
        for manager in reversed(self.managers):
            await manager.close()
        self.store.close()
        self.environment.stop()

    async def lobby(self):
        view = await self.manager.create("host", "房主", 1, "新版回归", "fast")
        return self.manager.require(view["room_id"], "host")

    async def active(self, second_human=False):
        room = await self.lobby()
        if second_human:
            await self.manager.join(room.game.room_id, "friend", "朋友", 2)
        await self.manager.command(room.game.room_id, "host", "start", {})
        for player, role in zip(room.game.players, ["wolf", "wolf", "seer", "witch", "villager", "villager"]):
            player.role = role
        self.manager.commit(room)
        return room

    async def test_presets_allow_humans_then_fill_only_remaining_seats(self):
        room = await self.lobby()
        presets = [{"id": seat, "provider": "mock", "personality": "cautious"} for seat in range(2, 7)]
        await self.manager.command(room.game.room_id, "host", "configure", {"pace": "fast", "seats": presets})
        self.assertEqual([p.id for p in room.game.players], [1])
        self.assertEqual(set(room.game.seat_presets), {"2", "3", "4", "5", "6"})
        await self.manager.join(room.game.room_id, "second", "二号真人", 2)
        await self.manager.join(room.game.room_id, "third", "三号真人", 3)
        await self.manager.command(room.game.room_id, "host", "start", {})
        self.assertEqual([p.id for p in room.game.players], list(range(1, 7)))
        self.assertEqual([p.id for p in room.game.players if not p.owner_id], [4, 5, 6])
        for identity, seat in (("second", 2), ("third", 3)):
            player = room.game.player(seat)
            self.assertEqual(player.owner_id, identity)
            self.assertEqual(player.provider, "mock")
            self.assertNotEqual(player.agent_id, room.game.pets[identity].agent_id)
        self.assertEqual(len({p.agent_id for p in room.game.players}), 6)

    async def test_leaving_lobby_restores_the_ai_seat_preset(self):
        room = await self.lobby()
        await self.manager.command(
            room.game.room_id,
            "host",
            "configure",
            {
                "pace": "fast",
                "seats": [{"id": 2, "provider": "mock", "personality": "commander"}],
            },
        )
        await self.manager.join(room.game.room_id, "friend", "离座真人", 2)
        old_agent_id = room.game.player(2).agent_id
        room.game.player(2).private_notes.append("旧玩家私有查验")
        room.game.pets["friend"].private_chat_history.append({"text": "旧玩家私聊"})
        result = await self.manager.command(room.game.room_id, "friend", "leave", {})
        self.assertTrue(result["left"])
        self.assertNotIn("friend", room.game.pets)
        await self.manager.command(room.game.room_id, "host", "start", {})
        player = room.game.player(2)
        self.assertIsNone(player.owner_id)
        self.assertEqual(player.personality, "commander")
        self.assertNotEqual(player.agent_id, old_agent_id)
        self.assertNotIn("旧玩家", json.dumps(player.memory, ensure_ascii=False))
        self.assertEqual(player.private_notes, [])

    async def test_leave_with_live_connection_and_replay_keeps_room_start_and_clock_working(self):
        room = await self.lobby()
        await self.manager.join(room.game.room_id, "friend", "离座真人", 2)
        host = await self.manager.subscribe(room.game.room_id, "host")
        leaving = await self.manager.subscribe(room.game.room_id, "friend")
        drain(host)
        drain(leaving)
        payload = {
            "action_id": "leave-uuid",
            "game_id": room.game.game_id,
            "expected_state_revision": room.game.state_revision,
        }
        first = await self.manager.command(room.game.room_id, "friend", "leave", payload)
        self.assertTrue(first["left"])
        self.assertNotIn(leaving, room.connections)
        self.assertIn(host, room.connections)
        self.assertIn("room_left", [packet["type"] for packet in drain(leaving)])
        snapshots = [packet["data"] for packet in drain(host) if packet["type"] == "state_snapshot"]
        self.assertEqual([player["id"] for player in snapshots[-1]["players"]], [1])
        revision = room.game.state_revision
        second = await self.manager.command(room.game.room_id, "friend", "leave", payload)
        self.assertTrue(second["action_ack"]["replayed"])
        self.assertEqual(room.game.state_revision, revision)
        with self.assertRaises(PermissionError):
            await self.manager.subscribe(room.game.room_id, "friend")
        started = await self.manager.command(room.game.room_id, "host", "start", {})
        self.assertEqual(started["lifecycle"], "ACTIVE")
        self.assertIsNone(room.game.player(2).owner_id)
        sequence = room.game.turn_sequence
        self.assertTrue(room.engine.tick(room.game.turn_deadline))
        self.manager.commit(room)
        self.assertGreater(room.game.turn_sequence, sequence)
        self.assertIn("phase_changed", [packet["type"] for packet in drain(host)])
        self.assertEqual(drain(leaving), [])
        after_start = await self.manager.command(room.game.room_id, "friend", "leave", payload)
        self.assertTrue(after_start["left"])
        self.assertTrue(after_start["action_ack"]["replayed"])

    async def test_dead_wolf_cannot_receive_new_private_events_or_reconnect_history(self):
        room = await self.active(second_human=True)
        living = await self.manager.subscribe(room.game.room_id, "friend")
        dead = await self.manager.subscribe(room.game.room_id, "host")
        drain(living)
        drain(dead)
        room.engine.wolf_message(2, "死亡前已经见过的狼队计划")
        self.manager.commit(room)
        self.assertIn("wolf_chat_message", [p["type"] for p in drain(dead)])
        drain(living)
        room.engine.kill_player(1)
        room.engine.wolf_message(2, "死亡后绝不能收到的新狼队计划")
        self.manager.commit(room)
        dead_packets = drain(dead)
        self.assertNotIn("wolf_chat_message", [p["type"] for p in dead_packets])
        self.assertNotIn("死亡后绝不能收到", json.dumps(dead_packets, ensure_ascii=False))
        self.assertIn("死亡后绝不能收到", json.dumps(drain(living), ensure_ascii=False))
        with self.assertRaises(ValueError):
            room.engine.wolf_message(1, "死亡狼人发送消息")
        room.engine.enter("night_wolves")
        action = room.engine.action_for(2)
        room.engine.apply(
            2,
            {
                "action": action["type"],
                "game_id": room.game.game_id,
                "turn_sequence": room.game.turn_sequence,
                "target": 4,
            },
        )
        room.engine.kill_player(2)
        self.manager.commit(room)
        view = self.manager.snapshot(room, "host")
        self.assertEqual(view["wolf_teammates"], [{"id": 2, "alive": True}])
        self.assertEqual(len(view["wolf_chat"]), 1)
        self.assertIn("死亡前已经见过", view["wolf_chat"][0]["text"])
        self.assertNotIn("player_action", [p["type"] for p in drain(dead)])
        room.connections.discard(dead)
        reconnect = await self.manager.subscribe(room.game.room_id, "host")
        snapshot = (await reconnect.queue.get())["data"]
        self.assertNotIn("死亡后绝不能收到", json.dumps(snapshot, ensure_ascii=False))
        self.assertEqual(snapshot["wolf_teammates"], [{"id": 2, "alive": True}])

    async def test_nonwolf_never_receives_wolf_chat_or_other_pet_events(self):
        room = await self.active(second_human=True)
        room.game.player(1).role = "villager"
        connection = await self.manager.subscribe(room.game.room_id, "host")
        drain(connection)
        room.engine.wolf_message(2, "绝密狼队信息")
        room.engine.pet_message("friend", "assistant", "别人的私有搭档建议")
        self.manager.commit(room)
        packets = drain(connection)
        self.assertNotIn("wolf_chat_message", [p["type"] for p in packets])
        self.assertNotIn("private_pet_message", [p["type"] for p in packets])
        encoded = json.dumps(packets, ensure_ascii=False)
        self.assertNotIn("绝密狼队信息", encoded)
        self.assertNotIn("别人的私有搭档建议", encoded)

    async def test_night_model_execution_does_not_reveal_hidden_actor(self):
        room = await self.active()
        room.game.player(1).role = "villager"
        connection = await self.manager.subscribe(room.game.room_id, "host")
        drain(connection)
        status = {"provider_used": "dashscope", "model_used": "night-only-model-status", "status": "success"}
        with patch.object(self.manager.router, "execution", return_value=status):
            for player_id, category in ((3, "seer_inspect"), (4, "witch"), (2, "wolf_discussion")):
                self.manager.execution(room, player_id, room.game.player(player_id).agent_id, category)
            self.manager.commit(room)
        packets = drain(connection)
        self.assertNotIn("model_execution", [p["type"] for p in packets])
        self.assertNotIn("night-only-model-status", json.dumps(packets, ensure_ascii=False))
        snapshot = self.manager.snapshot(room, "host")
        for seat in (2, 3, 4):
            self.assertEqual(next(p for p in snapshot["players"] if p["id"] == seat)["execution_status"], {})
            self.assertEqual(room.game.player(seat).conversation_state["last_execution"], status)

    async def test_wolf_discussion_is_two_rounds_with_legal_sequential_context(self):
        room = await self.active()
        room.game.player(1).role = "villager"
        room.game.player(3).role = "wolf"
        room.game.player(4).private_notes.append("OTHER_WITCH_PRIVATE_RESULT")
        room.game.player(4).memory["private_marker"] = "OTHER_PRIVATE_AGENT_MEMORY"
        calls = []

        async def discussion(view, *args):
            calls.append(view)
            return f"狼{view['self']['id']}号第{len(calls)}次计划"

        self.manager.ai.wolf_discuss = discussion
        await self.manager.discussion_rounds(room, room.game.turn_sequence)
        self.assertEqual([view["self"]["id"] for view in calls], [2, 3, 2, 3])
        self.assertEqual([len(view["wolf_chat"]) for view in calls], [0, 1, 2, 3])
        self.assertEqual(calls[1]["wolf_chat"][-1]["text"], "狼2号第1次计划")
        self.assertEqual(calls[2]["wolf_chat"][-1]["text"], "狼3号第2次计划")
        for view in calls:
            self.assertNotIn("OTHER_WITCH_PRIVATE_RESULT", json.dumps(view))
            self.assertNotIn("OTHER_PRIVATE_AGENT_MEMORY", json.dumps(view))
            self.assertIsNone(next(player for player in view["players"] if player["id"] == 4)["role"])
        self.assertEqual(room.game.night_choices, {})
        self.assertIsNone(room.game.night_kill)
        self.assertEqual(room.game.phase, "night_wolves")
        for player_id in (2, 3):
            room.engine.apply(
                player_id,
                {
                    "action": "wolf_kill",
                    "target": 4,
                    "game_id": room.game.game_id,
                    "turn_sequence": room.game.turn_sequence,
                },
            )
        self.assertEqual(room.game.night_choices, {"2": 4, "3": 4})
        # Seat 3's seer was replaced by a wolf in this fixture; skip that stage.
        self.assertEqual(room.game.phase, "night_witch")
        self.assertEqual(room.game.night_kill, 4)

    async def test_pet_flood_manager_gate_does_not_schedule_extra_model_calls(self):
        room = await self.active()
        self.manager.limits.config["pet_per_minute"] = 2
        calls = []

        async def reply(*args):
            calls.append(1)
            return "只依据公开信息的建议", {"facts": []}

        self.manager.ai.pet_reply = reply
        for index in range(2):
            await self.manager.pet_chat(room.game.room_id, "host", "公开信息建议", f"allowed-{index}")
        for index in range(15):
            with self.assertRaises(RateLimitError):
                await self.manager.pet_chat(room.game.room_id, "host", "重复刷调用", f"blocked-{index}")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(room.game.pets["host"].private_chat_history), 4)
        self.assertEqual(self.manager.snapshot(room, "host")["ai_budget"]["game"]["requests"], 0)
        self.assertTrue(room.engine.tick(room.game.turn_deadline))

    async def test_game_budget_zero_prevents_http_falls_back_and_keeps_clock_running(self):
        room = await self.active()
        self.manager.router.registry.register("dashscope", "qwen-plus", configured=True)
        room.game.player(3).provider = "dashscope"
        room.game.player(3).model_key = "dashscope:qwen-plus"
        self.manager.limits.config["game_max_requests"] = 0
        room.engine.enter("day_vote")
        self.manager.commit(room)
        with patch("app.llm.httpx.AsyncClient", side_effect=AssertionError("HTTP must not be attempted")) as http:
            await self.manager.actor(room, 3, room.game.turn_sequence)
            http.assert_not_called()
        self.assertIn("3", room.game.votes)
        host = self.manager.snapshot(room, "host")
        self.assertFalse(host["ai_budget"]["real_calls_allowed"])
        self.assertEqual(host["ai_budget"]["fallback_reason"], "game_request_budget")
        execution = room.game.player(3).execution_status
        self.assertEqual(execution["provider_used"], "mock")
        self.assertEqual(execution["status"], "budget_exhausted")
        self.assertEqual(execution["failure_reason"], "limit:game_request_budget")
        sequence = room.game.turn_sequence
        self.assertTrue(room.engine.tick(room.game.turn_deadline))
        self.assertGreater(room.game.turn_sequence, sequence)

    async def test_one_hundred_speech_chunks_only_send_events_then_persist_completion(self):
        room = await self.active()
        room.engine.enter("day_speech", 2)
        room.game.speech_queue = [3, 4, 5, 6, 1]
        self.manager.commit(room)
        connection = await self.manager.subscribe(room.game.room_id, "host")
        drain(connection)
        streamed, finish = asyncio.Event(), asyncio.Event()

        async def chunks(*args):
            for _ in range(100):
                yield "字"
            streamed.set()
            await finish.wait()

        self.manager.ai.speak = chunks
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            actor = asyncio.create_task(self.manager.actor(room, 2, room.game.turn_sequence))
            try:
                await asyncio.wait_for(streamed.wait(), 2)
                packets = drain(connection)
                self.assertEqual(save.call_count, 0)
                self.assertNotIn("state_snapshot", [p["type"] for p in packets])
                speech = [p for p in packets if p["type"] == "speech_chunk"]
                self.assertEqual(len(speech), 100)
                self.assertEqual("".join(p["data"]["delta"] for p in speech), "字" * 100)
                revisions = [p["state_revision"] for p in speech]
                self.assertEqual(revisions, sorted(set(revisions)))
                self.assertTrue(all(p["state_revision"] == p["data"]["state_revision"] for p in speech))
                self.assertEqual(len({p["event_id"] for p in speech}), 100)
            finally:
                finish.set()
                await asyncio.wait_for(actor, 2)
            self.assertEqual(save.call_count, 1)
            completed = drain(connection)
            self.assertEqual([p["type"] for p in completed].count("state_snapshot"), 1)
            self.assertIn("speech_finished", [p["type"] for p in completed])
        restored = self.store.get(room.game.room_id)
        speeches = [event for event in restored.events if event["kind"] == "speech"]
        self.assertEqual(speeches[-1]["speech"], "字" * 100)
        self.assertEqual(restored.current_turn_player_id, 3)
        self.assertEqual(restored.state_revision, room.game.state_revision)

    async def test_replayed_action_is_acknowledged_once_and_survives_restart(self):
        room = await self.active()
        room.engine.enter("day_vote")
        self.manager.commit(room)
        action = room.engine.action_for(1)
        payload = {
            "action": "vote",
            "target": 2,
            "action_id": "persistent-vote-uuid",
            "game_id": room.game.game_id,
            "turn_sequence": room.game.turn_sequence,
            "turn_id": room.game.turn_id,
            "expected_state_revision": room.game.state_revision,
        }
        first = await self.manager.command(room.game.room_id, "host", "action", payload)
        revision = room.game.state_revision
        second = await self.manager.command(room.game.room_id, "host", "action", payload)
        self.assertFalse(first["action_ack"]["replayed"])
        self.assertTrue(second["action_ack"]["replayed"])
        self.assertEqual(first["action_ack"]["state_revision"], second["action_ack"]["state_revision"])
        self.assertEqual(room.game.state_revision, revision)
        self.assertEqual(room.game.votes, {"1": 2})
        self.assertEqual(len([e for e in room.engine.outbox if e["type"] == "player_action"]), 0)
        restarted = RoomManager(self.store, LLMRouter(), time_scale=10, ai_pause=0)
        self.managers.append(restarted)
        result = await restarted.command(room.game.room_id, "host", "action", payload)
        self.assertTrue(result["action_ack"]["replayed"])
        self.assertEqual(result["action_ack"]["state_revision"], revision)
        self.assertEqual(restarted.require(room.game.room_id, "host").game.votes, {"1": 2})
        before = room.game.dump()
        with self.assertRaises(ValueError):
            await self.manager.command(
                room.game.room_id, "host", "action", {**payload, "action_id": "different-id", "target": 1}
            )
        self.assertEqual(room.game.dump(), before)
        self.assertLess(action["state_revision"], revision)

    async def test_stale_ai_result_for_previous_turn_is_discarded_and_logged(self):
        room = await self.active()
        room.engine.enter("day_vote")
        self.manager.commit(room)
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(view, *args):
            started.set()
            await release.wait()
            return {
                "action": "vote",
                "target": 4,
                "game_id": view["game_id"],
                "turn_id": view["turn_id"],
                "turn_sequence": view["turn_sequence"],
            }

        self.manager.ai.propose = blocked
        actor = asyncio.create_task(self.manager.actor(room, 3, room.game.turn_sequence))
        await asyncio.wait_for(started.wait(), 2)
        room.engine.enter("day_speech", 4)
        self.manager.commit(room)
        before = room.game.dump()
        with self.assertLogs("app.rooms", level="INFO") as logs:
            release.set()
            await asyncio.wait_for(actor, 2)
        self.assertTrue(any("stale_ai_response" in message for message in logs.output))
        self.assertEqual(room.game.dump(), before)
        self.assertEqual(room.game.votes, {})
        self.assertEqual(room.game.current_turn_player_id, 4)

    async def test_finished_room_unloads_from_memory_and_lazy_loads_from_sqlite(self):
        room = await self.active()
        room.engine.finish("good")
        self.manager.commit(room)
        room.last_access = time.time() - 600
        self.manager.unload_seconds = 10
        rid, game_id = room.game.room_id, room.game.game_id
        self.manager.maintenance()
        self.assertNotIn(rid, self.manager.rooms)
        persisted = self.store.get(rid)
        self.assertEqual(persisted.lifecycle, "FINISHED")
        self.assertEqual(persisted.game_id, game_id)
        loaded = self.manager.require(rid, "host")
        self.assertEqual(loaded.game.game_id, game_id)
        self.assertTrue(loaded.game.game_over)
        self.assertEqual(loaded.game.events, persisted.events)

    async def test_close_room_cancels_ai_job_and_persists_closed_lifecycle(self):
        room = await self.active()
        blocked = asyncio.Event()
        task = asyncio.create_task(blocked.wait())
        room.jobs[(room.game.turn_sequence, 2)] = task
        await asyncio.sleep(0)
        view = await self.manager.command(room.game.room_id, "host", "close", {})
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertEqual(room.jobs, {})
        self.assertEqual(view["lifecycle"], "ARCHIVED")
        self.assertIsNone(view["turn_deadline"])
        self.assertEqual(self.store.get(room.game.room_id).lifecycle, "ARCHIVED")
        with self.assertRaises(ValueError):
            await self.manager.command(room.game.room_id, "host", "start", {})

    async def test_rematch_preserves_members_but_resets_game_private_state(self):
        room = await self.active(second_human=True)
        game = room.game
        game.player(1).private_notes.append("OLD_PRIVATE_CHECK_RESULT")
        game.player(1).memory = {"facts": [{"text": "OLD_PRIVATE_MEMORY"}]}
        game.pets["host"].current_game_memory = {"facts": ["OLD_PET_MEMORY"]}
        room.engine.pet_message("host", "assistant", "OLD_PRIVATE_CHAT")
        room.engine.wolf_message(2, "OLD_WOLF_CHAT")
        room.engine.finish("good")
        self.manager.commit(room)
        old_id, old_agent = game.game_id, game.pets["host"].agent_id
        old_humans = [(p.id, p.name, p.owner_id) for p in game.players if p.owner_id]
        view = await self.manager.command(game.room_id, "host", "rematch", {})
        self.assertNotEqual(view["game_id"], old_id)
        self.assertEqual(view["lifecycle"], "LOBBY")
        self.assertEqual(view["phase"], "lobby")
        self.assertEqual([(p.id, p.name, p.owner_id) for p in game.players], old_humans)
        self.assertNotEqual(game.pets["host"].agent_id, old_agent)
        self.assertEqual(game.pets["host"].private_chat_history, [])
        self.assertEqual(game.pets["host"].current_game_memory, {})
        self.assertEqual(game.wolf_chat, [])
        self.assertEqual(game.processed_actions, {})
        self.assertEqual(game.player(1).private_notes, [])
        self.assertFalse(game.game_over)
        self.assertEqual(game.day, 1)
        self.assertNotIn("OLD_", json.dumps(game.dump(), ensure_ascii=False))
        self.assertEqual(set(game.seat_presets), {"3", "4", "5", "6"})


if __name__ == "__main__":
    unittest.main()
