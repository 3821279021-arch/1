"""Reduce a prompt projection while retaining the actor's durable memory."""
from __future__ import annotations

import json
from typing import Any
from .tokens import estimate_tokens


def fit_context(system: str, payload: dict[str, Any], budget: int) -> str:
    cached_text = None
    cached_fits = False
    def render():
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def fits():
        nonlocal cached_text, cached_fits
        text = render()
        if text != cached_text:
            cached_text = text
            cached_fits = estimate_tokens("dashscope", "qwen-plus", system + text) <= budget
        return cached_fits

    if fits():
        return render()

    memory = payload["memory"]
    for key in ("self_history", "stances", "vote_history", "judgments", "facts", "check_claims", "claims"):
        records = memory.get(key, [])
        while not fits() and len(records) > 2:
            # Own confirmed results are the strongest evidence and stay pinned.
            index = next((i for i, item in enumerate(records)
                          if not (item.get("confirmed") and item.get("visibility") == "private")), None)
            if index is None:
                break
            records.pop(index)
    events = payload["player_view"].get("events", [])
    while not fits() and events:
        events.pop(0)
    summary = memory.get("summary", {})
    checks = summary.get("check_claims", {})
    target = payload["action_decision"].get("target")
    # Keep changed checks and the current target before routine repeated claims.
    order = sorted(checks, key=lambda pair: (
        checks[pair].get("first_result") != checks[pair].get("latest_result"),
        pair.split(":")[-1] == str(target)))
    for pair in order:
        if fits() or len(checks) <= 6:
            break
        checks.pop(pair)
    for key in ("folded_counts", "received_votes", "judgments", "self_history", "vote_summary", "confirmed_facts", "self_plan"):
        if fits():
            break
        summary.pop(key, None)
    strategy = payload["belief_state"]
    for key in ("role_probabilities", "vote_pressure", "claim_consistency", "evidence"):
        if fits():
            break
        strategy.pop(key, None)
    for key in ("self_history", "stances", "vote_history", "judgments", "facts", "check_claims", "claims", "contradictions", "semantic_hints"):
        records = memory.get(key, [])
        while not fits() and records:
            index = next((i for i, item in enumerate(records)
                          if not (item.get("confirmed") and item.get("visibility") == "private")), None)
            if index is None:
                break
            records.pop(index)
    if not fits():
        # A small configured budget needs a compact core, rather than an
        # oversized request after all recent-event lists have been exhausted.
        memory["beliefs"] = [{key: entry[key] for key in
            ("player_id", "role_guess", "confirmed", "basis_event_id") if key in entry}
            for entry in memory.get("beliefs", []) if entry.get("confirmed")]
        memory["facts"] = []  # Own checks are also represented by the pinned beliefs.
        summary.pop("check_claims", None)
        summary.pop("contradictions", None)
        for key in ("credibility", "relationships", "strategy_plan"):
            strategy.pop(key, None)
        view = payload["player_view"]
        payload["player_view"] = {key: view[key] for key in
            ("day", "phase", "game_over", "turn_sequence", "pending_action", "wolf_teammates") if key in view}
        payload["player_view"]["self"] = {key: view["self"][key] for key in ("id", "role_key", "alive")}
        payload["player_view"]["players"] = [{"id": p["id"], "alive": p["alive"]} for p in view["players"]]
    if not fits():
        # Keep early/latest role changes in storage and send only their concise
        # roles when provenance metadata would exhaust this request's budget.
        for entry in summary.get("role_claims", {}).values():
            entry.pop("event_ids", None)
        payload["expression_plan"]["points"] = payload["expression_plan"].get("points", [])[:1]
    if not fits():
        raise ValueError("PromptBudgetExhausted: essential context exceeds configured limit")
    return render()
