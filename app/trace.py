"""Versioned research records. These are never used as a public player view."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .game import WerewolfGame
from .versions import APP_VERSION, SCHEMA_VERSION

_DENIED = {
    "apikey",
    "authorization",
    "secret",
    "sessionsecret",
    "sessiontoken",
    "recoverycode",
    "password",
    "passwordhash",
    "tokenhash",
    "recoveryhash",
    "credentialid",
    "credentialownerid",
    "ownerid",
    "hostid",
    "headers",
    "reasoning",
    "chainofthought",
    "thinking",
    "reasoningcontent",
}
_SECRET = re.compile(
    r"(?i)(?:bearer\s+[^\s\"'<>]+|\bsk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|session[_-]?(?:secret|token)|recovery[_-]?code|authorization)\s*[=:]\s*[^\s,;\"'<>]+)"
)


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    known = tuple(secret for secret in secrets if secret)
    if isinstance(value, dict):
        return {
            str(redact(str(key), known)): redact(item, known)
            for key, item in value.items()
            if re.sub(r"[^a-z]", "", str(key).lower()) not in _DENIED
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, known) for item in value]
    if isinstance(value, str):
        # Structured final content sometimes contains unsolicited reasoning.
        if value.lstrip().startswith(("{", "[")):
            try:
                structured = json.loads(value)
            except json.JSONDecodeError:
                pass
            else:
                return json.dumps(redact(structured, known), ensure_ascii=False)
        for secret in known:
            value = value.replace(secret, "[REDACTED]")
        return _SECRET.sub("[REDACTED]", value)
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def trace_event(
    game: WerewolfGame,
    seq: int,
    kind: str,
    payload: dict[str, Any],
    *,
    audience: str = "system",
    player_id: int | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": game.experiment_id,
        "game_id": game.game_id,
        "game_seed": game.game_seed,
        "seq": seq,
        "timestamp": utc_now(),
        "type": kind,
        "audience": audience,
        "player_id": player_id,
        "payload": payload,
    }


def game_metadata(game: WerewolfGame) -> dict[str, Any]:
    return {
        "app_version": APP_VERSION,
        "ruleset_version": game.ruleset_version,
        "prompt_version": game.prompt_version,
        "rng_algorithm": game.rng_algorithm,
        "ruleset": {"mode": game.mode, "player_count": game.player_count, "roles": game.role_roster},
        "reproducibility": "Environment only; cloud model output is not guaranteed identical.",
    }


def player_assignments(game: WerewolfGame) -> list[dict[str, Any]]:
    return [
        {
            "seat": p.id,
            "agent_id": p.agent_id,
            "name": p.name,
            "role": p.role,
            "provider": p.provider,
            "model": p.model,
            "parameters": p.model_options,
            "personality": p.personality,
        }
        for p in game.players
    ]


class TraceWriter:
    def __init__(self, path: Path, game: WerewolfGame, *, secrets: Iterable[str] = ()) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", encoding="utf-8")
        path.chmod(0o600)
        self.game = game
        self.seq = 0
        self.secrets = tuple(secrets)

    def write(self, kind: str, payload: dict[str, Any], audience: str = "system", player_id: int | None = None) -> None:
        event = trace_event(self.game, self.seq, kind, payload, audience=audience, player_id=player_id)
        self._file.write(json.dumps(redact(event, self.secrets), ensure_ascii=False) + "\n")
        self._file.flush()
        self.seq += 1

    def engine_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            audience = event.get("audience", "public")
            if audience not in {"public", "player", "wolves", "system"}:
                audience = "research_private"
            self.write(event["type"], event, audience, event.get("player_id"))

    def close(self) -> None:
        self._file.close()


def research_export(game: WerewolfGame, events: list[dict[str, Any]], telemetry: dict[str, Any]) -> Iterable[str]:
    """Caller must verify historical host ownership and finished status first."""
    rows = [
        trace_event(game, 0, "meta", game_metadata(game)),
        trace_event(game, 1, "role_assignment", {"players": player_assignments(game)}, audience="research_private"),
    ]
    for event in events:
        audience = event.get("audience", "public")
        if audience not in {"public", "player", "wolves", "system"}:
            audience = "research_private"
        rows.append(
            trace_event(game, len(rows), event["type"], event, audience=audience, player_id=event.get("player_id"))
        )
    for kind, records in (("model_call", telemetry["calls"]), ("model_outcome", telemetry["outcomes"])):
        for record in records:
            rows.append(trace_event(game, len(rows), kind, record, audience="research_private"))
    rows.append(trace_event(game, len(rows), "game_result", {"winner": game.winner, "days": game.day}))
    for row in rows:
        yield json.dumps(redact(row), ensure_ascii=False) + "\n"
