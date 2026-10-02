import asyncio

import pytest

from app.ai import AIOrchestrator
from app.game import WerewolfGame
from app.performance import profile_model_parameters, resolve_performance, validate_performance
from app.rules import RuleEngine
from app.scope import InformationScope


def make_game():
    game = WerewolfGame("room-perf", "owner", mode="quick6")
    engine = RuleEngine(game)
    engine.join("owner", "Host", 1)
    engine.configure("owner", "fast", [], False)
    engine.start("owner", now=100)
    return game, engine


def test_profiles_resolve_and_custom_validate():
    unrestricted = resolve_performance("unrestricted", {})
    assert unrestricted["history_mode"] == "full"
    assert unrestricted["recent_events_limit"] is None
    assert unrestricted["max_output_tokens"] == 16384
    assert unrestricted["force_speech"] is False

    profile, custom = validate_performance(
        "custom",
        {
            "prompt_token_limit": 100000,
            "history_mode": "full",
            "max_output_tokens": 32768,
            "reasoning_effort": "xhigh",
            "thinking_budget": 32768,
            "speech_character_limit": None,
            "force_concise": False,
            "force_speech": False,
        },
    )
    assert profile == "custom"
    assert custom["max_output_tokens"] == 32768
    with pytest.raises(ValueError):
        validate_performance("custom", {"prompt_token_limit": 500})


def test_profile_model_parameters_seat_override_wins():
    performance = resolve_performance("unrestricted", {})
    params = profile_model_parameters(performance, {"max_output_tokens": 2048, "temperature": 0.2})
    assert params["max_output_tokens"] == 2048
    assert params["temperature"] == 0.2


def test_ai_view_history_is_profile_controlled():
    game, _ = make_game()
    game.events = [{"event_id": f"e{i}", "audience": "public", "kind": "system", "data": {}} for i in range(30)]
    game.ai_performance_profile = "economy"
    compact = InformationScope.ai_view(game, 1)
    assert len(compact["events"]) == 8

    game.ai_performance_profile = "unrestricted"
    full = InformationScope.ai_view(game, 1)
    assert len(full["events"]) == 30
    assert full["ai_performance"]["history_mode"] == "full"


def test_rule_configuration_persists_profile():
    game = WerewolfGame("room-config", "owner", mode="quick6")
    engine = RuleEngine(game)
    engine.join("owner", "Host", 1)
    engine.configure(
        "owner",
        "fast",
        [],
        False,
        ai_performance_profile="custom",
        ai_performance_custom={
            "prompt_token_limit": 50000,
            "history_mode": "full",
            "max_output_tokens": 8192,
            "force_speech": False,
        },
    )
    assert game.ai_performance_profile == "custom"
    assert game.ai_performance_custom["max_output_tokens"] == 8192


class SilentRouter:
    async def speech_stream(self, provider, system, user, ctx):
        for chunk in ("<SKIP_", "SPEECH>"):
            yield chunk


def test_unrestricted_can_choose_strategic_silence():
    game, _ = make_game()
    game.ai_performance_profile = "unrestricted"
    view = InformationScope.ai_view(game, 1)
    ai = AIOrchestrator(SilentRouter())

    async def collect():
        memory = {"facts": [], "beliefs": [], "summary": {}}
        return [chunk async for chunk in ai.speak(view, "mock", "detective", memory)]

    assert asyncio.run(collect()) == []
    assert ai.decision_records[-1]["reason"] == "strategic_silence"
