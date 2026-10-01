"""python -m tools.arena run --config experiments/mock.json --games 4 --seed 42"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from .config import validate_config
from .runner import game_plan, run_experiment


def main() -> int:
    parser = argparse.ArgumentParser(description="Reproducible AI Werewolf Arena")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--games", type=int)
    run.add_argument("--seed")
    run.add_argument("--output", type=Path, default=Path("artifacts/experiments"))
    run.add_argument("--experiment-id")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--retry-failed", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if args.games is not None:
            config["games"] = args.games
        if args.seed is not None:
            config["seed"] = int(args.seed) if args.seed.lstrip("-").isdigit() else args.seed
        config = validate_config(config)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "games": config["games"],
                        "paid": any(a["provider"] != "mock" for a in config["agents"]),
                        "limits": config["limits"],
                        "plan": [game_plan(config, i) for i in range(min(config["games"], 12))],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        folder, summary = asyncio.run(
            run_experiment(config, args.output, args.experiment_id, resume=args.resume, retry_failed=args.retry_failed)
        )
        print(
            json.dumps(
                {
                    "artifacts": str(folder),
                    "completed": summary["completed_games"],
                    "planned": config["games"],
                    "status_counts": summary["status_counts"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if summary["completed_games"] == config["games"] else 2
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print("Configuration error: " + str(exc))
        return 1
    except (OSError, RuntimeError) as exc:
        print("Infrastructure error: " + type(exc).__name__)
        return 3
    except KeyboardInterrupt:
        print("Interrupted; use the same config with --resume")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
