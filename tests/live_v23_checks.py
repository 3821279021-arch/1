"""Opt-in paid DashScope checks: real usage, long-context budget and seer speech.

Set DASHSCOPE_API_KEY externally; no credential is written to the report.
"""

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path
from statistics import median

from dotenv import load_dotenv

from app.ai import AIOrchestrator
from app.game import WerewolfGame
from app.llm import LLMRouter
from app.memory import new_memory, update_memory
from app.rules import RuleEngine
from app.scope import InformationScope
from app.tokens import estimate_tokens

ROOT = Path(__file__).resolve().parents[1]


def scenarios():
    game = WerewolfGame("targeted-live", "owner")
    engine = RuleEngine(game)
    engine.join("owner", "测试玩家", 1)
    engine.start("owner")
    for p, role in zip(game.players, ["seer", "wolf", "villager", "witch", "villager", "wolf"]):
        p.role = role
    engine.enter("day_vote")
    view = InformationScope.ai_view(game, 1)
    memory = new_memory()
    cases = [("day-1", deepcopy(view), deepcopy(memory))]
    for day in range(1, 31):
        for offset in range(18):
            pid = offset % 6 + 1
            text = f"我是村民，怀疑{(pid + 1) % 6 + 1}号，支持{(pid + 3) % 6 + 1}号。请说明判断依据，不能仅凭语气认定身份。"
            memory = update_memory(
                memory,
                {
                    "type": "speech",
                    "event_id": f"p:{day}:{offset}",
                    "day": day,
                    "audience": "public",
                    "data": {"player_id": pid, "speech": text},
                },
                1,
            )
        if day in [5, 15, 30]:
            view["day"] = day
            cases.append((f"day-{day}", deepcopy(view), deepcopy(memory)))
    complex_memory = new_memory()
    for seq in range(1, 601):
        pid = seq % 6 + 1
        target = (seq // 6) % 6 + 1
        day = (seq - 1) // 20 + 1
        text = f"我是{['村民', '预言家', '女巫'][(seq // 36) % 3]}，查杀{target}号，支持{(target + 1) % 6 + 1}号，怀疑{(target + 2) % 6 + 1}号。"
        complex_memory = update_memory(
            complex_memory,
            {
                "type": "speech",
                "event_id": f"complex:{seq}",
                "day": day,
                "audience": "public",
                "data": {"player_id": pid, "speech": text},
            },
            1,
        )
    cases.append(("complex-day-30", deepcopy(view), complex_memory))
    # A real rules-generated private result, then a public speech turn.
    engine.enter("night_seer", 1)
    engine.apply(
        1,
        {
            "action": "seer_inspect",
            "target": 2,
            "game_id": game.game_id,
            "turn_sequence": game.turn_sequence,
            "turn_id": game.turn_id,
        },
    )
    engine.enter("day_speech", 1)
    return cases, InformationScope.ai_view(game, 1), deepcopy(game.player(1).memory)


async def main():
    load_dotenv(ROOT / ".env")
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("DASHSCOPE_API_KEY is required; no live request was sent.")
    models = [
        m.strip() for m in os.getenv("LIVE_CHECK_MODELS", "qwen-plus,qwen-turbo,qwen-flash").split(",") if m.strip()
    ]
    if not 1 <= len(models) <= 5:
        raise SystemExit("Use between one and five models.")
    os.environ.setdefault("DASHSCOPE_MODELS", ",".join(models))
    cases, seer_view, seer_memory = scenarios()
    router = LLMRouter()
    ai = AIOrchestrator(router)
    rows = []
    speeches = []
    try:
        for model in models:
            for case, view, memory in cases:
                system, user, ctx = ai.context(view, "detective", memory)
                context_tokens = estimate_tokens("dashscope", model, system + user)
                assert context_tokens <= int(os.getenv("AI_PROMPT_TOKEN_LIMIT", "6000")) - 600
                system += "只输出合法动作 JSON。"
                user += "\n只返回 " + json.dumps(ctx["decision"], ensure_ascii=False)
                with router.request_scope(
                    room_id="targeted-live", game_id="token-check", category="vote", day=view["day"]
                ):
                    result = await router.ask_json(
                        "dashscope:" + model, system, user, mock_context=ctx, validator=lambda d: d == ctx["decision"]
                    )
                record = deepcopy(router.records[-1])
                record.update(case=case, context_tokens=context_tokens)
                rows.append(record)
                print(
                    json.dumps(
                        {
                            key: record.get(key)
                            for key in [
                                "case",
                                "model",
                                "estimated_input_tokens",
                                "actual_input_tokens",
                                "estimated_actual_ratio",
                                "success",
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                assert result.provider_used == "dashscope" and result.model_used == model
            with router.request_scope(room_id="targeted-live", game_id="speech-check", category="speech", day=1):
                text = "".join(
                    [chunk async for chunk in ai.speak(seer_view, "dashscope:" + model, "detective", seer_memory)]
                )
            speeches.append({"model": model, "text": text, "routing": router.execution()})
            assert "我已查验2号是狼人。" in text and "不是狼" not in text
            print(json.dumps(speeches[-1], ensure_ascii=False), flush=True)
    finally:
        await router.close()
    errors = [abs(r["estimated_actual_ratio"] - 1) for r in rows]
    report = {
        "version": "2.3.1",
        "synthetic_history_real_api": True,
        "calls": list(router.records),
        "token_samples": rows,
        "median_estimation_error": median(errors),
        "max_estimation_error": max(errors),
        "seer_speeches": speeches,
        "activity": router.activity["dashscope"],
    }
    output = ROOT / "test-artifacts" / os.getenv("LIVE_CHECK_REPORT", "v231-live-targeted.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    assert median(errors) < 0.2
    print(
        json.dumps(
            {"report": str(output), "median_error": median(errors), "max_error": max(errors)}, ensure_ascii=False
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
