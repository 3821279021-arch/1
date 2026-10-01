"""Game randomness; identity/session secrets deliberately do not use this RNG."""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, MutableSequence, Sequence
from typing import Any, TypeVar

T = TypeVar("T")
SEEDED_ALGORITHM = "python-mt19937-v1"
SECURE_ALGORITHM = "system-random"


def derive_seed(seed: int | str, index: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}:{index}".encode()).digest()[:8], "big")


def _tuples(value: Any) -> Any:
    return tuple(_tuples(item) for item in value) if isinstance(value, (list, tuple)) else value


class GameRNG:
    def __init__(
        self,
        seed: int | str | None = None,
        state: Any = None,
        save: Callable[[Any], None] | None = None,
    ) -> None:
        if seed is not None and (type(seed) not in (int, str) or seed == ""):
            raise ValueError("game_seed must be a nonempty string or integer")
        self.seed = seed
        self.algorithm = SECURE_ALGORITHM if seed is None else SEEDED_ALGORITHM
        self._random = random.SystemRandom() if seed is None else random.Random(seed)
        if seed is not None and state is not None:
            self._random.setstate(_tuples(state))
        self._save = save

    def _checkpoint(self) -> None:
        if self.seed is not None and self._save is not None:
            state = self._random.getstate()
            self._save([state[0], list(state[1]), state[2]])

    def choice(self, values: Sequence[T]) -> T:
        result = self._random.choice(values)
        self._checkpoint()
        return result

    def shuffle(self, values: MutableSequence[Any]) -> None:
        self._random.shuffle(values)
        self._checkpoint()

    def sample(self, values: Sequence[T], count: int) -> list[T]:
        result = self._random.sample(values, count)
        self._checkpoint()
        return result

    def randrange(self, stop: int) -> int:
        result = self._random.randrange(stop)
        self._checkpoint()
        return result
