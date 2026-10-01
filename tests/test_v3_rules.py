"""V3 authoritative rules, role effects, early completion and scoped visibility."""
import json
import unittest
from collections import Counter
from unittest.mock import patch

from app.game import WerewolfGame
from app.roles import GAME_MODES, ROLE_DEFINITIONS
from app.rules import RuleEngine
from app.scope import InformationScope


def fixture(roles=None, mode='quick6'):
    kwargs = {'mode': mode}
    if roles:
        kwargs.update(mode='custom', player_count=len(roles), role_roster=roles)
    game = WerewolfGame('rules-v3', 'host', **kwargs)
    engine = RuleEngine(game)
    engine.join('host', '房主', 1)
    engine.start('host', now=100)
    if roles:
        for player, role in zip(game.players, roles):
            player.role = role
            player.role_state = {'antidote': True, 'poison': True} if role == 'witch' else {}
    return game, engine


def submit(engine, pid, now=101, **values):
    game = engine.g
    pending = engine.action_for(pid)
    payload = {'action': pending['type'], 'game_id': game.game_id,
               'turn_id': game.turn_id, 'turn_sequence': game.turn_sequence, **values}
    return engine.apply(pid, payload, now=now)


def secondary(engine, pid, action, target, now=101):
    game = engine.g
    return engine.apply(pid, {'action': action, 'target': target,
                             'game_id': game.game_id, 'turn_sequence': game.turn_sequence}, now=now)


class V3RulesTests(unittest.TestCase):
    def test_modes_deal_all_seats_and_restore_without_changing_mode(self):
        for mode, definition in GAME_MODES.items():
            with self.subTest(mode=mode):
                game, engine = fixture(mode=mode)
                self.assertEqual(game.player_count, definition.player_count)
                self.assertEqual(len(game.players), definition.player_count)
                self.assertEqual(Counter(p.role for p in game.players), Counter(definition.roles))
                restored = WerewolfGame.restore(game.dump())
                self.assertEqual(restored.dump(), game.dump())
                engine.finish('draw', now=102)
                engine.rematch('host')
                self.assertEqual(game.mode, mode)
                self.assertEqual(game.player_count, definition.player_count)
                self.assertEqual(len(game.players), 1)

    def test_join_random_available_and_change_seat(self):
        game = WerewolfGame('random', 'host', mode='standard12')
        engine = RuleEngine(game)
        with patch('app.rules.random.SystemRandom.choice', side_effect=lambda choices: choices[-1]):
            self.assertEqual(engine.join('host', '甲').id, 12)
            self.assertEqual(engine.join('b', '乙').id, 11)
            self.assertEqual(engine.change_seat('host').id, 10)
        with self.assertRaises(ValueError):
            engine.change_seat('host', 11)
        with self.assertRaises(ValueError):
            engine.join('c', '丙', 13)
        self.assertEqual(game.owned_player('host').id, 10)

    def test_custom_configuration_rejects_invalid_roster_atomically(self):
        game = WerewolfGame('custom', 'host')
        engine = RuleEngine(game)
        engine.join('host', '甲', 1)
        before = game.dump()
        for roles in [['wolf', 'wolf', 'villager', 'villager'], ['unknown']*6, ['hidden_wolf', 'villager', 'villager', 'villager']]:
            with self.assertRaises(ValueError):
                engine.configure('host', 'fast', [], mode='custom', player_count=len(roles), roles=roles)
            self.assertEqual(before, game.dump())
        roles = ['wolf', 'hunter', 'knight', 'idiot', 'guard', 'villager', 'villager']
        engine.configure('host', 'fast', [], mode='custom', player_count=7, roles=roles)
        self.assertEqual(game.player_count, 7)
        engine.start('host', now=100)
        self.assertEqual(Counter(p.role for p in game.players), Counter(roles))

    def test_malformed_configuration_types_are_rejected_atomically(self):
        game = WerewolfGame('malformed', 'host')
        engine = RuleEngine(game)
        engine.join('host', '甲', 1)
        before = game.dump()
        for entries in [[{'id': []}], [{'id': True}], ['bad'],
                        [{'id': 2, 'credential_id': []}],
                        [{'id': 2, 'model_id': 7}], [{'id': 2, 'model_options': []}]]:
            with self.assertRaises(ValueError):
                engine.configure('host', 'fast', entries)
            self.assertEqual(game.dump(), before)
        for changes in [{'roles': 3}, {'player_count': 6.0}]:
            with self.assertRaises(ValueError):
                engine.configure('host', 'fast', [], **changes)
            self.assertEqual(game.dump(), before)

    def test_completed_night_actions_advance_without_waiting(self):
        game, engine = fixture(['wolf', 'wolf', 'seer', 'witch', 'villager', 'villager'])
        deadline = game.turn_deadline
        submit(engine, 1)
        self.assertEqual(game.phase, 'night_discussion')
        submit(engine, 2)
        self.assertEqual(game.phase, 'night_wolves')
        self.assertLess(101, deadline)
        submit(engine, 1, target=5)
        submit(engine, 2, target=5)
        self.assertEqual(game.phase, 'night_seer')
        submit(engine, 3, target=1)
        self.assertEqual(game.phase, 'night_witch')
        self.assertIn('1号是狼人', game.player(3).private_notes[-1])
        submit(engine, 4, save=True)
        self.assertEqual(game.phase, 'day_speech')
        self.assertTrue(game.player(5).alive)

    def test_no_action_roles_skip_entire_unused_night(self):
        game, engine = fixture(['wolf', 'villager', 'villager', 'villager'])
        submit(engine, 1)
        self.assertEqual(game.phase, 'night_wolves')
        submit(engine, 1, target=None)
        self.assertEqual(game.phase, 'day_speech')
        self.assertLess(game.turn_started_at, 110)

    def test_multiple_seers_all_must_complete(self):
        game, engine = fixture(['wolf', 'seer', 'seer', 'villager', 'villager'])
        engine.enter('night_seer', now=100)
        submit(engine, 2, target=1)
        self.assertEqual(game.phase, 'night_seer')
        submit(engine, 3, target=4)
        self.assertEqual(game.phase, 'day_speech')
        self.assertNotIn('4号是好人', json.dumps(InformationScope.player_view(game, 2), ensure_ascii=False))

    def test_guard_targets_and_knife_block_but_poison_bypasses(self):
        game, engine = fixture(['wolf', 'guard', 'witch', 'hunter', 'villager', 'villager'])
        engine.enter('night_guard', now=100)
        submit(engine, 2, target=4)
        self.assertEqual(game.phase, 'night_wolves')
        submit(engine, 1, target=4)
        self.assertEqual(game.phase, 'night_witch')
        submit(engine, 3, poison_target=4)
        self.assertFalse(game.player(4).alive)
        self.assertEqual(game.player(4).role_state['death_cause'], 'poison')
        self.assertNotEqual(game.phase, 'death_skill')
        engine.enter('night_guard', now=102)
        self.assertNotIn(4, engine.action_for(2)['options'])
        self.assertIn(2, engine.action_for(2)['options'])

    def test_guard_cannot_repeat_and_timeout_releases_previous_target(self):
        game, engine = fixture(['wolf', 'guard', 'villager', 'villager'])
        engine.enter('night_guard', now=100)
        submit(engine, 2, target=3)
        engine.enter('night_guard', now=102)
        with self.assertRaises(ValueError):
            submit(engine, 2, now=103, target=3)
        engine.tick(game.turn_deadline)
        engine.enter('night_guard', now=200)
        self.assertIn(3, engine.action_for(2)['options'])

    def test_hunter_death_skill_defers_win_and_can_reverse_result(self):
        game, engine = fixture(['wolf', 'hunter', 'villager', 'villager'])
        game.player(3).alive = False
        game.player(4).alive = False
        game.night_kill = 2
        engine.resolve_dawn(now=101)
        self.assertFalse(game.game_over)
        self.assertEqual(game.phase, 'death_skill')
        self.assertEqual(engine.action_for(2)['type'], 'hunter_shoot')
        self.assertIsNone(engine.action_for(1))
        submit(engine, 2, now=102, target=1)
        self.assertTrue(game.game_over)
        self.assertEqual(game.winner, 'good')

    def test_wolf_king_death_skill_and_chained_hunter(self):
        game, engine = fixture(['wolf_king', 'wolf', 'hunter', 'villager', 'villager', 'villager'])
        game.death_context = {'after': 'next_night', 'dead': []}
        engine.kill_player(1, cause='vote')
        engine.continue_deaths(now=100)
        self.assertEqual(engine.action_for(1)['type'], 'wolf_king_shoot')
        submit(engine, 1, target=3)
        self.assertEqual(game.phase, 'death_skill')
        self.assertEqual(engine.action_for(3)['type'], 'hunter_shoot')
        submit(engine, 3, target=2)
        self.assertEqual(game.winner, 'good')

    def test_knight_duel_is_once_and_resumes_day(self):
        game, engine = fixture(['wolf', 'wolf', 'knight', 'villager', 'villager', 'villager'])
        engine.enter('day_speech', 4, now=100)
        game.speech_queue = [5, 6, 1, 2, 3]
        self.assertTrue(secondary(engine, 3, 'duel', 1))
        self.assertFalse(game.player(1).alive)
        self.assertTrue(game.player(3).alive)
        self.assertEqual(game.phase, 'last_words')
        submit(engine, 1, speech='遗言')
        self.assertEqual(game.current_turn_player_id, 4)
        self.assertEqual(engine.secondary_actions(3), [])
        with self.assertRaises(ValueError):
            secondary(engine, 3, 'duel', 2)

    def test_knight_wrong_duel_kills_knight(self):
        game, engine = fixture(['wolf', 'knight', 'villager', 'villager'])
        engine.enter('day_speech', 2, now=100)
        game.speech_queue = [3, 4, 1]
        secondary(engine, 2, 'duel', 3)
        self.assertFalse(game.player(2).alive)
        self.assertTrue(game.player(3).alive)
        submit(engine, 2, speech='遗言')
        self.assertEqual(game.current_turn_player_id, 3)

    def test_white_wolf_self_destruct_ends_day(self):
        game, engine = fixture(['white_wolf_king', 'wolf', 'villager', 'villager', 'villager', 'villager'])
        engine.enter('day_speech', 5, now=100)
        secondary(engine, 1, 'self_destruct', 3)
        self.assertFalse(game.player(1).alive)
        self.assertFalse(game.player(3).alive)
        self.assertEqual(game.phase, 'last_words')
        submit(engine, 1, speech='遗言')
        submit(engine, 3, speech='遗言')
        self.assertEqual(game.day, 2)
        self.assertEqual(game.phase, 'night_discussion')

    def test_idiot_first_vote_reveals_and_removes_vote_right(self):
        game, engine = fixture(['wolf', 'idiot', 'villager', 'villager'])
        engine.enter('day_vote', now=100)
        for pid, target in [(1, 2), (2, 1), (3, 2), (4, 2)]:
            submit(engine, pid, target=target)
        self.assertTrue(game.player(2).alive)
        self.assertTrue(game.player(2).role_state['vote_disabled'])
        self.assertEqual(InformationScope.player_view(game, 3)['players'][1]['role'], '白痴')
        engine.enter('day_vote', now=102)
        self.assertIsNone(engine.action_for(2))
        self.assertEqual(engine.required_actors(), [1, 3, 4])
        for pid in [1, 3, 4]:
            submit(engine, pid, now=103, target=2)
        self.assertFalse(game.player(2).alive)

    def test_wolf_beauty_charm_and_death_no_hunter_shot(self):
        game, engine = fixture(['wolf_beauty', 'wolf', 'hunter', 'villager', 'villager', 'villager'])
        engine.enter('night_beauty', now=100)
        submit(engine, 1, target=3)
        game.death_context = {'after': 'next_night', 'dead': []}
        engine.kill_player(1, cause='vote')
        self.assertFalse(game.player(3).alive)
        self.assertEqual(game.player(3).role_state['death_cause'], 'charm')
        self.assertEqual(game.death_skill_queue, [])
        self.assertEqual(game.death_context['dead'], [1, 3])

    def test_hidden_wolf_seer_disguise_visibility_and_awakening(self):
        game, engine = fixture(['wolf', 'hidden_wolf', 'seer', 'villager', 'villager', 'villager'])
        wolf = InformationScope.player_view(game, 1)
        hidden = InformationScope.player_view(game, 2)
        self.assertEqual(wolf['wolf_teammates'], [])
        self.assertEqual(hidden['wolf_teammates'], [{'id': 1, 'alive': True}])
        self.assertNotIn('wolf_chat', hidden)
        engine.wolf_message(1, '觉醒前的秘密')
        engine.enter('night_seer', now=100)
        submit(engine, 3, target=2)
        self.assertIn('2号是好人', game.player(3).private_notes[-1])
        engine.kill_player(1, cause='vote')
        engine.enter('night_discussion', now=102)
        self.assertEqual(engine.action_for(2)['type'], 'wolf_discuss')
        engine.wolf_message(2, '觉醒后的秘密')
        now_hidden = InformationScope.player_view(game, 2)
        self.assertNotIn('觉醒前的秘密', json.dumps(now_hidden, ensure_ascii=False))
        self.assertIn('觉醒后的秘密', json.dumps(now_hidden, ensure_ascii=False))
        submit(engine, 2, now=103)
        self.assertEqual(engine.action_for(2)['type'], 'wolf_kill')

    def test_credentials_and_hidden_skill_history_never_leak(self):
        game = WerewolfGame('credentials', 'host')
        engine = RuleEngine(game)
        engine.join('host', '房主', 1)
        engine.join('visitor', '访客', 2)
        engine.configure('host', 'fast', [{'id': 3, 'provider': 'openai_compatible',
                         'credential_id': 'private-credential-id', 'credential_owner_id': 'host',
                         'model_id': 'test-model', 'model_options': {'temperature': 0.7}}])
        self.assertNotIn('private-credential-id', json.dumps(InformationScope.owner_view(game, 'visitor')))
        self.assertIn('private-credential-id', json.dumps(InformationScope.owner_view(game, 'host')))
        self.assertNotIn('credential_owner_id', json.dumps(InformationScope.owner_view(game, 'host')))
        engine.start('host', now=100)
        for player, role in zip(game.players, ['villager', 'villager', 'seer', 'wolf', 'wolf', 'witch']):
            player.role, player.role_state = role, {}
        engine.enter('night_seer', now=100)
        submit(engine, 3, target=4)
        visitor = InformationScope.owner_view(game, 'visitor')
        self.assertNotIn('action_history', visitor)
        self.assertNotIn('4号是狼人', json.dumps(visitor, ensure_ascii=False))
        self.assertIsNone(visitor['current_turn_player_id'])
        self.assertEqual(game.action_history[-1]['action'], 'seer_inspect')

    def test_witch_double_potion_invalid_and_skip_after_used_up(self):
        game, engine = fixture(['wolf', 'witch', 'villager', 'villager'])
        game.night_kill = 3
        engine.enter('night_witch', now=100)
        before = game.dump()
        with self.assertRaises(ValueError):
            submit(engine, 2, save=True, poison_target=1)
        self.assertEqual(game.dump(), before)
        game.player(2).role_state.update(antidote=False, poison=False)
        engine.enter('night_witch', now=102)
        self.assertEqual(game.phase, 'last_words')

    def test_every_catalogued_role_has_real_rules_and_skill_hooks(self):
        self.assertEqual(len(ROLE_DEFINITIONS), 12)
        for key, definition in ROLE_DEFINITIONS.items():
            self.assertEqual(key, definition.key)
            self.assertTrue(definition.rules)
            self.assertIn(definition.faction, {'good', 'wolves'})
            self.assertTrue(callable(definition.legal_targets))
            self.assertTrue(callable(definition.private_information))
        for key in ['hunter', 'wolf_king', 'wolf_beauty']:
            self.assertTrue(callable(ROLE_DEFINITIONS[key].death_trigger))


if __name__ == '__main__':
    unittest.main()
