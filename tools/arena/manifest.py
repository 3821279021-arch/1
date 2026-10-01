"""Immutable manifests bind resume to code, config and effective versions."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from pathlib import Path
from typing import Any

from app.trace import redact, utc_now
from app.versions import APP_VERSION, PROMPT_VERSION, RULESET_VERSION, SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[2]


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def code_fingerprint() -> str:
    files = [
        *sorted((ROOT / "app").glob("*.py")),
        *sorted((ROOT / "tools" / "arena").glob("*.py")),
        ROOT / "requirements.lock",
    ]
    return digest({str(file.relative_to(ROOT)): hashlib.sha256(file.read_bytes()).hexdigest() for file in files})


def make_manifest(config: dict[str, Any], experiment_id: str) -> dict[str, Any]:
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
            ).stdout.strip()
            or None
        )
    except OSError:
        commit = None
    agents = [{key: value for key, value in agent.items() if key != "credential_env"} for agent in config["agents"]]
    return redact(
        {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": experiment_id,
            "created_at": utc_now(),
            "git_commit": commit,
            "app_version": APP_VERSION,
            "code_sha256": code_fingerprint(),
            "config_sha256": digest(config),
            "prompt_sha256": hashlib.sha256((ROOT / "app" / "ai.py").read_bytes()).hexdigest(),
            "experiment_seed": config["seed"],
            "game_count": config["games"],
            "ruleset": config["ruleset"],
            "ruleset_version": RULESET_VERSION,
            "seat_policy": config["seat_policy"],
            "role_policy": config["role_policy"],
            "paired_seeds": config["paired_seeds"],
            "lineups": config["lineups"],
            "agents": agents,
            "prompt_version": PROMPT_VERSION,
            "model_parameters": {a["agent_id"]: a["parameters"] for a in agents},
            "environment": {"python": platform.python_version(), "platform": platform.platform()},
            "price_table_version": config["price_table_version"],
            "prices": config["prices"],
            "limits": config["limits"],
            "reproducibility": "Environment only; hosted model outputs may change.",
        }
    )


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def ensure_manifest(path: Path, config: dict[str, Any], experiment_id: str, *, resume: bool) -> dict[str, Any]:
    if path.exists():
        if not resume:
            raise ValueError("Experiment exists; use --resume or a different experiment id")
        existing = json.loads(path.read_text(encoding="utf-8"))
        if (
            existing.get("config_sha256") != digest(config)
            or existing.get("code_sha256") != code_fingerprint()
            or existing.get("experiment_id") != experiment_id
        ):
            raise ValueError("Resume refused: config, code or experiment id changed")
        return existing
    if resume:
        raise ValueError("Cannot resume without an existing manifest")
    manifest = make_manifest(config, experiment_id)
    atomic_json(path, manifest)
    return manifest
