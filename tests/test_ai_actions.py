import unittest
from unittest.mock import patch

from app.ai import AIOrchestrator, memory_from, normalize_action
from app.game import WerewolfGame
from app.llm import LLMResult, LLMRouter
from app.rules import RuleEngine
from app.scope import InformationScope


class ActionNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_response_shapes_and_explicit_abstention(self):
        responses = [
            ({"vote": 3}, 3),
            ({"vote": {"target": 5}}, 5),
            ({"action": {"type": "vote", "target": 4}}, 4),
            ({"target": None}, None),
            ({"vote": {"target": None}}, None),
            ({"target": "3"}, 3),
        ]
        game = WerewolfGame("normalize", "owner")
        engine = RuleEngine(game)
        engine.join("owner", "玩家", 1)
        engine.start("owner")
        engine.enter("day_vote")
        view = InformationScope.ai_view(game, 1)
        router = LLMRouter()
        ai = AIOrchestrator(router)
        for response, expected in responses:

            async def answer(*args, **kwargs):
                return LLMResult(response, "dashscope", "qwen-plus")

            with patch.object(router, "ask_json", answer):
                proposal = await ai.propose(view, "dashscope", "commander", memory_from(view))
            self.assertEqual(proposal["target"], expected)
            self.assertEqual(proposal["action"], "vote")
            self.assertEqual(set(proposal), {"action", "game_id", "turn_sequence", "turn_id", "target"})
            engine.validate(1, proposal)

    def test_night_action_envelopes(self):
        self.assertEqual(normalize_action("wolf_kill", {"wolf_kill": {"target": 2}}), {"target": 2})
        self.assertEqual(normalize_action("seer_inspect", {"action": {"target": 3}}), {"target": 3})
        self.assertEqual(
            normalize_action("witch", {"action": {"save": True, "poison_target": None}, "questions": ["私密分析"]}),
            {"save": True, "poison_target": None},
        )
        self.assertEqual(normalize_action("vote", {"save": True, "poison_target": 2}), {})

    def test_only_expected_action_fields_survive(self):
        self.assertEqual(normalize_action("vote", {"vote": 6, "speech": "多余发言", "save": True}), {"target": 6})
        self.assertEqual(
            normalize_action("witch", {"save": False, "poison_target": 3, "vote": 2}),
            {"save": False, "poison_target": 3},
        )


if __name__ == "__main__":
    unittest.main()
