"""Serial batch runner; the existing RuleEngine remains authoritative."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

from app.ai import AIOrchestrator
from app.credentials import CredentialService
from app.game import WerewolfGame
from app.llm import LLMRouter
from app.persistence import Store
from app.performance import profile_model_parameters, resolve_performance
from app.rng import GameRNG, derive_seed
from app.roles import ROLE_DEFINITIONS
from app.runtime_lock import RuntimeLock
from app.scope import InformationScope
from app.trace import TraceWriter, game_metadata, player_assignments, redact

from .config import validate_config
from .manifest import atomic_json, digest, ensure_manifest
from .metrics import aggregate, write_csv


class BudgetExceeded(RuntimeError):
    pass


class ArenaBudget:
    """Reserve before network I/O, persist reservations, refund known usage."""

    def __init__(self, config: dict[str, Any], store: Store) -> None:
        self.config, self.store = config, store
        saved = store.usage_load()
        self.tokens = saved.get("arena_tokens", 0)
        self.cost = saved.get("arena_cost", 0.0)
        self.exhausted = False

    def save(self) -> None:
        self.store.usage_save({"arena_tokens": self.tokens, "arena_cost": self.cost})

    def before_call(self, meta: dict[str, Any]) -> dict[str, Any]:
        tokens = meta["estimated_input_tokens"] + meta["max_output_tokens"]
        price = self.config["prices"].get(meta["model_key"])
        cost = (
            (
                (meta["estimated_input_tokens"] * price["input"] + meta["max_output_tokens"] * price["output"])
                / 1_000_000
            )
            if price
            else 0.0
        )
        cap = self.config["limits"].get("max_estimated_cost")
        if (
            self.tokens + tokens > self.config["limits"]["max_total_tokens"]
            or cap is not None
            and self.cost + cost > cap
        ):
            self.exhausted = True
            raise BudgetExceeded("Experiment resource budget exhausted")
        self.tokens += tokens
        self.cost += cost
        self.save()
        return {"tokens": tokens, "cost": cost}

    def after_call(self, ticket: dict[str, Any], record: dict[str, Any]) -> None:
        if record.get("input_tokens") is not None and record.get("output_tokens") is not None:
            self.tokens += record["input_tokens"] + record["output_tokens"] - ticket["tokens"]
        if record.get("estimated_cost") is not None:
            self.cost += record["estimated_cost"] - ticket["cost"]
        self.save()


def game_plan(config: dict[str, Any], index: int) -> dict[str, Any]:
    count = config["ruleset"]["player_count"]
    variants = len(config["lineups"])
    rotation = (index // variants) % count if config["seat_policy"] == "rotate" else 0
    cycle = variants * (count if config["seat_policy"] == "rotate" else 1)
    seed = derive_seed(config["seed"], index // cycle if config["paired_seeds"] else index)
    lineup = config["lineups"][index % variants]
    members = [lineup["agents"][i % len(lineup["agents"])] for i in range(count)]
    if config["seat_policy"] == "random_seeded":
        GameRNG(derive_seed(seed, 1)).shuffle(members)
    members = members[-rotation:] + members[:-rotation] if rotation else members
    return {
        "game_id": f"game-{index + 1:04d}",
        "game_index": index,
        "game_seed": seed,
        "lineup": lineup["id"],
        "rotation": rotation,
        "agents_by_seat": members,
    }


def build_game(config: dict[str, Any], experiment_id: str, plan: dict[str, Any]) -> tuple[WerewolfGame, Any]:
    from app.rules import RuleEngine

    rules = config["ruleset"]
    game = WerewolfGame(
        f"{experiment_id}:{plan['game_id']}",
        "arena-local-owner",
        game_id=plan["game_id"],
        experiment_id=experiment_id,
        game_seed=plan["game_seed"],
        mode=rules["mode"],
        player_count=rules["player_count"],
        role_roster=rules["roles"],
        unique_model_per_ai_seat=False,
    )
    engine = RuleEngine(game)
    agents = {a["agent_id"]: a for a in config["agents"]}
    seats = [
        {
            "id": i + 1,
            "provider": agents[key]["provider"],
            "model_key": f"{agents[key]['provider']}:{agents[key]['model']}",
            "personality": agents[key]["personality"],
            "model_options": agents[key]["parameters"],
        }
        for i, key in enumerate(plan["agents_by_seat"])
    ]
    engine.configure(
        game.host_id,
        "fast",
        seats,
        False,
        ai_performance_profile=config["ai_performance_profile"],
        ai_performance_custom=config["ai_performance_custom"],
    )
    engine.start(game.host_id, now=100)
    for p in game.players:
        p.agent_id = f"{plan['game_id']}:seat-{p.id}"
    return game, engine


def player_results(
    game: WerewolfGame,
    plan: dict[str, Any],
    deaths: dict[int, int],
    calls: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for p in game.players:
        faction = ROLE_DEFINITIONS[p.role].faction
        actions = [a for a in game.action_history if a["player_id"] == p.id]
        votes = [a for a in actions if a["action"] == "vote" and a.get("target") is not None]
        vote_hits = sum(ROLE_DEFINITIONS[game.player(a["target"]).role].faction != faction for a in votes)
        skills = [
            a
            for a in actions
            if a["phase"].startswith("night_")
            and (a.get("target") is not None or a.get("poison_target") is not None or a.get("save"))
        ]
        skill_hits = 0
        for a in skills:
            target = a.get("poison_target") or a.get("target") or a.get("saved_target")
            if target is not None:
                target_faction = ROLE_DEFINITIONS[game.player(target).role].faction
                if a["action"] == "guard_protect" or a.get("save") and not a.get("poison_target"):
                    skill_hits += target_faction == "good" and deaths.get(target, game.day + 1) > a["day"]
                else:
                    skill_hits += target_faction != faction
        pcalls = [c for c in calls if c.get("agent_id") == p.agent_id]
        poutcomes = [o for o in outcomes if o.get("agent_id") == p.agent_id]
        result.append(
            {
                "seat": p.id,
                "agent_id": plan["agents_by_seat"][p.id - 1],
                "model": p.model,
                "provider": p.provider,
                "role": p.role,
                "faction": faction,
                "won": faction == game.winner,
                "survival_days": deaths.get(p.id, game.day),
                "vote_n": len(votes),
                "vote_hits": vote_hits,
                "skill_n": len(skills),
                "skill_hits": skill_hits,
                "calls": len(pcalls),
                "input_tokens": sum(c.get("input_tokens", 0) for c in pcalls),
                "output_tokens": sum(c.get("output_tokens", 0) for c in pcalls),
                "input_usage_n": sum("input_tokens" in c for c in pcalls),
                "output_usage_n": sum("output_tokens" in c for c in pcalls),
                "latencies_ms": [c["latency_ms"] for c in pcalls if c.get("latency_ms") is not None],
                "model_successes": sum(c.get("success", False) for c in pcalls),
                "invalid_model_outputs": sum(bool(c.get("invalid_output")) for c in pcalls),
                "outcome_n": len(poutcomes),
                "cost": sum(c["estimated_cost"] for c in pcalls)
                if all("estimated_cost" in c for c in pcalls)
                else None,
                "fallbacks": sum(
                    o.get("status") in {"mock_fallback", "budget_exhausted", "partial", "switched"} for o in poutcomes
                ),
            }
        )
    return result


async def play_game(
    config: dict[str, Any], experiment_id: str, plan: dict[str, Any], path: Path, store: Store, budget: ArenaBudget
) -> dict[str, Any]:
    game, engine = build_game(config, experiment_id, plan)
    router = LLMRouter()
    router.credentials = CredentialService(":memory:")
    router.retries = config["limits"]["max_retries"]
    router.mock_chunk_delay = 0
    # Ambient server fallback settings cannot change an arena's chosen models.
    router.prices = config["prices"]
    router.default_prices = {"input": 0.0, "output": 0.0}
    agents = {a["agent_id"]: a for a in config["agents"]}
    bindings: dict[str, dict[str, Any]] = {}
    secrets = []
    writer = None
    status, error = "completed", None
    calls: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    deaths: dict[int, int] = {}
    ai = AIOrchestrator(router)
    started = time.monotonic()
    action_attempts = 0
    try:
        for agent in config["agents"]:
            if agent["provider"] == "mock":
                continue
            ref = agent.get("credential_env") or agent["provider"].upper().replace("-", "_") + "_API_KEY"
            secret = os.getenv(ref)
            if not secret:
                raise ValueError(f"Missing runtime credential reference for agent {agent['agent_id']}")
            secrets.append(secret)
            credential = router.credentials.create(
                game.host_id,
                agent["provider"],
                secret,
                base_url=agent["base_url"],
                temporary=True,
                scope_id=game.room_id,
            )
            bindings[agent["agent_id"]] = {
                "credential_id": credential["id"],
                "credential_owner_id": game.host_id,
                "credential_scope_id": game.room_id,
                "credential_revision": credential["revision"],
                "model_id": agent["model"],
            }
        writer = TraceWriter(path, game, secrets=secrets)
        writer.write("meta", {**game_metadata(game), **plan})
        writer.write("seat_assignment", {"agents_by_seat": plan["agents_by_seat"]})
        writer.write("role_assignment", {"players": player_assignments(game)}, "research_private")
        router.trace_sink = lambda kind, payload: writer.write(kind, payload, "research_private")

        async def record(kind: str, payload: dict[str, Any]) -> None:
            assert writer is not None
            clean = redact(payload, secrets)
            (calls if kind == "call" else outcomes).append(clean)
            store.record_model(kind, clean)
            writer.write("model_call" if kind == "call" else "model_outcome", clean, "research_private")

        router.record_sink = record

        def commit() -> None:
            assert writer is not None
            events, engine.outbox = engine.outbox, []
            writer.engine_events(events)
            store.save(game, events)
            for p in game.players:
                if not p.alive:
                    deaths.setdefault(p.id, game.day)

        def apply_action(pid: int, proposal: dict[str, Any]) -> None:
            nonlocal action_attempts, status
            action_attempts += 1
            try:
                engine.apply(pid, proposal, now=100)
            except ValueError:
                status = "invalid_action"
                raise

        commit()
        for _ in range(config["limits"]["max_steps"]):
            if game.game_over:
                break
            pids = [pid for pid in engine.required_actors() if pid not in game.submitted]
            if not pids:
                raise RuntimeError("Engine stalled without a required actor")
            pid = pids[0]
            p = game.player(pid)
            agent = agents[plan["agents_by_seat"][pid - 1]]
            view = InformationScope.ai_view(game, pid)
            pending = view["pending_action"]
            with router.request_scope(
                room_id=game.room_id,
                game_id=game.game_id,
                agent_id=p.agent_id,
                category=pending["type"],
                day=game.day,
                phase=game.phase,
                request_id=f"{game.turn_id}:{pid}",
                guard=budget,
                model_parameters=profile_model_parameters(
                    resolve_performance(game.ai_performance_profile, game.ai_performance_custom),
                    agent["parameters"],
                ),
                performance_profile=game.ai_performance_profile,
                **bindings.get(agent["agent_id"], {}),
            ):
                secondary = await ai.choose_secondary(view, p.model_key, p.personality, p.memory)
                if secondary:
                    apply_action(pid, secondary)
                    commit()
                    if game.game_over or view["turn_id"] != game.turn_id:
                        continue
                    view = InformationScope.ai_view(game, pid)
                if pending["type"] == "speech":
                    text = "".join([chunk async for chunk in ai.speak(view, p.model_key, p.personality, p.memory)])
                    proposal = {
                        "action": "speech",
                        "speech": text,
                        "game_id": game.game_id,
                        "turn_id": game.turn_id,
                        "turn_sequence": game.turn_sequence,
                    }
                    writer.write("model_output", {"output": text, "agent_id": p.agent_id}, "research_private")
                elif pending["type"] == "wolf_discuss":
                    message = await ai.wolf_discuss(view, p.model_key, p.personality, p.memory)
                    proposal = {
                        "action": "wolf_discuss",
                        "text": message,
                        "game_id": game.game_id,
                        "turn_id": game.turn_id,
                        "turn_sequence": game.turn_sequence,
                    }
                else:
                    proposal = await ai.propose(view, p.model_key, p.personality, p.memory)
                if budget.exhausted:
                    raise BudgetExceeded("Experiment resource budget exhausted")
                writer.write("proposed_action", {"player_id": pid, "action": proposal}, "research_private", pid)
                apply_action(pid, proposal)
                commit()
        else:
            raise TimeoutError("Maximum engine steps exceeded")
    except asyncio.CancelledError:
        status, error = "timeout", "Game timeout or runner interrupted"
        raise
    except TimeoutError:
        status, error = "timeout", "Maximum engine steps exceeded"
    except Exception as exc:
        status = (
            "budget_exhausted"
            if isinstance(exc, BudgetExceeded)
            else status
            if status == "invalid_action"
            else "failed"
        )
        error = type(exc).__name__  # Error messages may contain provider secrets.
    finally:
        await router.close()
        router.credentials.close()
        if writer is None:
            writer = TraceWriter(path, game, secrets=secrets)
            writer.write("meta", {**game_metadata(game), **plan})
        if error:
            writer.write("error", {"status": status, "error": error})
        writer.write("game_result", {"status": status, "winner": game.winner, "days": game.day})
        writer.close()
    return {
        **plan,
        "status": status,
        "winner": game.winner,
        "error": error,
        "duration_s": time.monotonic() - started,
        "action_attempts": action_attempts,
        "days": game.day,
        "players": player_results(game, plan, deaths, calls, outcomes),
        "calls": calls,
        "outcomes": outcomes,
        "decisions": redact(list(ai.decision_records), secrets),
    }


async def run_experiment(
    config: dict[str, Any],
    output: Path,
    experiment_id: str | None = None,
    *,
    resume: bool = False,
    retry_failed: bool = False,
) -> tuple[Path, dict[str, Any]]:
    config = validate_config(config)
    experiment_id = experiment_id or "arena-" + digest(config)[:12]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", experiment_id):
        raise ValueError("Unsafe experiment id")
    folder = output / experiment_id
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    lock = RuntimeLock(str(folder / "runner.lock"))
    store = None
    try:
        manifest = ensure_manifest(folder / "manifest.json", config, experiment_id, resume=resume)
        store = Store(str(folder / "arena.sqlite3"))
        store.register_experiment(manifest)
        store.experiment_status(experiment_id, "running")
        budget = ArenaBudget(config, store)
        previous = store.experiment_results(experiment_id)
        for index in range(config["games"]):
            plan = game_plan(config, index)
            game_id, seed = plan["game_id"], plan["game_seed"]
            trace = folder / "games" / (game_id + ".jsonl")
            old = previous.get(game_id, {})
            if old.get("status") == "completed":
                if not trace.exists():
                    raise RuntimeError("Completed game is missing its trace")
                continue
            if old and old["status"] != "running" and not retry_failed:
                continue
            if trace.exists():
                trace.replace(trace.with_name(f"{game_id}.attempt-{old.get('attempts', 0)}.jsonl"))
            store.experiment_game(experiment_id, game_id, index, seed, "running")
            try:
                result = await asyncio.wait_for(
                    play_game(config, experiment_id, plan, trace, store, budget),
                    config["limits"]["game_timeout_seconds"],
                )
            except TimeoutError:
                report = store.model_report(f"{experiment_id}:{game_id}", game_id)
                snapshot = store.get(f"{experiment_id}:{game_id}")
                result = {
                    **plan,
                    "status": "timeout",
                    "error": "Game timeout",
                    "players": [],
                    "calls": report["calls"],
                    "outcomes": report["outcomes"],
                    "action_attempts": len(snapshot.action_history) if snapshot else 0,
                }
            store.experiment_game(experiment_id, game_id, index, seed, result["status"], result)
            atomic_json(folder / "status.json", store.experiment_results(experiment_id))
            if (
                result["status"] == "budget_exhausted"
                or result["status"] != "completed"
                and config["limits"]["fail_fast"]
            ):
                break
        saved = store.experiment_results(experiment_id)
        results = [record["result"] for record in saved.values() if record["result"] is not None]
        summary = aggregate(results, manifest)
        summary["game_records"] = [
            {
                key: result.get(key)
                for key in ("game_id", "game_seed", "status", "winner", "lineup", "rotation", "error", "days")
            }
            for result in results
        ]
        summary["budget"] = {
            "reserved_tokens": budget.tokens,
            "reserved_cost": budget.cost
            if config["prices"] or all(a["provider"] == "mock" for a in config["agents"])
            else None,
        }
        atomic_json(folder / "summary.json", summary)
        write_csv(folder / "summary.csv", summary)
        store.experiment_status(
            experiment_id, "completed" if summary["completed_games"] == config["games"] else "partial"
        )
        return folder, summary
    finally:
        if store is not None:
            store.close()
        lock.close()
