import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.limits import BudgetExceeded, Limits, RateLimitError


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class LimitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.meta = {
            "room_id": "room-a",
            "game_id": "game-a",
            "agent_id": "agent-a",
            "category": "decision",
            "provider": "dashscope",
            "model": "qwen-plus",
            "estimated_input_tokens": 20,
            "max_output_tokens": 30,
        }

    def limits(self, **config):
        return Limits(clock=self.clock, config=config)

    def test_pet_flood_is_rejected_before_additional_provider_work(self):
        limits = self.limits(pet_per_minute=3)
        calls = 0
        for _ in range(30):
            try:
                limits.check("pet", "room-a:owner-a")
            except RateLimitError as exc:
                self.assertEqual(exc.status_code, 429)
                self.assertEqual(exc.reason, "pet_rate")
                self.assertEqual(exc.retry_after, 60)
            else:
                calls += 1  # API schedules its model request only after this check.
        self.assertEqual(calls, 3)
        self.clock.now += 60
        limits.check("pet", "room-a:owner-a")

    def test_session_room_and_room_ai_limits_are_independent(self):
        limits = self.limits(session_per_minute=1, room_per_hour=1, room_ai_per_minute=1)
        for kind in ("session", "room"):
            limits.check(kind, "owner")
            with self.assertRaises(RateLimitError):
                limits.check(kind, "owner")
            limits.check(kind, "another-owner")
        ticket = limits.before_call(self.meta)
        limits.after_call(ticket)
        with self.assertRaisesRegex(BudgetExceeded, "AI") as refused:
            limits.before_call(dict(self.meta, game_id="another-game"))
        self.assertEqual(refused.exception.reason, "room_ai_rate")
        self.clock.now += 60
        limits.before_call(self.meta)

    def test_active_room_limit(self):
        limits = self.limits(max_active_rooms=2)
        limits.check_active_rooms(1)
        with self.assertRaises(RateLimitError) as refused:
            limits.check_active_rooms(2)
        self.assertEqual(refused.exception.reason, "active_rooms")

    def test_game_request_budget_counts_retry_attempts(self):
        limits = self.limits(game_max_requests=2)
        first = limits.before_call(self.meta)
        limits.after_call(first, {"success": False})
        second = limits.before_call(self.meta)
        limits.after_call(second, {"success": True, "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
        with self.assertRaises(BudgetExceeded) as refused:
            limits.before_call(self.meta)
        self.assertEqual(refused.exception.reason, "game_request_budget")
        self.assertEqual(limits.snapshot("room-a", "game-a")["game"]["requests"], 2)
        self.assertFalse(limits.snapshot("room-a", "game-a")["real_calls_allowed"])

    def test_concurrent_reservations_cannot_overrun_game_token_budget(self):
        limits = self.limits(game_token_budget=100)
        gate = threading.Barrier(8)

        def reserve(_):
            gate.wait()
            try:
                return limits.before_call(self.meta)
            except BudgetExceeded:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            tickets = list(pool.map(reserve, range(8)))
        tickets = [ticket for ticket in tickets if ticket]
        self.assertEqual(len(tickets), 2)
        self.assertEqual(limits.snapshot("room-a", "game-a")["game"]["reserved_tokens"], 100)
        for ticket in tickets:
            limits.after_call(ticket, {"usage": {"input_tokens": 10, "output_tokens": 5}})
        snapshot = limits.snapshot("room-a", "game-a")["game"]
        self.assertEqual(snapshot["tokens"], 30)
        self.assertEqual(snapshot["reserved_tokens"], 0)
        self.assertTrue(limits.snapshot("room-a", "game-a")["real_calls_allowed"])
        # Accurate usage refunds unused reservation, allowing another real request.
        limits.before_call(self.meta)

    def test_usage_refund_unknown_usage_and_duplicate_completion(self):
        limits = self.limits()
        ticket = limits.before_call(self.meta)
        limits.after_call(ticket, {"usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}})
        limits.after_call(ticket, {"usage": {"total_tokens": 0}})
        ticket = limits.before_call(self.meta)
        limits.after_call(ticket, {"success": False})
        self.assertEqual(limits.snapshot("room-a", "game-a")["game"]["tokens"], 62)

    def test_global_daily_budget_applies_across_rooms_and_rolls_over_utc(self):
        limits = self.limits(daily_token_budget=100)
        for room_id in ("room-a", "room-b"):
            ticket = limits.before_call(dict(self.meta, room_id=room_id))
            limits.after_call(ticket)
        with self.assertRaises(BudgetExceeded) as refused:
            limits.before_call(dict(self.meta, room_id="room-c"))
        self.assertEqual(refused.exception.reason, "daily_token_budget")
        old_day = limits.snapshot("room-a", "game-a")["daily_date"]
        self.clock.now += 86_400
        ticket = limits.before_call(dict(self.meta, room_id="room-c"))
        limits.after_call(ticket)
        self.assertNotEqual(limits.snapshot("room-c", "game-a")["daily_date"], old_day)
        self.assertEqual(limits.snapshot("room-c", "game-a")["daily"]["tokens"], 50)

    def test_restart_consumes_outstanding_reservations_without_resetting_budget(self):
        saved = {}

        def persist(state):
            saved.clear()
            saved.update(state)

        limits = Limits(clock=self.clock, config={"game_token_budget": 50}, persist=persist)
        limits.before_call(self.meta)  # Simulate crash during an already issued HTTP request.
        restarted = Limits(clock=self.clock, config={"game_token_budget": 50}, load=lambda: saved, persist=persist)
        self.assertEqual(restarted.snapshot("room-a", "game-a")["game"]["tokens"], 50)
        self.assertEqual(restarted.snapshot("room-a", "game-a")["game"]["reserved_tokens"], 0)
        with self.assertRaises(BudgetExceeded):
            restarted.before_call(self.meta)
        self.assertFalse(saved["pending"])

    def test_rate_windows_survive_restart_and_do_not_store_raw_identity(self):
        saved = {}

        def persist(state):
            saved.clear()
            saved.update(state)

        limits = Limits(clock=self.clock, config={"pet_per_minute": 1}, persist=persist)
        limits.check("pet", "secret-session-token")
        self.assertNotIn("secret-session-token", str(saved))
        restarted = Limits(clock=self.clock, config={"pet_per_minute": 1}, load=lambda: saved)
        with self.assertRaises(RateLimitError):
            restarted.check("pet", "secret-session-token")

    def test_optional_cost_budget_reserves_using_actual_model_price(self):
        with patch.dict("os.environ", {"AI_MODEL_PRICES": '{"dashscope:qwen-plus":{"input":1,"output":2}}'}):
            limits = self.limits(game_cost_budget=0.00008)
            ticket = limits.before_call(self.meta)
            with self.assertRaises(BudgetExceeded) as refused:
                limits.before_call(self.meta)
            self.assertEqual(refused.exception.reason, "game_cost_budget")
            limits.after_call(ticket, {"usage": {"input_tokens": 5, "output_tokens": 5}})
            self.assertAlmostEqual(limits.snapshot("room-a", "game-a")["game"]["estimated_cost"], 0.000015)

    def test_total_only_usage_cost_is_conservative(self):
        limits = self.limits(input_price_per_million=1, output_price_per_million=2)
        ticket = limits.before_call(self.meta)
        limits.after_call(ticket, {"usage": {"total_tokens": 10}})
        self.assertEqual(limits.snapshot("room-a", "game-a")["game"]["tokens"], 10)
        self.assertAlmostEqual(limits.snapshot("room-a", "game-a")["game"]["estimated_cost"], 0.00002)

    def test_invalid_usage_counters_preserve_reservation_and_do_not_break_game(self):
        limits = self.limits()
        ticket = limits.before_call(self.meta)
        limits.after_call(ticket, {"usage": {"total_tokens": "invalid"}})
        game = limits.snapshot("room-a", "game-a")["game"]
        self.assertEqual(game["tokens"], 50)
        self.assertEqual(game["reserved_tokens"], 0)

    def test_backward_clock_adjustment_does_not_reset_rate(self):
        limits = self.limits(pet_per_minute=1)
        limits.check("pet", "room-a:owner-a")
        self.clock.now -= 10
        with self.assertRaises(RateLimitError):
            limits.check("pet", "room-a:owner-a")

    def test_corrupt_or_failed_accounting_blocks_paid_calls(self):
        limits = Limits(clock=self.clock, load=lambda: {"version": 999})
        with self.assertRaises(BudgetExceeded) as refused:
            limits.before_call(self.meta)
        self.assertEqual(refused.exception.reason, "budget_storage_unavailable")

        def broken(_):
            raise OSError("Disk unavailable")

        limits = Limits(clock=self.clock, persist=broken)
        with self.assertRaises(BudgetExceeded):
            limits.before_call(self.meta)
        self.assertEqual(limits.snapshot("room-a", "game-a")["game"]["requests"], 0)


if __name__ == "__main__":
    unittest.main()
