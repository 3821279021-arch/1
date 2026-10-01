"""Regressions for failures observed with the authorized live DashScope tests."""

import asyncio
import hashlib
import json
import os
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.ai import AIOrchestrator
from app.game import WerewolfGame
from app.llm import LLMRouter
from app.memory import new_memory, update_memory
from app.rules import RuleEngine
from app.scope import InformationScope
from app.tokens import estimate_tokens


def event(seq, pid, text, day=1):
    return {
        "type": "speech",
        "event_id": f"live-regression:{seq}",
        "day": day,
        "audience": "public",
        "data": {"player_id": pid, "speech": text},
    }


def fixture():
    game = WerewolfGame("live-regression", "owner")
    engine = RuleEngine(game)
    engine.join("owner", "测试玩家", 1)
    engine.start("owner")
    for p, role in zip(game.players, ["seer", "wolf", "villager", "witch", "villager", "wolf"]):
        p.role = role
    engine.enter("day_vote")
    return game, engine, InformationScope.ai_view(game, 1)


class LiveFailureRegressions(unittest.IsolatedAsyncioTestCase):
    def test_qwen_vocabulary_counts_without_network_and_has_original_digest(self):
        path = Path(__file__).resolve().parents[1] / "app/tokenizers/qwen2-vocab.json"
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
        )
        with patch("httpx.Client", side_effect=AssertionError("No runtime download")):
            self.assertEqual(estimate_tokens("dashscope", "qwen-plus", "只输出合法 JSON。"), 5)
            self.assertEqual(estimate_tokens("dashscope", "qwen-turbo", '只返回 {"target":2}'), 7)

    async def test_dashscope_reserves_only_the_payload_it_actually_sends(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test"}, clear=True):
            router = LLMRouter()
            entry = router.registry.get("dashscope:qwen-plus")
            _, _, meta = await router._before_call(
                entry, entry.key, {"action": "vote", "options": [2]}, "只输出合法 JSON。", '只返回 {"target":2}', False
            )
            # DashScope JSON mode does not send the native action schema.
            self.assertEqual(meta["base_input_tokens"], 27)
            self.assertLess(abs(meta["estimated_input_tokens"] / 27 - 1), 0.2)
            await router.close()

    def test_negated_wolf_is_a_reported_good_result_and_does_not_reverse_subjects(self):
        for text in ["我查验了2号，他不是狼。", "查验2号不是狼人。", "验了2号，结果并非狼人。"]:
            memory = update_memory(new_memory(), event(1, 3, text), 1)
            self.assertEqual([(c["target_player_id"], c["result"]) for c in memory["check_claims"]], [(2, "good")])
            self.assertFalse(memory["check_claims"][0]["confirmed"])
        memory = update_memory(new_memory(), event(1, 3, "查验2号是狼人，我不是狼人。"), 1)
        self.assertEqual(memory["check_claims"][0]["result"], "wolf")
        memory = update_memory(new_memory(), event(1, 3, "我验了2号是狼，3号是我的金水。"), 1)
        self.assertEqual(
            [(c["target_player_id"], c["result"]) for c in memory["check_claims"]], [(2, "wolf"), (3, "good")]
        )

    def test_quoting_another_player_does_not_create_a_check_or_role_by_the_speaker(self):
        memory = update_memory(new_memory(), event(1, 3, "我是预言家，查验2号不是狼。"), 1)
        for seq, text in enumerate(
            ["3号说验了2号，但没公布结果。", "3号查验2号是好人，请说明依据。", "3号说“我是预言家”，不能直接确认。"], 2
        ):
            memory = update_memory(memory, event(seq, 5, text), 1)
        self.assertTrue(all(c["player_id"] == 3 for c in memory["check_claims"]))
        self.assertTrue(all(c["player_id"] == 3 for c in memory["claims"]))
        self.assertFalse(any(c["player_id"] == 5 for c in memory["contradictions"]))

    def test_complex_history_fits_small_budgets_and_durable_memory_stays_intact(self):
        _, _, view = fixture()
        memory = new_memory()
        roles = ["村民", "预言家", "女巫"]
        for seq in range(1, 601):
            pid = seq % 6 + 1
            target = (seq // 6) % 6 + 1
            day = (seq - 1) // 20 + 1
            text = f"我是{roles[(seq // 36) % 3]}，查杀{target}号，支持{(target + 1) % 6 + 1}号，怀疑{(target + 2) % 6 + 1}号。"
            memory = update_memory(memory, event(seq, pid, text, day), 1)
        memory = update_memory(
            memory,
            {
                "type": "private_note",
                "event_id": "own-result",
                "day": 30,
                "audience": "player",
                "player_id": 1,
                "data": {"note": "第30夜查验：2号是狼人。"},
            },
            1,
        )
        view["day"] = 30
        before = deepcopy(memory)
        ai = AIOrchestrator(LLMRouter())
        for limit in [6000, 3000, 1800]:
            with patch.dict(os.environ, {"AI_PROMPT_TOKEN_LIMIT": str(limit)}):
                system, user, _ = ai.context(view, "detective", memory)
            self.assertLessEqual(estimate_tokens("dashscope", "qwen-plus", system + user), limit - 600)
            self.assertTrue(
                any(b["player_id"] == 2 and b.get("confirmed") for b in json.loads(user)["memory"]["beliefs"])
            )
        self.assertEqual(before, memory)

    async def test_seer_may_choose_to_misreport_without_server_rewriting(self):
        game, engine, _ = fixture()
        engine.enter("day_speech", 1)
        view = InformationScope.ai_view(game, 1)
        memory = update_memory(
            new_memory(),
            {
                "type": "private_note",
                "event_id": "own-result",
                "day": 1,
                "audience": "player",
                "player_id": 1,
                "data": {"note": "第1夜查验：2号是狼人。"},
            },
            1,
        )
        router = LLMRouter()
        ai = AIOrchestrator(router)

        async def bad_stream(*args):
            for chunk in ["我查", "验了2号，", "他不是狼。", "请2号解释公开立场。"]:
                yield chunk

        router.speech_stream = bad_stream
        output = "".join([chunk async for chunk in ai.speak(view, "mock", "detective", memory)])
        self.assertNotIn("我已查验2号是狼人。", output)
        self.assertIn("我查验了2号，他不是狼。", output)
        self.assertIn("请2号解释公开立场。", output)
        self.assertLessEqual(len(output), 100)

    async def test_unconfirmed_public_claim_never_gets_a_verified_seer_prefix(self):
        game, engine, _ = fixture()
        game.player(1).role = "villager"
        engine.enter("day_speech", 1)
        view = InformationScope.ai_view(game, 1)
        memory = update_memory(new_memory(), event(1, 3, "我是预言家，查杀2号。"), 1)
        router = LLMRouter()
        ai = AIOrchestrator(router)
        output = "".join([chunk async for chunk in ai.speak(view, "mock", "detective", memory)])
        self.assertNotIn("我是预言家", output)
        self.assertNotIn("我已查验", output)

    async def test_wolf_speech_receives_legal_private_context_and_keeps_model_claims(self):
        game, engine, _ = fixture()
        game.player(1).role = "wolf"
        game.player(6).role = "seer"
        engine.enter("day_speech", 1)
        view = InformationScope.ai_view(game, 1)
        view["wolf_chat"] = [{"text": "private-wolf-plan-sentinel"}]
        memory = update_memory(
            new_memory(),
            {
                "type": "wolf_chat_message",
                "event_id": "private-wolf",
                "day": 1,
                "audience": "wolves",
                "data": {"player_id": 2, "speech": "private-wolf-plan-sentinel，我是狼人。"},
            },
            1,
        )
        captured = []
        router = LLMRouter()
        ai = AIOrchestrator(router)

        async def bad_stream(provider, system, user, ctx):
            captured.append(user)
            for text in ["2号首轮沉默，像是在藏身份。", "3号昨晚没发言，行为可疑。", "平安夜我未刀人。"]:
                yield text

        router.speech_stream = bad_stream
        output = "".join([chunk async for chunk in ai.speak(view, "mock", "detective", memory)])
        self.assertIn("private-wolf-plan-sentinel", captured[0])
        self.assertIn("wolf_teammates", captured[0])
        self.assertNotIn("alignment_probabilities", captured[0])
        self.assertIn("沉默", output)
        self.assertIn("刀人", output)
        self.assertEqual(ai.speech_repairs["filtered_sentences"], 0)
        self.assertEqual(ai.speech_repairs["safe_replacements"], 0)

    async def test_context_tokenization_does_not_block_the_game_event_loop(self):
        _, _, view = fixture()
        router = LLMRouter()
        ai = AIOrchestrator(router)
        original = ai.context
        ticks = []

        def slow_context(*args, **kwargs):
            time.sleep(0.06)
            return original(*args, **kwargs)

        ai.context = slow_context

        async def ticker():
            for _ in range(5):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.005)

        await asyncio.gather(ai.propose(view, "mock", "detective", new_memory()), ticker())
        self.assertLess(ticks[-1] - ticks[0], 0.045)
        await router.close()


if __name__ == "__main__":
    unittest.main()
