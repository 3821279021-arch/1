"""V3 checks strategy autonomy separately from legality and information scope."""

import asyncio
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from app.ai import AIOrchestrator, normalize_action
from app.llm import LLMResult
from app.memory import new_memory, update_memory


def view(role="villager", count=12, action="vote"):
    return {
        "room_id": "test-room",
        "game_id": "test-game",
        "turn_id": "test-turn",
        "turn_sequence": 3,
        "day": 1,
        "phase": "day_vote",
        "game_over": False,
        "player_count": count,
        "mode": "standard12",
        "role_roster": ["wolf"] * 4 + ["seer", "witch", "hunter", "guard"] + ["villager"] * 4,
        "rules": "狼人达到人数优势获胜；守卫不能连续守同一目标。",
        "self": {"id": 1, "role": role, "role_key": role, "alive": True, "private_notes": [], "role_state": {}},
        "players": [{"id": i, "alive": True, "role": role if i == 1 else None} for i in range(1, count + 1)],
        "events": [],
        "pending_action": {
            "type": action,
            "options": list(range(2, count + 1)),
            "game_id": "test-game",
            "turn_id": "test-turn",
            "turn_sequence": 3,
        },
    }


class Model:
    def __init__(self, replies=None, chunks=None):
        self.replies = list(replies or [])
        self.chunks = chunks or []
        self.calls = []

    async def ask_json(self, provider, system, user, *, mock_context, validator=None):
        self.calls.append((system, user, deepcopy(mock_context)))
        data = self.replies.pop(0)
        if isinstance(data, Exception):
            raise data
        if validator:
            self.last_valid = validator(data)
        return LLMResult(data, provider, "independent-model")

    async def speech_stream(self, provider, system, user, ctx):
        self.calls.append((system, user, deepcopy(ctx)))
        for chunk in self.chunks:
            yield chunk


class AutonomousModels(unittest.IsolatedAsyncioTestCase):
    async def test_models_keep_different_legal_votes_even_with_legacy_server_mode(self):
        scoped = view()
        first, second = Model([{"target": 10}]), Model([{"target": 12}])
        with patch.dict("os.environ", {"AI_STRATEGY_MODE": "server"}):
            a = await AIOrchestrator(first).propose(scoped, "model-a", "detective", new_memory())
            b = await AIOrchestrator(second).propose(scoped, "model-b", "detective", new_memory())
        self.assertEqual((a["target"], b["target"]), (10, 12))
        for model in (first, second):
            system, user, metadata = model.calls[0]
            payload = json.loads(user.split("\n", 1)[0])
            self.assertNotIn("服务器已决定", system + user)
            self.assertNotIn("belief_state", payload)
            self.assertNotIn("action_decision", payload)
            self.assertNotIn("expression_plan", payload)
            self.assertNotIn("decision", metadata)

    async def test_player_may_bluff_lie_accuse_and_change_stance_in_public(self):
        scoped = view(action="speech")
        scoped["phase"] = "day_speech"
        speech = "我是预言家，我查验了12号是狼人。我之前支持10号，现在我要投10号。"
        model = Model(chunks=[speech[:9], speech[9:17], speech[17:]])
        ai = AIOrchestrator(model)
        output = "".join([chunk async for chunk in ai.speak(scoped, "real", "trickster", new_memory())])
        self.assertEqual(output, speech)
        self.assertEqual(ai.speech_repairs["filtered_sentences"], 0)

    async def test_real_seer_can_keep_or_misreport_own_check_without_server_prefix(self):
        scoped = view(role="seer", action="speech")
        scoped["phase"] = "day_speech"
        scoped["self"]["private_notes"] = ["第1夜查验：12号是狼人。"]
        memory = update_memory(
            new_memory(),
            {
                "type": "private_note",
                "event_id": "my-check",
                "day": 1,
                "audience": "player",
                "player_id": 1,
                "data": {"note": scoped["self"]["private_notes"][0]},
            },
            1,
        )
        model = Model(chunks=["我是村民，12号是我的金水。"])
        output = "".join([chunk async for chunk in AIOrchestrator(model).speak(scoped, "real", "detective", memory)])
        self.assertEqual(output, "我是村民，12号是我的金水。")
        payload = json.loads(model.calls[0][1])
        self.assertIn("第1夜查验：12号是狼人。", json.dumps(payload, ensure_ascii=False))

    async def test_wolf_uses_legal_private_team_context_and_chooses_disclosure(self):
        scoped = view(role="wolf", action="speech")
        scoped["wolf_teammates"] = [{"id": 9, "alive": True}]
        scoped["wolf_chat"] = [{"player_id": 9, "text": "队内约定：明天9号跳预言家。"}]
        model = Model(chunks=["我支持9号预言家。"])
        output = "".join(
            [chunk async for chunk in AIOrchestrator(model).speak(scoped, "real", "detective", new_memory())]
        )
        self.assertEqual(output, "我支持9号预言家。")
        self.assertIn("队内约定", model.calls[0][1])

    def test_prompt_contains_history_without_server_guesses_or_credentials(self):
        scoped = view()
        scoped["self"].update(
            credential_id="credential-reference", encrypted_secret="secret-key", model_id="secret-model"
        )
        scoped["seat_presets"] = {"2": {"credential_id": "another-reference"}}
        scoped["pet"] = {"private_chat_history": ["another-owner-secret"]}
        memory = update_memory(
            new_memory(),
            {
                "type": "speech",
                "event_id": "claim",
                "audience": "public",
                "data": {"player_id": 11, "speech": "我是守卫，怀疑12号。"},
            },
            1,
        )
        before_view, before_memory = deepcopy(scoped), deepcopy(memory)
        system, user, metadata = AIOrchestrator(Model()).context(scoped, "detective", memory)
        payload = json.loads(user)
        self.assertEqual(payload["memory"]["claims"][0]["claimed_role"], "guard")
        self.assertFalse(payload["memory"]["claims"][0]["confirmed"])
        self.assertNotIn("suspicions", payload["memory"])
        self.assertNotIn("semantic_hints", payload["memory"])
        self.assertFalse(payload["memory"]["beliefs"])
        for secret in (
            "credential-reference",
            "secret-key",
            "secret-model",
            "another-reference",
            "another-owner-secret",
        ):
            self.assertNotIn(secret, system + user)
        self.assertEqual(metadata["_model_route"]["model_id"], "secret-model")
        self.assertEqual((scoped, memory), (before_view, before_memory))
        self.assertIn("12人狼人杀", system)
        self.assertNotIn("没有守卫", system)

    async def test_extra_role_actions_keep_model_target_and_strip_other_actions(self):
        for action in ("guard_protect", "hunter_shoot", "wolf_king_shoot", "wolf_beauty_charm"):
            scoped = view(action=action)
            scoped["self"]["alive"] = action not in {"hunter_shoot", "wolf_king_shoot"}
            ai = AIOrchestrator(Model([{action: {"target": "12"}, "vote": 2, "speech": "ignored"}]))
            proposal = await ai.propose(scoped, "real", "detective", new_memory())
            self.assertEqual(proposal["target"], 12)
            self.assertEqual(proposal["action"], action)
            self.assertNotIn("speech", proposal)
            self.assertNotIn("vote", proposal)
        self.assertEqual(normalize_action("unknown_future_role", {"action": {"target": 12}}), {"target": 12})

    async def test_invalid_final_output_and_timeout_have_neutral_traced_fallback(self):
        for reply, reason in [
            ({"target": 99}, "invalid_output_after_router_retries"),
            (TimeoutError(), "TimeoutError"),
        ]:
            model, scoped = Model([reply]), view()
            ai = AIOrchestrator(model)
            proposal = await ai.propose(scoped, "real", "detective", new_memory())
            self.assertIsNone(proposal["target"])
            self.assertEqual(ai.decision_records[-1]["source"], "rule_fallback")
            self.assertEqual(ai.decision_records[-1]["reason"], reason)
            self.assertNotIn("prompt", ai.decision_records[-1])

    async def test_real_router_failure_cannot_turn_practice_strategy_into_a_vote(self):
        class FailedModel(Model):
            async def ask_json(self, *args, **kwargs):
                return LLMResult(
                    {"target": 12},
                    "mock (fallback from real)",
                    "rule-based-mock",
                    routing={"status": "mock_fallback", "failure_reason": "credential_unavailable"},
                )

        ai = AIOrchestrator(FailedModel())
        command = await ai.propose(view(), "real", "detective", new_memory())
        self.assertIsNone(command["target"])
        self.assertEqual(ai.decision_records[-1]["reason"], "credential_unavailable")

    async def test_same_night_double_potion_is_rejected_before_game_engine(self):
        scoped = view(action="witch")
        scoped["pending_action"].update(antidote=True, poison=True, killed=10, can_save=True, can_use_both=False)
        model = Model([{"save": True, "poison_target": 12}])
        command = await AIOrchestrator(model).propose(scoped, "real", "detective", new_memory())
        self.assertFalse(model.last_valid)
        self.assertEqual((command["save"], command["poison_target"]), (False, None))

    def test_foreign_private_and_wolf_memory_cannot_enter_good_player_prompt(self):
        scoped, memory = view(), new_memory()
        memory["facts"] = [
            {"text": "foreign-private-sentinel", "source_player_id": 2, "visibility": "private", "confirmed": True},
            {"text": "wolf-team-sentinel", "source_player_id": 2, "visibility": "wolves", "confirmed": False},
        ]
        memory["summary"]["confirmed_facts"] = [
            {"text": "foreign-summary-sentinel", "source_player_id": 3, "visibility": "private", "confirmed": True}
        ]
        _, user, _ = AIOrchestrator(Model()).context(scoped, "detective", memory)
        for sentinel in ("foreign-private-sentinel", "wolf-team-sentinel", "foreign-summary-sentinel"):
            self.assertNotIn(sentinel, user)

    def test_small_budget_retains_all_twelve_player_private_checks(self):
        from app.game import WerewolfGame
        from app.rules import RuleEngine
        from app.scope import InformationScope
        from app.tokens import estimate_tokens

        game = WerewolfGame("budget", "owner", mode="standard12")
        engine = RuleEngine(game)
        engine.join("owner", "测试", 1)
        engine.start("owner")
        game.player(1).role = "seer"
        scoped, memory = InformationScope.ai_view(game, 1), new_memory()
        for pid in range(2, 13):
            note = f"第{pid}夜查验：{pid}号是好人。"
            scoped["self"]["private_notes"].append(note)
            memory = update_memory(
                memory,
                {
                    "type": "private_note",
                    "event_id": f"check:{pid}",
                    "day": pid,
                    "audience": "player",
                    "player_id": 1,
                    "data": {"note": note},
                },
                1,
            )
        before = deepcopy(memory)
        with patch.dict("os.environ", {"AI_PROMPT_TOKEN_LIMIT": "1800"}):
            system, user, _ = AIOrchestrator(Model()).context(scoped, "detective", memory)
        self.assertLessEqual(estimate_tokens("dashscope", "qwen-plus", system + user), 1200)
        self.assertEqual(len(json.loads(user)["memory"]["beliefs"]), 11)
        self.assertEqual(memory, before)

    async def test_illegal_potion_output_cannot_consume_unavailable_drugs(self):
        scoped = view(action="witch")
        scoped["pending_action"].update(antidote=False, poison=False, killed=None)
        model = Model([{"save": True, "poison_target": 12}])
        proposal = await AIOrchestrator(model).propose(scoped, "real", "detective", new_memory())
        self.assertFalse(model.last_valid)
        self.assertEqual((proposal["save"], proposal["poison_target"]), (False, None))

    async def test_secondary_skill_is_optional_and_fully_model_chosen(self):
        scoped = view(role="knight", action="speech")
        scoped["secondary_actions"] = [{"type": "duel", "options": [10, 12]}]
        ai = AIOrchestrator(
            Model([{"action": None, "target": None}, {"action": "duel", "target": 12}, {"action": "duel", "target": 9}])
        )
        self.assertIsNone(await ai.choose_secondary(scoped, "real", "detective", new_memory()))
        command = await ai.choose_secondary(scoped, "real", "detective", new_memory())
        self.assertEqual(command["target"], 12)
        self.assertEqual(command["action"], "duel")
        self.assertEqual(command["turn_id"], scoped["turn_id"])
        self.assertIsNone(await ai.choose_secondary(scoped, "real", "detective", new_memory()))

    async def test_cancelled_turn_is_never_replaced_with_an_action(self):
        class Cancelled(Model):
            async def ask_json(self, *args, **kwargs):
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await AIOrchestrator(Cancelled()).propose(view(), "real", "detective", new_memory())

    def test_multiple_digit_claims_and_private_checks_remain_distinct(self):
        memory = update_memory(
            new_memory(),
            {
                "type": "speech",
                "event_id": "claim12",
                "audience": "public",
                "data": {"player_id": 11, "speech": "我是预言家，查杀12号。"},
            },
            1,
        )
        self.assertEqual(memory["check_claims"][0]["target_player_id"], 12)
        self.assertFalse(memory["check_claims"][0]["confirmed"])
        memory = update_memory(
            memory,
            {
                "type": "private_note",
                "event_id": "check12",
                "audience": "player",
                "player_id": 1,
                "data": {"note": "查验：12号是好人。"},
            },
            1,
        )
        self.assertTrue(next(item for item in memory["beliefs"] if item["player_id"] == 12)["confirmed"])
        self.assertFalse(any(item["player_id"] == 2 for item in memory["beliefs"]))


if __name__ == "__main__":
    unittest.main()
