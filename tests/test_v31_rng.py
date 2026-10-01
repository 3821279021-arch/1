"""Environment reproducibility survives persistence and process boundaries."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.game import WerewolfGame
from app.persistence import Store
from app.rng import GameRNG, derive_seed
from app.rules import RuleEngine


def layout(seed, count=6):
    game = WerewolfGame("rng", "host", experiment_id="test", game_seed=seed)
    engine = RuleEngine(game)
    seats = [engine.join(str(i), str(i)).id for i in range(count)]
    engine.start("host")
    return seats, [p.role for p in game.players]


class ReproducibilityTests(unittest.TestCase):
    def test_rematch_rebinds_rng_to_the_new_normal_game(self):
        game = WerewolfGame("rematch-rng", "host", experiment_id="seeded", game_seed=99)
        engine = RuleEngine(game)
        engine.join("host", "Host", 1)
        engine.start("host", now=100)
        engine.finish("good", now=101)
        engine.rematch("host")
        self.assertIsNone(game.game_seed)
        self.assertEqual(engine.rng.algorithm, "system-random")
        with patch("app.rng.random.SystemRandom.choice", return_value=6) as secure_choice:
            self.assertEqual(engine.change_seat("host").id, 6)
            secure_choice.assert_called_once()

    def test_same_seed_and_different_seeds(self):
        self.assertEqual(layout(42), layout(42))
        self.assertGreater(len({json.dumps(layout(seed)) for seed in range(20)}), 10)

    def test_secure_normal_mode_and_explicit_context(self):
        with patch("app.rng.random.SystemRandom") as secure:
            secure.return_value.choice.return_value = 4
            game = WerewolfGame("normal", "host")
            self.assertEqual(RuleEngine(game).join("host", "Host").id, 4)
            secure.assert_called_once()
        with self.assertRaises(ValueError):
            WerewolfGame("bad", "host", game_seed=1)
        with self.assertRaises(ValueError):
            WerewolfGame("bad", "host", experiment_id="x", game_seed=True)

    def test_rng_continuation_after_sqlite_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "legacy.sqlite")
            game = WerewolfGame("persist", "host", experiment_id="exp", game_seed="stable")
            engine = RuleEngine(game)
            engine.join("host", "Host")
            store = Store(path)
            store.save(game, engine.outbox)
            store.close()
            next_seat = engine.join("friend", "Friend").id
            roles = layout(50)[1]
            store = Store(path)
            restored = store.get("persist")
            self.assertEqual(restored.game_seed, "stable")
            self.assertEqual(RuleEngine(restored).join("friend", "Friend").id, next_seat)
            self.assertEqual(restored.rng_state, game.rng_state)
            self.assertEqual(len(roles), 6)
            store.close()

    def test_cross_process_seed_derivation(self):
        script = "from app.rng import derive_seed; print(derive_seed('arena', 9))"
        value = subprocess.check_output([sys.executable, "-c", script], text=True).strip()
        self.assertEqual(int(value), derive_seed("arena", 9))

    def test_rng_sample_and_restore(self):
        states = []
        rng = GameRNG(100, save=states.append)
        rng.sample(list(range(10)), 4)
        copy = GameRNG(100, json.loads(json.dumps(states[-1])))
        self.assertEqual(rng.randrange(999), copy.randrange(999))

    def test_old_snapshot_migrates_without_seed(self):
        state = WerewolfGame("old", "host").dump()
        for key in (
            "experiment_id",
            "game_seed",
            "rng_algorithm",
            "rng_state",
            "ruleset_version",
            "prompt_version",
            "trace_schema_version",
        ):
            state.pop(key)
        restored = WerewolfGame.restore(state)
        self.assertIsNone(restored.game_seed)
        self.assertEqual(restored.rng_algorithm, "system-random")
        self.assertEqual(restored.ruleset_version, "werewolf-v3.0")
