"""Presence, cancellation, durable resume and private artifact retention."""

import asyncio
import time
import unittest

from app.llm import LLMRouter
from app.persistence import Store
from app.rooms import RoomManager


class SuspensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.manager = RoomManager(self.store, LLMRouter(), ai_pause=0)
        state = await self.manager.create("owner", "甲", 1)
        self.rid = state["room_id"]
        self.room = self.manager.get(self.rid)
        await self.manager.command(self.rid, "owner", "start", {})

    async def asyncTearDown(self):
        await self.manager.close()
        self.store.close()

    async def test_disconnect_grace_reconnect_and_expiry(self):
        c = await self.manager.subscribe(self.rid, "owner")
        deadline = self.room.game.turn_deadline
        await self.manager.unsubscribe(self.rid, c)
        since = self.room.offline_since
        self.assertFalse(self.manager.check_presence(self.room, since + 44))
        c = await self.manager.subscribe(self.rid, "owner")
        self.assertEqual(self.room.game.turn_deadline, deadline)
        await self.manager.unsubscribe(self.rid, c)
        self.assertTrue(self.manager.check_presence(self.room, self.room.offline_since + 46))
        self.assertEqual(self.room.game.lifecycle, "SUSPENDED")

    async def test_leave_freezes_unloads_and_requires_explicit_resume(self):
        deadline = self.room.game.turn_deadline
        before = time.time()
        result = await self.manager.command(self.rid, "owner", "leave", {})
        self.assertTrue(result["left"])
        remaining = self.room.game.suspended_remaining
        self.assertAlmostEqual(remaining, deadline - before, delta=0.2)
        self.assertFalse(self.room.engine.tick(time.time() + 86400))
        self.room.last_access = time.time() - 600
        await self.manager.maintenance_async()
        self.assertNotIn(self.rid, self.manager.rooms)
        room = await self.manager.require_async(self.rid, "owner")
        self.assertEqual(room.game.suspended_remaining, remaining)
        c = await self.manager.subscribe(self.rid, "owner")
        self.assertEqual((await c.queue.get())["data"]["lifecycle"], "SUSPENDED")
        old_turn = room.game.turn_id
        resumed = await self.manager.command(self.rid, "owner", "resume", {})
        self.assertEqual(resumed["turn_id"], old_turn)
        self.assertAlmostEqual(resumed["turn_deadline"] - time.time(), remaining, delta=0.2)

    async def test_cancel_public_stream_and_pet_and_prevent_new_jobs(self):
        started = asyncio.Event()
        pet_started = asyncio.Event()
        cancelled = asyncio.Event()
        pet_cancelled = asyncio.Event()

        async def blocked(*args):
            started.set()
            try:
                await asyncio.Event().wait()
                yield "不会出现"
            finally:
                cancelled.set()

        async def blocked_pet(*args):
            pet_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                pet_cancelled.set()

        self.manager.ai.speak = blocked
        self.manager.ai.pet_reply = blocked_pet
        self.room.engine.enter("day_speech", 2)
        self.manager.launch_jobs(self.room)
        chat = asyncio.create_task(self.manager.pet_chat(self.rid, "owner", "建议"))
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(pet_started.wait(), 2)
        await self.manager.command(self.rid, "owner", "leave", {})
        await asyncio.wait_for(cancelled.wait(), 2)
        await asyncio.wait_for(pet_cancelled.wait(), 2)
        await asyncio.gather(chat, return_exceptions=True)
        self.manager.launch_jobs(self.room)
        self.assertEqual(self.room.jobs, {})
        with self.assertRaisesRegex(ValueError, "暂停"):
            await self.manager.pet_chat(self.rid, "owner", "再问")
        self.assertEqual(self.room.game.current_speech, "")

    async def test_leaving_with_another_online_player_does_not_pause(self):
        # Retain player ownership for resume and historical permissions.
        self.room.game.players[1].owner_id = "other"
        from app.game import PetAI

        self.room.game.pets["other"] = PetAI("other")
        await self.manager.subscribe(self.rid, "other")
        await self.manager.command(self.rid, "owner", "leave", {})
        self.assertEqual(self.room.game.lifecycle, "ACTIVE")

    async def test_expired_suspension_removes_all_private_data(self):
        await self.manager.command(self.rid, "owner", "leave", {})
        self.room.game.suspended_at = time.time() - 49 * 3600
        self.store.save(self.room.game)
        self.store.record_model("call", {"room_id": self.rid, "private": "private-marker"})
        await self.manager.maintenance_async()
        self.assertIsNone(self.store.get(self.rid))
        for table in ("room_events", "player_memory", "model_telemetry", "completed_games"):
            self.assertEqual(
                self.store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE room_id=?", (self.rid,)).fetchone()[0], 0
            )
        stats = self.store.db.execute("SELECT statistics FROM room_statistics").fetchone()[0]
        self.assertNotIn("owner", stats)
        self.assertNotIn("private-marker", stats)

    async def test_delete_requires_host_and_is_durable(self):
        self.room.game.players[1].owner_id = "other"
        with self.assertRaises(PermissionError):
            await self.manager.command(self.rid, "other", "delete", {})
        self.assertTrue((await self.manager.command(self.rid, "owner", "delete", {}))["deleted"])
        self.assertNotIn(self.rid, self.manager.rooms)
        self.assertIsNone(self.store.get(self.rid))

    async def test_old_save_defaults_and_restarted_suspension(self):
        await self.manager.command(self.rid, "owner", "leave", {})
        from app.game import WerewolfGame

        saved = self.room.game.dump()
        old = {
            k: v for k, v in saved.items() if k not in {"suspended_at", "suspended_remaining", "retention_expires_at"}
        }
        self.assertEqual(WerewolfGame.restore(old).lifecycle, "SUSPENDED")
        restarted = RoomManager(self.store, LLMRouter())
        self.assertNotIn(self.rid, restarted.rooms)
        self.assertEqual((await restarted.get_async(self.rid)).game.suspended_remaining, saved["suspended_remaining"])
        await restarted.close()
