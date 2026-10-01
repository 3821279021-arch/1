"""Offline CI smoke asserts deterministic layouts/results and zero network use."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from .manifest import ROOT
from .runner import run_experiment


async def smoke() -> None:
    cfg = json.loads((ROOT / "experiments/mock.json").read_text())
    cfg["games"] = 2
    with (
        tempfile.TemporaryDirectory() as directory,
        patch("httpx.AsyncClient.send", side_effect=AssertionError("Offline smoke attempted network")) as network,
    ):
        runs = []
        for name in ("first", "second"):
            folder, summary = await run_experiment(cfg, Path(directory), name)
            assert summary["completed_games"] == 2
            assert summary["fallback"]["hits"] == 0
            signatures = []
            for path in sorted((folder / "games").glob("*.jsonl")):
                events = [json.loads(line) for line in path.read_text().splitlines()]
                signatures.append(
                    [
                        (e["game_seed"], e["type"], e["payload"])
                        for e in events
                        if e["type"] in {"seat_assignment", "role_assignment", "game_result"}
                    ]
                )
            runs.append(signatures)
        assert runs[0] == runs[1], "Seeded mock runs diverged"
        network.assert_not_called()
    print("Deterministic arena smoke passed: 2 x 2 games, matching layouts/results, no network")


if __name__ == "__main__":
    asyncio.run(smoke())
