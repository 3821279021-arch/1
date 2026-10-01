"""Batch planning, real engine runs, resume, failure accounting and metrics."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

from app.persistence import Store
from app.roles import ROLE_DEFINITIONS
from tools.arena.config import validate_config
from tools.arena.manifest import ensure_manifest
from tools.arena.metrics import aggregate, percentile, win_stats
from tools.arena.runner import ArenaBudget, BudgetExceeded, build_game, game_plan, run_experiment

ROOT = Path(__file__).resolve().parents[1]


def config(**changes):
    raw = json.loads((ROOT / "experiments/mock.json").read_text())
    raw.update(changes)
    return validate_config(raw)


class ArenaPlanningTests(unittest.TestCase):
    def test_paired_lineups_and_rotation_preserve_roles_and_cover_factions(self):
        cfg = config(games=12, lineups=[{"id": "ab", "agents": ["A", "B"]}, {"id": "ba", "agents": ["B", "A"]}])
        plans = [game_plan(cfg, i) for i in range(12)]
        self.assertEqual(len({p["game_seed"] for p in plans}), 1)
        roles, seen = [], set()
        for plan in plans:
            game, _ = build_game(cfg, "paired", plan)
            roles.append([p.role for p in game.players])
            seen.update(
                ROLE_DEFINITIONS[p.role].faction for p in game.players if plan["agents_by_seat"][p.id - 1] == "A"
            )
        self.assertTrue(all(r == roles[0] for r in roles))
        self.assertEqual(seen, {"good", "wolves"})
        self.assertNotEqual(plans[0]["agents_by_seat"], plans[2]["agents_by_seat"])
        self.assertNotEqual(plans[0]["game_seed"], game_plan(cfg, 12)["game_seed"])

    def test_random_and_unpaired_schedules(self):
        cfg = config(seat_policy="random_seeded", paired_seeds=False)
        self.assertEqual(game_plan(cfg, 0), game_plan(cfg, 0))
        self.assertNotEqual(game_plan(cfg, 0)["game_seed"], game_plan(cfg, 1)["game_seed"])

    def test_config_rejects_credentials_ambiguous_models_and_cost_caps(self):
        cases = [
            {"games": True},
            {"seat_policy": "unknown"},
            {"limits": {"max_concurrency": 2}},
            {"ruleset": {"mode": "standard9", "player_count": 6}},
            {"agents": [{"id": "A", "provider": "mock", "model": "mock", "api_key": "secret"}]},
            {"agents": [{"id": "A", "provider": "openai", "model": "gpt-4.1-mini", "parameters": {"seed": 1}}]},
            {
                "agents": [{"id": "A", "provider": "openai", "model": "gpt-4.1-mini"}],
                "limits": {"max_estimated_cost": 10},
            },
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config(**changes)

    def test_manifest_is_versioned_and_immutable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest.json"
            cfg = config()
            manifest = ensure_manifest(path, cfg, "manifest", resume=False)
            schema = json.loads((ROOT / "schemas/experiment-manifest.schema.json").read_text())
            jsonschema.validate(manifest, schema)
            self.assertIn("prompt_sha256", manifest)
            self.assertEqual(ensure_manifest(path, cfg, "manifest", resume=True), manifest)
            with self.assertRaises(ValueError):
                ensure_manifest(path, config(seed=44), "manifest", resume=True)
            with self.assertRaises(ValueError):
                ensure_manifest(path, cfg, "manifest", resume=False)

    def test_budget_reservation_survives_restart_and_blocks_before_io(self):
        store = Store(":memory:")
        cfg = config(limits={"max_total_tokens": 500})
        budget = ArenaBudget(cfg, store)
        meta = {"estimated_input_tokens": 100, "max_output_tokens": 100, "model_key": "mock:mock"}
        ticket = budget.before_call(meta)
        budget.after_call(ticket, {"input_tokens": 90, "output_tokens": 50})
        resumed = ArenaBudget(cfg, store)
        self.assertEqual(resumed.tokens, 140)
        resumed.before_call({**meta, "max_output_tokens": 250})
        with self.assertRaises(BudgetExceeded):
            resumed.before_call(meta)
        store.close()


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_illegal_final_action_fails_and_explicit_retry_recovers(self):
        async def invalid(self, view, *args):
            return {
                "action": view["pending_action"]["type"],
                "target": 999,
                **{key: view[key] for key in ("game_id", "turn_id", "turn_sequence")},
            }

        with tempfile.TemporaryDirectory() as folder:
            cfg = config(games=1)
            with patch("app.ai.AIOrchestrator.propose", invalid):
                output, failed = await run_experiment(cfg, Path(folder), "invalid")
            self.assertEqual(failed["status_counts"], {"invalid_action": 1})
            self.assertEqual(failed["completed_games"], 0)
            self.assertGreater(failed["invalid_actions"]["rate"], 0)
            _, recovered = await run_experiment(cfg, Path(folder), "invalid", resume=True, retry_failed=True)
            self.assertEqual(recovered["completed_games"], 1)
            self.assertTrue((output / "games/game-0001.attempt-1.jsonl").exists())

    async def test_all_board_sizes_finish_with_private_trace_and_summaries(self):
        with tempfile.TemporaryDirectory() as folder:
            for mode in ("quick6", "standard9", "standard12", "custom"):
                rules = {"mode": mode}
                if mode == "custom":
                    rules.update(
                        player_count=7, roles=["wolf", "seer", "guard", "hunter", "witch", "villager", "villager"]
                    )
                output, summary = await run_experiment(config(games=1, ruleset=rules), Path(folder), mode)
                self.assertEqual(summary["completed_games"], 1)
                self.assertTrue((output / "summary.csv").exists())
                rows = [json.loads(line) for line in (output / "games/game-0001.jsonl").read_text().splitlines()]
                self.assertEqual([r["seq"] for r in rows], list(range(len(rows))))
                self.assertIn("model_prompt", {r["type"] for r in rows})
                self.assertIn("model_output", {r["type"] for r in rows})
                self.assertEqual(rows[-1]["payload"]["status"], "completed")
                schema = json.loads((ROOT / "schemas/game-trace-event.schema.json").read_text())
                for row in rows:
                    jsonschema.validate(row, schema)

    async def test_resume_does_not_rerun_success_or_overwrite_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            output, first = await run_experiment(config(games=2), Path(folder), "resume")
            manifest = (output / "manifest.json").read_bytes()
            trace = (output / "games/game-0001.jsonl").read_bytes()
            with patch("tools.arena.runner.play_game", side_effect=AssertionError("must not rerun")):
                _, resumed = await run_experiment(config(games=2), Path(folder), "resume", resume=True)
            self.assertEqual(first["by"], resumed["by"])
            self.assertEqual(manifest, (output / "manifest.json").read_bytes())
            self.assertEqual(trace, (output / "games/game-0001.jsonl").read_bytes())
            with self.assertRaises(ValueError):
                await run_experiment(config(games=3), Path(folder), "resume", resume=True)

    async def test_timeout_is_not_success_and_retry_keeps_previous_attempt(self):
        cfg = config(games=1, limits={"max_steps": 1})
        with tempfile.TemporaryDirectory() as folder:
            output, summary = await run_experiment(cfg, Path(folder), "timeout")
            self.assertEqual(summary["status_counts"], {"timeout": 1})
            self.assertEqual(summary["completed_games"], 0)
            _, again = await run_experiment(cfg, Path(folder), "timeout", resume=True, retry_failed=True)
            self.assertEqual(again["completed_games"], 0)
            self.assertTrue((output / "games/game-0001.attempt-1.jsonl").exists())
            status = json.loads((output / "status.json").read_text())
            self.assertEqual(status["game-0001"]["attempts"], 2)

    async def test_running_crash_record_is_resumed(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(games=1)
            output = Path(folder) / "crash"
            output.mkdir()
            manifest = ensure_manifest(output / "manifest.json", cfg, "crash", resume=False)
            store = Store(str(output / "arena.sqlite3"))
            store.register_experiment(manifest)
            plan = game_plan(cfg, 0)
            store.experiment_game("crash", plan["game_id"], 0, plan["game_seed"], "running")
            store.close()
            _, summary = await run_experiment(cfg, Path(folder), "crash", resume=True)
            self.assertEqual(summary["completed_games"], 1)

    async def test_missing_credentials_fail_without_exposing_secret_or_borrowing(self):
        cfg = config(
            games=1,
            agents=[
                {"id": "A", "provider": "openai", "model": "gpt-4.1-mini", "credential_env": "ARENA_MISSING_TEST_KEY"}
            ],
        )
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ARENA_MISSING_TEST_KEY": ""}):
            _, summary = await run_experiment(cfg, Path(folder), "missing")
            self.assertEqual(summary["status_counts"], {"failed": 1})


class MetricsAndCliTests(unittest.TestCase):
    def test_wilson_and_interpolated_latency(self):
        self.assertIsNone(win_stats(0, 0)["win_rate"])
        stats = win_stats(50, 100)
        self.assertAlmostEqual(stats["ci95_low"], 0.40383, places=4)
        self.assertAlmostEqual(stats["ci95_high"], 0.59617, places=4)
        self.assertEqual(percentile([10, 20, 30], 0.5), 20)
        self.assertEqual(percentile([10, 20, 30], 0.95), 29)

    def test_unknown_cost_is_null_and_failed_calls_count(self):
        manifest = {
            "experiment_id": "metrics",
            "game_count": 1,
            "price_table_version": "unpriced",
            "ruleset": {"mode": "quick6"},
            "created_at": "2026-10-01T00:00:00Z",
        }
        results = [
            {
                "status": "failed",
                "calls": [
                    {"provider": "openai", "success": False, "latency_ms": 120, "failure_reason": "invalid_output"}
                ],
                "outcomes": [{"status": "mock_fallback"}],
            }
        ]
        summary = aggregate(results, manifest)
        self.assertIsNone(summary["resources"]["estimated_cost"])
        self.assertEqual(summary["resources"]["latency_p50_ms"], 120)
        self.assertEqual(summary["fallback"]["rate"], 1)
        self.assertEqual(summary["invalid_model_outputs"]["rate"], 1)
        self.assertEqual(summary["invalid_actions"]["n"], 0)

    def test_cli_dry_run_and_error_exit_codes(self):
        cmd = [sys.executable, "-m", "tools.arena", "run", "--config", str(ROOT / "experiments/mock.json")]
        dry = subprocess.run([*cmd, "--dry-run", "--games", "2", "--seed", "42"], capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(json.loads(dry.stdout)["games"], 2)
        bad = subprocess.run([*cmd, "--games", "0"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)


if __name__ == "__main__":
    unittest.main()
