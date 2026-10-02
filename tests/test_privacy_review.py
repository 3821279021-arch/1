"""Regressions for session expiry and authoritative room archival."""

import hashlib
import json
import time
import unittest
from unittest.mock import patch

from app.game import WerewolfGame
from app.llm import LLMRouter
from app.persistence import Store
from app.rooms import Connection, RoomManager
from app.rules import RuleEngine


class PrivacyLifecycleReviewTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"SESSION_TTL_DAYS": "1"})
        self.environment.start()
        self.store = Store(":memory:")
        self.manager = RoomManager(self.store, LLMRouter())
        self.manager.lobby_ttl = self.manager.finished_ttl = 60
        self.manager.unload_seconds = 100000
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.environment.stop()

    def room(self, room_id, lifecycle="FINISHED"):
        game = WerewolfGame(room_id, "owner")
        engine = RuleEngine(game)
        engine.join("owner", "房主", 1)
        if lifecycle == "FINISHED":
            engine.finish("draw", now=self.now - 120)
        elif lifecycle == "ACTIVE":
            engine.start("owner", now=self.now)
        room = self.manager._room(game)
        room.last_access = self.now
        self.manager.rooms[room_id] = room
        self.store.save(game)
        if lifecycle == "LOBBY":
            with self.store.db:
                self.store.db.execute("UPDATE rooms SET updated_at=? WHERE room_id=?", (self.now - 120, room_id))
        return room

    def test_expired_session_is_rejected_before_cleanup_runs(self):
        owner, token = self.store.identity()
        self.assertEqual(self.store.authenticate(token), owner)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.db:
            self.store.db.execute("UPDATE identities SET created_at=? WHERE token_hash=?", (self.now - 86401, digest))
        self.assertIsNone(self.store.authenticate(token))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM identities").fetchone()[0], 1)
        self.store.archive_expired(now=self.now, lobby_seconds=60, finished_seconds=60)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM identities").fetchone()[0], 0)

    def test_archive_updates_in_memory_engine_snapshot_and_database_revision(self):
        room = self.room("archive-in-memory")
        old_revision = room.game.state_revision
        old_game_id = room.game.game_id
        # Inspect the authoritative archival step before V3.2's immediate TTL
        # purge. A normal maintenance call below must then remove all state.
        with patch.object(self.store, "cleanup_expired_rooms", return_value=[]):
            self.manager.maintenance(self.now)
        self.assertEqual(room.game.lifecycle, "ARCHIVED")
        self.assertIs(room.engine.g, room.game)
        stored = self.store.get(room.game.room_id)
        snapshot = self.manager.snapshot(room, "owner")
        self.assertEqual(stored.lifecycle, snapshot["lifecycle"])
        self.assertEqual(stored.state_revision, old_revision + 1)
        self.assertEqual(snapshot["state_revision"], stored.state_revision)
        self.assertEqual(snapshot["game_id"], old_game_id)
        # A later save must persist the new archived object, never resurrect the
        # stale FINISHED object previously held by the room's engine.
        self.manager.commit(room)
        self.assertEqual(self.store.get(room.game.room_id).lifecycle, "ARCHIVED")
        self.assertEqual(self.store.get(room.game.room_id).state_revision, snapshot["state_revision"])
        self.manager.maintenance(self.now)
        self.assertIsNone(self.store.get(room.game.room_id))
        self.assertNotIn(room.game.room_id, self.manager.rooms)

    def test_connected_finished_room_stays_finished_until_disconnected(self):
        room = self.room("connected-finished")
        connection = Connection("owner")
        room.connections.add(connection)
        revision = room.game.state_revision
        self.manager.maintenance(self.now)
        self.assertEqual(room.game.lifecycle, "FINISHED")
        self.assertEqual(self.store.get(room.game.room_id).lifecycle, "FINISHED")
        self.assertEqual(self.store.get(room.game.room_id).state_revision, revision)
        self.manager.commit(room)
        self.assertEqual(self.store.get(room.game.room_id).lifecycle, "FINISHED")
        room.connections.remove(connection)
        self.manager.maintenance(self.now)
        self.assertEqual(room.game.lifecycle, "ARCHIVED")
        self.assertIsNone(self.store.get(room.game.room_id))
        self.assertEqual(self.store.completed_list(room.game.room_id), [])

    def test_connected_lobby_and_active_clocks_are_protected(self):
        lobby = self.room("connected-lobby", "LOBBY")
        lobby.connections.add(Connection("owner"))
        active = self.room("active-clock", "ACTIVE")
        with self.store.db:
            self.store.db.execute(
                "UPDATE rooms SET updated_at=? WHERE room_id=?", (self.now - 120, active.game.room_id)
            )
        self.manager.maintenance(self.now)
        self.assertEqual(lobby.game.lifecycle, "LOBBY")
        self.assertEqual(self.store.get(lobby.game.room_id).lifecycle, "LOBBY")
        self.assertEqual(active.game.lifecycle, "ACTIVE")
        self.assertEqual(self.store.get(active.game.room_id).lifecycle, "ACTIVE")
        self.assertIn(active.game.room_id, self.manager.rooms)

    def test_legacy_real_bindings_and_each_private_agent_are_migrated_independently(self):
        from app.scope import InformationScope

        game = WerewolfGame("legacy-real-agents", "owner-a")
        engine = RuleEngine(game)
        engine.join("owner-a", "甲", 1)
        engine.join("owner-b", "乙", 2)
        engine.start("owner-a", now=self.now)
        for player, role in zip(game.players, ["villager", "villager", "seer", "witch", "wolf", "wolf"]):
            player.role = role
        game.player(3).provider = game.player(4).provider = "dashscope"
        game.player(3).private_notes = ["第1夜查验：5号是狼人。"]
        game.player(4).private_notes = ["第1夜你救了 1 号。"]
        game.pets["owner-a"].private_chat_history = [{"role": "user", "text": "甲的私密偏好"}]
        game.pets["owner-b"].private_chat_history = [{"role": "user", "text": "乙的私密偏好"}]
        encoded = game.dump()
        for player in encoded["players"]:
            for field in ("agent_id", "model_key", "model", "voice_profile", "conversation_state", "execution_status"):
                player.pop(field, None)
            player["memory"] = {"facts": [], "judgments": []}
        for pet in encoded["pets"].values():
            for field in ("agent_id", "model_key", "execution_status"):
                pet.pop(field, None)
        for field in ("lifecycle", "unique_model_per_ai_seat", "state_revision"):
            encoded.pop(field, None)
        with patch.dict("os.environ", {"DASHSCOPE_MODEL": "qwen-plus"}):
            restored = WerewolfGame.restore(encoded)
        self.assertEqual(restored.lifecycle, "ACTIVE")
        self.assertFalse(restored.unique_model_per_ai_seat)
        self.assertEqual(restored.player(3).model_key, "dashscope:qwen-plus")
        self.assertEqual(restored.player(4).model_key, "dashscope:qwen-plus")
        self.assertNotEqual(restored.player(3).agent_id, restored.player(4).agent_id)
        self.assertNotEqual(restored.player(3).voice_profile, restored.player(4).voice_profile)
        self.assertNotEqual(restored.pets["owner-a"].agent_id, restored.pets["owner-b"].agent_id)
        room = self.manager._room(restored)
        seer = json.dumps(InformationScope.ai_view(restored, 3), ensure_ascii=False)
        witch = json.dumps(InformationScope.ai_view(restored, 4), ensure_ascii=False)
        self.assertIn("查验：5号是狼人", seer)
        self.assertNotIn("查验：5号是狼人", witch)
        self.assertIn("你救了 1 号", witch)
        self.assertNotIn("你救了 1 号", seer)
        owner_a = json.dumps(InformationScope.owner_view(restored, "owner-a"), ensure_ascii=False)
        self.assertIn("甲的私密偏好", owner_a)
        self.assertNotIn("乙的私密偏好", owner_a)
        room.game.player(3).memory["facts"][-1]["text"] = "记忆外部修改"
        self.assertNotIn("记忆外部修改", json.dumps(room.game.player(4).memory, ensure_ascii=False))

    def test_unloaded_archive_lazy_load_keeps_latest_authoritative_revision(self):
        room = self.room("archive-unload")
        old_revision = room.game.state_revision
        # Manually closed archives keep the legacy retention window; automatic
        # TTL expiry now purges immediately rather than remaining reloadable.
        room.engine.close_room("owner")
        self.manager.commit(room)
        room.last_access = self.now - 120
        self.manager.unload_seconds = 60
        self.manager.maintenance(self.now)
        self.assertNotIn(room.game.room_id, self.manager.rooms)
        stored = self.store.get(room.game.room_id)
        restored = self.manager.get(room.game.room_id)
        snapshot = self.manager.snapshot(restored, "owner")
        self.assertEqual(snapshot["lifecycle"], "ARCHIVED")
        self.assertEqual(snapshot["state_revision"], old_revision + 1)
        self.assertEqual(restored.engine.g.state_revision, stored.state_revision)
        self.assertIs(restored.engine.g, restored.game)


if __name__ == "__main__":
    unittest.main()
