"""Semantic memory retains early evidence without crossing the actor's scope."""

import json
import unittest

from app.ai import AIOrchestrator, memory_from
from app.llm import LLMRouter
from app.memory import build_memory_from_view, new_memory, prompt_memory, update_memory


def speech(seq, pid, text, day=1):
    return {
        "type": "chat_message",
        "event_id": f"speech:{seq}",
        "audience": "public",
        "data": {
            "kind": "speech",
            "day": day,
            "seq": seq,
            "player_id": pid,
            "speech": text,
            "text": f"{pid}号：{text}",
        },
    }


class MemoryTests(unittest.TestCase):
    def test_role_and_check_claims_are_unconfirmed_and_keep_provenance(self):
        memory = update_memory(new_memory(), speech(1, 2, "我是预言家，首夜查杀3号，4号是我的金水。"), 1)
        self.assertEqual(memory["claims"][0]["claimed_role"], "seer")
        self.assertEqual(
            {(c["target_player_id"], c["result"]) for c in memory["check_claims"]}, {(3, "wolf"), (4, "good")}
        )
        self.assertTrue(all(not f["confirmed"] and f["source_player_id"] == 2 for f in memory["facts"]))
        self.assertTrue(
            all(f["id"] and f["event_id"] == "speech:1" and f["visibility"] == "public" for f in memory["facts"])
        )
        self.assertTrue(all(b["confidence"] < 0.5 and not b["confirmed"] for b in memory["beliefs"]))
        negative = update_memory(new_memory(), speech(2, 2, "我不是预言家，3号像预言家。"), 1)
        self.assertFalse(negative["claims"])

    def test_long_game_keeps_early_semantic_evidence_and_own_speech(self):
        memory = update_memory(new_memory(), speech(1, 1, "我是村民，支持2号，更怀疑5号。"), 1)
        memory = update_memory(memory, speech(2, 2, "我是预言家，查验3号是好人。"), 1)
        for seq in range(3, 603):
            memory = update_memory(memory, speech(seq, 4, "目前证据不足，继续听发言。", seq // 30 + 1), 1)
        self.assertEqual(len(memory["recent_events"]), 12)
        self.assertTrue(any(f["event_id"] == "speech:1" for f in memory["facts"]))
        self.assertTrue(any(f["event_id"] == "speech:2" for f in memory["facts"]))
        self.assertEqual(memory["self_history"][0]["text"], "我是村民，支持2号，更怀疑5号。")
        self.assertTrue(any(c["target_player_id"] == 3 and c["result"] == "good" for c in memory["check_claims"]))
        compact = prompt_memory(memory)
        self.assertNotIn("processed_event_ids", compact)
        self.assertTrue(any(f["event_id"] == "speech:2" for f in compact["facts"]))

    def test_stance_changes_denials_and_changed_check_results_need_explanation(self):
        memory = update_memory(new_memory(), speech(1, 5, "支持2号，查验4号是好人。"), 1)
        memory = update_memory(memory, speech(2, 5, "支持3号。我从未相信2号，查杀4号。", day=2), 1)
        summaries = [c["summary"] for c in memory["contradictions"]]
        self.assertTrue(any("立场" in text for text in summaries))
        self.assertTrue(any("否认曾" in text for text in summaries))
        self.assertTrue(any("查验声称前后不同" in text for text in summaries))
        self.assertTrue(all(c["requires_explanation"] and not c["confirmed"] for c in memory["contradictions"]))
        memory = update_memory(memory, speech(3, 5, "我是村民。", day=2), 1)
        memory = update_memory(memory, speech(4, 5, "我是女巫。", day=3), 1)
        self.assertTrue(any("身份声称" in c["summary"] for c in memory["contradictions"]))

    def test_public_votes_received_and_self_private_choices_are_distinct(self):
        event = {
            "type": "player_action",
            "event_id": "private-action",
            "audience": "player",
            "player_id": 1,
            "data": {"day": 1, "action": "vote", "target": 3},
        }
        memory = update_memory(new_memory(), event, 1)
        self.assertEqual(memory["self_history"][0]["target"], 3)
        self.assertFalse(memory["vote_history"])
        self.assertFalse(update_memory(new_memory(), event, 2)["self_history"])
        ballot = {
            "kind": "vote",
            "event_id": "vote-result",
            "day": 1,
            "text": "公开票型",
            "votes": {"1": 3, "2": 1, "3": 1, "4": None},
        }
        memory = update_memory(memory, ballot, 1)
        self.assertEqual(memory["vote_history"][0]["votes"]["4"], None)
        self.assertEqual(memory["received_votes"][0]["from_player_ids"], [2, 3])
        self.assertEqual(memory["self_history"][-1]["visibility"], "public")
        self.assertTrue(memory["facts"][-1]["confirmed"])

    def test_private_checks_stay_private_and_overrule_later_public_claims(self):
        check = {
            "type": "private_note",
            "event_id": "own-check",
            "audience": "player",
            "player_id": 1,
            "data": {"day": 1, "note": "第1夜查验：3号是狼人。"},
        }
        memory = update_memory(new_memory(), check, 1)
        self.assertEqual(memory["facts"][0]["kind"], "private_check")
        self.assertEqual(memory["facts"][0]["visibility"], "private")
        self.assertEqual(memory["beliefs"][0]["confidence"], 1)
        memory = update_memory(memory, speech(2, 2, "我是预言家，查验3号是好人。"), 1)
        belief = next(b for b in memory["beliefs"] if b["player_id"] == 3)
        self.assertTrue(belief["confirmed"])
        self.assertEqual(belief["role_guess"], "wolf")
        other = update_memory(new_memory(), check, 2)
        self.assertFalse(other["facts"])
        self.assertFalse(other["beliefs"])

    def test_updates_deduplicate_and_do_not_alias_player_or_event(self):
        first = new_memory()
        event = speech(1, 2, "我是预言家。")
        second = update_memory(first, event, 1)
        self.assertFalse(first["claims"])
        event["data"]["speech"] = "外部修改"
        self.assertEqual(second["claims"][0]["claimed_role"], "seer")
        repeat = update_memory(second, speech(1, 2, "我是预言家。"), 1)
        self.assertEqual(repeat, second)
        repeat["claims"][0]["claimed_role"] = "wolf"
        self.assertEqual(second["claims"][0]["claimed_role"], "seer")
        noisy = update_memory(second, {"type": "speech_chunk", "data": {"chunk": "我是狼人"}}, 1)
        self.assertEqual(noisy, second)

    def test_existing_memory_is_primary_and_old_save_migration_is_once(self):
        memory = update_memory(new_memory(), speech(1, 2, "我是预言家。"), 1)
        view = {
            "self": {"id": 1, "memory": memory, "private_notes": ["应忽略的全量旧笔记"]},
            "events": [speech(100, 5, "我是狼人。")],
            "day": 2,
        }
        result = memory_from(view)
        self.assertEqual(result, memory)
        result["claims"][0]["claimed_role"] = "villager"
        self.assertEqual(memory["claims"][0]["claimed_role"], "seer")
        legacy = {
            "self": {"id": 1, "private_notes": ["第1夜查验：3号是好人。"]},
            "events": [speech(1, 2, "我是预言家。")],
            "day": 2,
        }
        migrated = build_memory_from_view(legacy, {"judgments": [{"action": "vote", "target": 5}]})
        self.assertEqual(migrated["judgments"][-1]["target"], 5)
        self.assertEqual(migrated["beliefs"][-1]["role_guess"], "good")
        legacy["self"]["memory"] = migrated
        self.assertEqual(memory_from(legacy), migrated)

    def test_engine_emits_updates_once_and_filters_private_skill_by_scope(self):
        from app.game import WerewolfGame
        from app.rules import RuleEngine
        from app.scope import InformationScope

        game = WerewolfGame("memory-scope", "owner")
        engine = RuleEngine(game)
        engine.join("owner", "甲", 1)
        engine.start("owner", now=100)
        for p, role in zip(game.players, ["seer", "wolf", "wolf", "villager", "witch", "villager"]):
            p.role = role
        engine.record("speech", "公开发言", 2, speech="我是预言家，支持4号。")
        engine.enter("night_seer", now=100)
        engine.action_for(1)
        engine.apply(
            1,
            {"action": "seer_inspect", "target": 2, "game_id": game.game_id, "turn_sequence": game.turn_sequence},
            now=101,
        )
        private = json.dumps(InformationScope.ai_view(game, 1)["self"]["memory"], ensure_ascii=False)
        outsider = json.dumps(InformationScope.ai_view(game, 4)["self"]["memory"], ensure_ascii=False)
        self.assertIn("查验：2号是狼人", private)
        self.assertNotIn("查验：2号是狼人", outsider)
        self.assertTrue(any(c["player_id"] == 2 for c in game.player(4).memory["claims"]))
        game.player(1).memory["claims"][0]["claimed_role"] = "witch"
        self.assertEqual(game.player(4).memory["claims"][0]["claimed_role"], "seer")

    def test_ai_prompt_distinguishes_claims_and_keeps_each_agent_scope(self):
        memory = update_memory(new_memory(), speech(1, 2, "我是预言家。"), 1)
        view = {
            "self": {"id": 1, "role": "村民", "role_key": "villager", "memory": memory},
            "events": [speech(i, 4, "证据不足。") for i in range(30)],
            "day": 1,
            "turn_sequence": 2,
            "players": [{"id": 1, "alive": True}],
            "pending_action": None,
        }
        system, user, _ = AIOrchestrator(LLMRouter()).context(view, "detective", memory)
        payload = json.loads(user)
        self.assertIn("confirmed=false", system)
        self.assertEqual(len(payload["player_view"]["events"]), 12)
        self.assertNotIn("memory", payload["player_view"]["self"])
        self.assertEqual(payload["memory"]["claims"][0]["claimed_role"], "seer")
        self.assertEqual(len(view["events"]), 30)
        self.assertIn("memory", view["self"])


if __name__ == "__main__":
    unittest.main()
