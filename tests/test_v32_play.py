"""New roles use real engine actions, dawn/death hooks and scoped views."""

import json
import unittest
from collections import Counter

from app.boards import configuration, framework, generate_board
from app.game import WerewolfGame
from app.llm import LLMRouter
from app.rng import GameRNG
from app.roles import ROLE_DEFINITIONS, ROLE_TAGS
from app.scope import InformationScope
from tests.test_v3_rules import fixture, submit


class NewRoleTests(unittest.TestCase):
    def test_dream_protects_against_knife_and_poison(self):
        for cause in ("knife", "poison"):
            with self.subTest(cause=cause):
                g, e = fixture(["dreamer", "wolf", "wolf", "witch", "villager", "villager", "seer"])
                e.enter("night_dreamer", now=100)
                submit(e, 1, target=5)
                e.enter("night_witch", now=100)
                if cause == "knife":
                    g.night_kill = 5
                else:
                    g.player(4).role_state["poison_target"] = 5
                e.resolve_dawn(101)
                self.assertTrue(g.player(5).alive)
                self.assertEqual(g.player(1).role_state["dream_target"], 5)

    def test_consecutive_dream_kills_but_skip_resets_streak(self):
        for skipped in (False, True):
            g, e = fixture(["dreamer", "wolf", "wolf", "witch", "villager", "villager", "seer"])
            e.enter("night_dreamer", now=100)
            submit(e, 1, target=5)
            if skipped:
                g.day += 1
                e.enter("night_dreamer", now=100)
                submit(e, 1, target=None)
            g.day += 1
            e.enter("night_dreamer", now=100)
            submit(e, 1, target=5)
            e.enter("night_witch", now=100)
            e.resolve_dawn(101)
            self.assertEqual(g.player(5).alive, skipped)

    def test_dreamer_night_death_chain_does_not_fire_hunter(self):
        g, e = fixture(["dreamer", "wolf", "wolf", "hunter", "villager", "villager", "seer"])
        e.enter("night_dreamer", now=100)
        submit(e, 1, target=4)
        g.night_kill = 1
        e.enter("night_witch", now=100)
        e.resolve_dawn(101)
        self.assertFalse(g.player(1).alive)
        self.assertFalse(g.player(4).alive)
        self.assertEqual(g.death_skill_queue, [])
        self.assertNotEqual(g.phase, "death_skill")

    def test_day_death_does_not_chain_dream_target(self):
        g, e = fixture(["dreamer", "wolf", "wolf", "witch", "villager", "villager", "seer"])
        g.player(1).role_state.update(dream_target=5, dream_day=1)
        e.enter("day_vote", now=100)
        e.kill_player(1, cause="vote")
        self.assertTrue(g.player(5).alive)

    def test_gravekeeper_only_checks_uninspected_dead_with_private_true_faction(self):
        g, e = fixture(["gravekeeper", "hidden_wolf", "wolf", "witch", "villager", "villager", "seer"])
        g.player(2).alive = False
        e.enter("night_grave", now=100)
        self.assertEqual(e.action_for(1)["options"], [2])
        with self.assertRaises(ValueError):
            submit(e, 1, target=3)
        submit(e, 1, target=2)
        self.assertIn("狼人阵营", g.player(1).private_notes[-1])
        other = InformationScope.player_view(g, 5)
        self.assertNotIn("守墓查验", json.dumps(other, ensure_ascii=False))
        e.enter("night_grave", now=102)
        self.assertNotEqual(g.phase, "night_grave")

    def test_crow_breaks_vote_tie_even_after_night_death_and_no_identity_leak(self):
        g, e = fixture(["crow", "wolf", "wolf", "witch", "villager", "villager", "seer"])
        e.enter("night_crow", now=100)
        submit(e, 1, target=2)
        g.player(1).alive = False
        e.enter("day_vote", now=100)
        g.votes = {"2": 3, "3": 2, "4": None, "5": None, "6": None, "7": None}
        e.resolve_vote(101)
        self.assertFalse(g.player(2).alive)
        bonus = next(event for event in g.events if "bonus_votes" in event)
        self.assertIsNone(bonus["player_id"])
        self.assertEqual(bonus["bonus_votes"], {2: 1})

    def test_crow_repeated_target_rejected_and_old_mark_does_not_vote(self):
        g, e = fixture(["crow", "wolf", "wolf", "witch", "villager", "villager", "seer"])
        e.enter("night_crow", now=100)
        submit(e, 1, target=2)
        g.day += 1
        e.enter("night_crow", now=102)
        self.assertNotIn(2, e.action_for(1)["options"])
        with self.assertRaises(ValueError):
            submit(e, 1, target=2)
        submit(e, 1, target=None)
        e.enter("day_vote", now=103)
        g.votes = {"1": 2, "2": 3, "3": None, "4": None, "5": None, "6": None, "7": None}
        e.resolve_vote(104)
        self.assertTrue(g.player(2).alive)

    def test_bear_neighbors_skip_dead_wrap_and_detect_hidden_wolf(self):
        for wolf_alive, bear_alive in ((True, True), (False, True), (True, False)):
            g, e = fixture(["bear_trainer", "villager", "hidden_wolf", "wolf", "witch", "seer", "villager"])
            g.player(2).alive = False
            g.player(3).alive = wolf_alive
            g.player(4).alive = False
            g.player(1).alive = bear_alive
            e.enter("night_witch", now=100)
            e.resolve_dawn(101)
            growls = [event for event in g.events if "bear_growled" in event]
            self.assertEqual(len(growls), int(bear_alive))
            if bear_alive:
                self.assertEqual(growls[0]["bear_growled"], wolf_alive)
                self.assertIsNone(growls[0]["player_id"])

    def test_new_actions_accept_model_chosen_targets_and_have_complete_rules(self):
        for action in ("dream_visit", "grave_inspect", "crow_mark"):
            self.assertTrue(LLMRouter._valid_data({"target": 12}, {"action": action, "options": [12]}))
            self.assertTrue(LLMRouter._valid_data({action: {"target": 12}}, {"action": action, "options": [12]}))
            self.assertFalse(LLMRouter._valid_data({"target": "fake"}, {"action": action, "options": [12]}))
        for key in ("dreamer", "gravekeeper", "crow", "bear_trainer"):
            self.assertTrue(ROLE_DEFINITIONS[key].rules)
            self.assertTrue(ROLE_TAGS[key])


class BoardTests(unittest.TestCase):
    def test_random_boards_obey_constraints_across_seeds_and_sizes(self):
        for count in (6, 9, 12, 16):
            frame = framework(count)
            for seed in range(30):
                roles = generate_board(count, list(ROLE_DEFINITIONS), GameRNG(seed))
                self.assertEqual(len(roles), count)
                self.assertEqual(sum(ROLE_DEFINITIONS[r].faction == "wolves" for r in roles), frame["wolves"])
                self.assertGreaterEqual(roles.count("villager"), frame["min_villagers"])
                self.assertTrue(any("信息型" in ROLE_TAGS[r] for r in roles))
                self.assertTrue(any("保护型" in ROLE_TAGS[r] for r in roles))
                self.assertLessEqual(sum("强狼" in ROLE_TAGS[r] for r in roles), 1)

    def test_custom_pool_and_impossible_pool_reject_without_mutation(self):
        pool = ["wolf", "seer", "witch", "villager"]
        roles = generate_board(9, pool, GameRNG(18))
        self.assertTrue(set(roles) <= set(pool))
        for pool in (["wolf", "seer", "villager"], ["hidden_wolf", "seer", "witch", "villager"], ["unknown"]):
            with self.assertRaises(ValueError):
                configuration("quick6", 6, None, "custom_random", pool)

    def test_draw_is_seeded_and_actual_roster_hidden_before_and_during_game(self):
        _, count, roster, pool = configuration("quick6", 6, None, "constrained_random", None)
        g = WerewolfGame(
            "random",
            "host",
            mode="custom",
            player_count=count,
            role_roster=roster,
            board_policy="constrained_random",
            random_role_pool=pool,
            game_seed=77,
            experiment_id="random-test",
        )
        from app.rules import RuleEngine

        e = RuleEngine(g)
        e.join("host", "主人", 1)
        for started in (False, True):
            if started:
                e.start("host", 100)
                self.assertEqual(Counter(g.role_roster), Counter(generate_board(count, pool, GameRNG(77))))
            view = InformationScope.player_view(g, 1)
            self.assertEqual(view["role_roster"], [])
            self.assertEqual(view["game_mode"]["roles"], [])
            self.assertEqual(view["board_framework"]["wolves"], 2)
            if started:
                self.assertEqual(view["phase"], "night")
        restored = WerewolfGame.restore(g.dump())
        self.assertEqual(restored.dump(), g.dump())
        e.finish("draw", 101)
        self.assertEqual(InformationScope.player_view(g, 1)["role_roster"], g.role_roster)
        e.rematch("host")
        self.assertEqual(g.board_policy, "constrained_random")
        self.assertEqual(g.random_role_pool, pool)


class AutonomousGames(unittest.IsolatedAsyncioTestCase):
    async def test_mock_agents_complete_new_fixed_and_random_boards(self):
        import os
        from unittest.mock import patch

        from app.ai import AIOrchestrator
        from app.rules import RuleEngine

        with patch.dict(os.environ, {"AI_CHUNK_DELAY": "0"}):
            ai = AIOrchestrator(LLMRouter())
            for mode in ("wolfking12", "beautyknight12", "hidden12", "advanced9", "fun6", "special12", "random"):
                if mode == "random":
                    _, count, roster, pool = configuration("standard12", 12, None, "constrained_random", None)
                    g = WerewolfGame(
                        mode,
                        "host",
                        mode="custom",
                        player_count=count,
                        role_roster=roster,
                        board_policy="constrained_random",
                        random_role_pool=pool,
                    )
                else:
                    g = WerewolfGame(mode, "host", mode=mode)
                e = RuleEngine(g)
                e.join("host", "主人", 1)
                e.start("host", 100)
                for step in range(600):
                    if g.game_over:
                        break
                    actors = e.required_actors()
                    if not actors:
                        e.tick(now=1000 + step * 100)
                        continue
                    for pid in sorted(actors):
                        view = InformationScope.player_view(g, pid)
                        pending = view["pending_action"]
                        if not pending:
                            continue
                        p = g.player(pid)
                        if pending["type"] == "speech":
                            text = "".join([chunk async for chunk in ai.speak(view, "mock", p.personality, p.memory)])
                            payload = {
                                "action": "speech",
                                "speech": text,
                                "game_id": g.game_id,
                                "turn_sequence": g.turn_sequence,
                            }
                        else:
                            payload = await ai.propose(view, "mock", p.personality, p.memory)
                        e.apply(pid, payload, now=100.1 + step * 0.01)
                self.assertTrue(g.game_over, (mode, g.phase, g.day))
                self.assertIn(g.winner, {"wolves", "good", "draw"})
