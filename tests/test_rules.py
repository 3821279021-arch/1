import json
import tempfile
import unittest

from app.game import WerewolfGame
from app.persistence import Store
from app.rules import RuleEngine
from app.scope import InformationScope


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.g = WerewolfGame("room-one", "a")
        self.e = RuleEngine(self.g)
        self.e.join("a", "甲", 1)
        self.e.join("b", "乙", 2)
        self.e.start("a", now=100)
        for p, role in zip(self.g.players, ["villager", "seer", "wolf", "wolf", "witch", "villager"]):
            p.role = role

    def payload(self, pid, **values):
        return {
            "action": self.e.action_for(pid)["type"],
            "game_id": self.g.game_id,
            "turn_sequence": self.g.turn_sequence,
            **values,
        }

    def test_scopes_separate_all_three_channels(self):
        self.e.wolf_message(3, "秘密刀人讨论")
        self.e.pet_message("b", "user", "私人宠物消息")
        self.g.player(2).private_notes.append("查验结果")
        village = InformationScope.owner_view(self.g, "a")
        encoded = json.dumps(village, ensure_ascii=False)
        self.assertNotIn("秘密刀人讨论", encoded)
        self.assertNotIn("私人宠物消息", encoded)
        self.assertNotIn("查验结果", encoded)
        self.assertNotIn("wolf_chat", village)
        self.assertTrue(all(p["role"] is None for p in village["players"] if p["id"] != 1))
        self.assertIn("秘密刀人讨论", json.dumps(InformationScope.ai_view(self.g, 3), ensure_ascii=False))
        with self.assertRaises(ValueError):
            InformationScope.owner_view(self.g, "intruder")
        self.assertFalse(InformationScope.permits(self.g, "a", {"audience": "wolves", "player_id": None}))

    def test_wrong_phase_turn_dead_target_and_duplicate(self):
        self.e.enter("day_speech", 1, now=100)
        wrong = {
            "action": "speech",
            "speech": "恶意发言",
            "game_id": self.g.game_id,
            "turn_sequence": self.g.turn_sequence,
        }
        with self.assertRaises(ValueError):
            self.e.apply(2, wrong, now=101)
        self.e.enter("day_vote", now=100)
        with self.assertRaises(ValueError):
            self.e.apply(1, self.payload(1, target=1), now=101)
        self.g.player(6).alive = False
        with self.assertRaises(ValueError):
            self.e.apply(1, self.payload(1, target=6), now=101)
        valid = self.payload(1, target=2)
        self.e.apply(1, valid, now=101)
        with self.assertRaises(ValueError):
            self.e.apply(1, valid, now=102)
        self.assertNotIn("target", str(InformationScope.owner_view(self.g, "b")["vote_status"]))
        self.assertFalse(any(e["kind"] == "vote" for e in self.g.events))

    def test_witch_validation_is_atomic(self):
        self.e.enter("night_witch", now=100)
        self.g.night_kill = 1
        before = self.g.dump()
        with self.assertRaises(ValueError):
            self.e.apply(5, self.payload(5, save=True, poison_target=5), now=101)
        self.assertEqual(before, self.g.dump())
        self.e.apply(5, self.payload(5, save=True), now=101)
        self.assertFalse(self.g.witch_antidote)
        self.assertIsNone(self.g.night_kill)

    def test_deadline_and_stale_request(self):
        self.e.enter("day_speech", 1, now=100)
        self.g.speech_queue = [2, 3, 4, 5, 6]
        request = self.payload(1, speech="你好")
        deadline = self.g.turn_deadline
        self.assertFalse(self.e.tick(deadline - 0.01))
        with self.assertRaises(ValueError):
            self.e.apply(1, request, now=deadline)
        self.e.tick(deadline)
        self.assertEqual(self.g.current_turn_player_id, 2)
        with self.assertRaises(ValueError):
            self.e.apply(1, request, now=deadline + 1)

    def test_vote_abstention_tie_and_reveal(self):
        self.e.enter("day_vote", now=100)
        self.e.apply(1, self.payload(1, target=2), now=101)
        self.e.apply(2, self.payload(2, target=1), now=101)
        self.e.tick(115)
        self.assertTrue(self.g.player(1).alive)
        self.assertTrue(self.g.player(2).alive)
        self.assertEqual(self.g.phase, "night_discussion")
        vote = next(e for e in self.g.events if e["kind"] == "vote")
        self.assertEqual(vote["votes"]["6"], None)

    def test_wolf_requires_majority(self):
        self.e.enter("night_wolves", now=100)
        self.e.apply(3, self.payload(3, target=1), now=101)
        self.e.tick(115)
        self.assertIsNone(self.g.night_kill)

    def test_persistence_and_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder + "/state.sqlite")
            owner, token = store.identity()
            store.save(self.g)
            store.close()
            store = Store(folder + "/state.sqlite")
            restored = store.load()[0]
            self.assertEqual(restored.dump(), self.g.dump())
            self.assertEqual(restored.turn_deadline, self.g.turn_deadline)
            self.assertEqual(store.authenticate(token), owner)
            self.assertIsNone(store.authenticate("forged"))
            other = WerewolfGame("another", "b")
            RuleEngine(other).join("b", "另一个", 1)
            self.assertNotEqual(restored.game_id, other.game_id)
            self.assertEqual(other.phase, "lobby")
            store.close()

    def test_pet_config_validation_is_atomic(self):
        before = self.g.dump()
        with self.assertRaises(ValueError):
            self.e.configure_pet("a", {"control_mode": "autopilot", "play_style": {"aggression": 99}})
        self.assertEqual(before, self.g.dump())

    def test_lobby_human_can_replace_configured_bot(self):
        game = WerewolfGame("join-planned-ai", "host")
        engine = RuleEngine(game)
        engine.join("host", "房主", 1)
        engine.configure("host", "fast", [{"id": 2, "provider": "dashscope", "personality": "detective"}])
        engine.join("friend", "朋友", 2)
        self.assertEqual(game.player(2).owner_id, "friend")
        self.assertEqual(len([p for p in game.players if p.id == 2]), 1)
        with self.assertRaises(ValueError):
            engine.join("intruder", "抢座", 2)
        engine.start("host")
        with self.assertRaises(ValueError):
            engine.join("late", "迟到", 3)

    def test_ai_context_and_memory_cannot_mutate_state(self):
        self.e.record("vote", "公开票型", votes={"1": 2})
        view = InformationScope.ai_view(self.g, 1)
        view["events"][-1]["votes"]["1"] = 999
        self.assertEqual(self.g.events[-1]["votes"]["1"], 2)
        memory = {"judgments": [{"target": 2}]}
        self.e.remember(1, memory)
        memory["judgments"][0]["target"] = 999
        self.assertEqual(self.g.player(1).memory["judgments"][0]["target"], 2)

    def test_cancel_delegation(self):
        self.e.configure_pet("a", {"delegate_next": True})
        self.assertTrue(self.g.pets["a"].delegate_next)
        self.e.configure_pet("a", {"control_mode": "copilot"})
        self.assertFalse(self.g.pets["a"].delegate_next)


if __name__ == "__main__":
    unittest.main()
