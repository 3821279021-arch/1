"""Explicit denominators, Wilson intervals and unknown-aware resource metrics."""

from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from app.versions import SCHEMA_VERSION


def win_stats(wins: int, n: int) -> dict[str, Any]:
    if not n:
        return {"wins": wins, "games": n, "n": n, "win_rate": None, "ci95_low": None, "ci95_high": None}
    p, z = wins / n, 1.959963984540054
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return {
        "wins": wins,
        "games": n,
        "n": n,
        "win_rate": p,
        "ci95_low": max(0, center - half),
        "ci95_high": min(1, center + half),
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * fraction
    lower = math.floor(pos)
    return ordered[lower] + (ordered[math.ceil(pos)] - ordered[lower]) * (pos - lower)


def rate(hits: int, total: int) -> dict[str, Any]:
    return {"hits": hits, "n": total, "rate": hits / total if total else None}


def observation_stats(players: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [p for p in players if p["status"] == "completed"]
    latencies = [float(value) for p in players for value in p.get("latencies_ms", [])]
    outcomes = sum(p.get("outcome_n", 0) for p in players)
    fallbacks = sum(p.get("fallbacks", 0) for p in players)
    return {
        **win_stats(sum(p["won"] for p in completed), len(completed)),
        "attempts_n": len(players),
        "model_calls": sum(p.get("calls", 0) for p in players),
        "input_tokens": sum(p.get("input_tokens", 0) for p in players),
        "output_tokens": sum(p.get("output_tokens", 0) for p in players),
        "input_usage_n": sum(p.get("input_usage_n", 0) for p in players),
        "output_usage_n": sum(p.get("output_usage_n", 0) for p in players),
        "estimated_cost": sum(p["cost"] for p in players) if all(p.get("cost") is not None for p in players) else None,
        "latency_n": len(latencies),
        "latency_p50_ms": percentile(latencies, 0.5),
        "latency_p95_ms": percentile(latencies, 0.95),
        "fallbacks": fallbacks,
        "outcome_n": outcomes,
        "fallback_rate": fallbacks / outcomes if outcomes else None,
        "model_successes": sum(p.get("model_successes", 0) for p in players),
        "invalid_model_outputs": sum(p.get("invalid_model_outputs", 0) for p in players),
    }


def aggregate(results: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    completed = [r for r in results if r.get("status") == "completed"]
    all_players = [
        {
            **p,
            "status": r["status"],
            "ruleset": manifest["ruleset"]["mode"],
            "date": manifest["created_at"][:10],
            "game_id": r["game_id"],
            "lineup": r["lineup"],
        }
        for r in results
        for p in r.get("players", [])
    ]
    players = [p for p in all_players if p["status"] == "completed"]
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {
        key: defaultdict(list) for key in ("agent", "model", "provider", "faction", "role", "seat", "lineup")
    }
    for player in all_players:
        for dim, group in groups.items():
            key = str(player["agent_id"] if dim == "agent" else player[dim])
            group[key].append(player)
    grouped = {dim: {key: observation_stats(ps) for key, ps in values.items()} for dim, values in groups.items()}
    calls = [call for result in results for call in result.get("calls", [])]
    outcomes = [o for result in results for o in result.get("outcomes", [])]
    decisions = [d for result in results for d in result.get("decisions", [])]
    latencies = [float(c["latency_ms"]) for c in calls if c.get("latency_ms") is not None]
    costs = [float(c["estimated_cost"]) for c in calls if c.get("estimated_cost") is not None]
    paid = [c for c in calls if c.get("provider") != "mock"]
    cost_known = all(c.get("estimated_cost") is not None for c in paid)
    votes = sum(p.get("vote_n", 0) for p in players)
    vote_hits = sum(p.get("vote_hits", 0) for p in players)
    skills = sum(p.get("skill_n", 0) for p in players)
    skill_hits = sum(p.get("skill_hits", 0) for p in players)
    fallback_n = sum(o.get("status") in {"mock_fallback", "budget_exhausted", "partial", "switched"} for o in outcomes)
    invalid_outputs = sum(
        bool(c.get("invalid_output")) or "invalid" in str(c.get("failure_reason", "")).lower() for c in calls
    )
    invalid_decisions = sum("invalid" in str(d.get("reason", "")).lower() for d in decisions)
    rejected_actions = sum(r["status"] == "invalid_action" for r in results)
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": manifest["experiment_id"],
        "manifest": manifest,
        "planned_games": manifest["game_count"],
        "completed_games": len(completed),
        "status_counts": dict(Counter(r["status"] for r in results)),
        "overall": observation_stats(all_players),
        "overall_note": "N counts player-game observations; players in one game are correlated. Group intervals are descriptive, not significance tests.",
        "by": grouped,
        "vote_accuracy": rate(vote_hits, votes),
        "night_skill_effectiveness": rate(skill_hits, skills),
        "skill_definition": "Night actions with non-null targets; hostile skills hit the opposing faction, seer detects wolves, guard/save keeps a good target alive through dawn. This is descriptive, not a strategic score.",
        "invalid_actions": rate(rejected_actions, sum(r.get("action_attempts", 0) for r in results)),
        "invalid_model_outputs": rate(invalid_outputs, len(calls)),
        "invalid_decisions": rate(invalid_decisions, len(decisions)),
        "fallback": rate(fallback_n, len(outcomes)),
        "model_success": rate(sum(c.get("success", False) for c in calls), len(calls)),
        "resources": {
            "n": len(calls),
            "input_tokens": sum(c.get("input_tokens", 0) for c in calls),
            "output_tokens": sum(c.get("output_tokens", 0) for c in calls),
            "input_usage_n": sum(c.get("input_tokens") is not None for c in calls),
            "output_usage_n": sum(c.get("output_tokens") is not None for c in calls),
            "estimated_input_tokens": sum(c.get("estimated_input_tokens", 0) for c in calls),
            "estimated_cost": sum(costs) if cost_known else None,
            "known_cost_subtotal": sum(costs),
            "cost_n": len(costs),
            "price_table_version": manifest["price_table_version"],
            "mean_tokens_per_game": sum(c.get("input_tokens", 0) + c.get("output_tokens", 0) for c in calls)
            / len(completed)
            if completed and all("input_tokens" in c and "output_tokens" in c for c in calls)
            else None,
            "mean_cost_per_game": sum(costs) / len(completed) if completed and cost_known else None,
            "latency_n": len(latencies),
            "latency_p50_ms": percentile(latencies, 0.5),
            "latency_p95_ms": percentile(latencies, 0.95),
            "duration_n": len(completed),
            "mean_game_duration_s": mean(r["duration_s"] for r in completed) if completed else None,
            "survival_n": len(players),
            "mean_survival_days": mean(p["survival_days"] for p in players) if players else None,
        },
        "observations": players,
    }


def write_csv(path: Path, summary: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        fields = [
            "dimension",
            "value",
            "wins",
            "games",
            "n",
            "win_rate",
            "ci95_low",
            "ci95_high",
            "attempts_n",
            "model_calls",
            "input_tokens",
            "output_tokens",
            "input_usage_n",
            "output_usage_n",
            "estimated_cost",
            "latency_n",
            "latency_p50_ms",
            "latency_p95_ms",
            "fallbacks",
            "outcome_n",
            "fallback_rate",
            "model_successes",
            "invalid_model_outputs",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"dimension": "overall", "value": "all", **summary["overall"]})
        for dim, groups in summary["by"].items():
            for key, stats in groups.items():
                writer.writerow({"dimension": dim, "value": key, **stats})
    path.chmod(0o600)
