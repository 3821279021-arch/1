"""Serializable per-room state. All gameplay mutations belong to RuleEngine."""

from __future__ import annotations

import os
import secrets
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

from .rng import SECURE_ALGORITHM, SEEDED_ALGORITHM
from .roles import GAME_MODES as GAME_MODES
from .roles import ROLE_DEFINITIONS, validate_mode
from .versions import PROMPT_VERSION, RULESET_VERSION, SCHEMA_VERSION

ROLE_NAMES = {key: role.display_name for key, role in ROLE_DEFINITIONS.items()}
PERSONALITIES = {
    "detective": {
        "name": "逻辑侦探",
        "prompt": "重视证据、票型和时间线，区分事实与猜测。",
        "aggression": 0.4,
        "caution": 0.7,
        "logic": 0.9,
        "deception": 0.3,
        "length": 100,
        "social": "理性",
    },
    "hunter": {
        "name": "激进猎手",
        "prompt": "主动追问、表达鲜明判断；风格由你决定，允许游戏内试探和身份宣称。",
        "aggression": 0.9,
        "caution": 0.3,
        "logic": 0.6,
        "deception": 0.3,
        "length": 80,
        "social": "直接",
    },
    "trickster": {
        "name": "欺诈大师",
        "prompt": "重视心理博弈，可以伪装身份、诈查验、试探和改变立场，由你自主选择策略。",
        "aggression": 0.5,
        "caution": 0.5,
        "logic": 0.6,
        "deception": 0.9,
        "length": 100,
        "social": "试探",
    },
    "cautious": {
        "name": "谨慎型",
        "prompt": "保持谨慎，标注证据不足，优先提出可验证的问题。",
        "aggression": 0.2,
        "caution": 0.95,
        "logic": 0.8,
        "deception": 0.2,
        "length": 90,
        "social": "温和",
    },
    "performer": {
        "name": "表演型",
        "prompt": "表达有情绪和临场感，发言自然，但不泄露内部分析过程。",
        "aggression": 0.6,
        "caution": 0.4,
        "logic": 0.5,
        "deception": 0.6,
        "length": 120,
        "social": "活泼",
    },
    "commander": {
        "name": "指挥官",
        "prompt": "整理局势，提出清晰的下一步行动和追问建议。",
        "aggression": 0.6,
        "caution": 0.6,
        "logic": 0.8,
        "deception": 0.4,
        "length": 120,
        "social": "组织",
    },
}
PROVIDERS = {"openai", "anthropic", "gemini", "dashscope", "mock", "auto", "openai-compatible", "openai_compatible"}
TIMINGS = {
    "fast": {
        "night_discussion": 15,
        "night_wolves": 15,
        "night_seer": 15,
        "night_witch": 15,
        "day_speech": 15,
        "last_words": 20,
        "day_vote": 15,
    },
    "standard": {
        "night_discussion": 30,
        "night_wolves": 15,
        "night_seer": 15,
        "night_witch": 15,
        "day_speech": 30,
        "last_words": 20,
        "day_vote": 15,
    },
    "slow": {
        "night_discussion": 45,
        "night_wolves": 20,
        "night_seer": 20,
        "night_witch": 20,
        "day_speech": 60,
        "last_words": 30,
        "day_vote": 25,
    },
}
PHASE_NAMES = {
    "lobby": "等待玩家",
    "night_discussion": "狼人讨论",
    "night_wolves": "狼人最终选择",
    "night_seer": "预言家查验",
    "night_witch": "女巫用药",
    "day_speech": "依次发言",
    "day_vote": "投票放逐",
    "last_words": "遗言",
    "finished": "游戏结束",
}
PHASE_NAMES.update(night_guard="守卫守护", night_beauty="狼美人魅惑", death_skill="死亡技能")
for timing in TIMINGS.values():
    timing.update(night_guard=timing["night_seer"], night_beauty=timing["night_seer"], death_skill=timing["last_words"])


@dataclass
class Player:
    id: int
    name: str
    provider: str = "mock"
    role: str = "villager"
    alive: bool = True
    owner_id: str | None = None
    personality: str = "detective"
    private_notes: list[str] = field(default_factory=list)
    memory: dict[str, Any] = field(
        default_factory=lambda: {
            "facts": [],
            "conjectures": [],
            "suspicions": {},
            "role_guesses": {},
            "contradictions": [],
            "judgments": [],
        }
    )
    agent_id: str = field(default_factory=lambda: secrets.token_hex(16))
    model: str = "mock"
    model_key: str = "mock:mock"
    model_locked: bool = False
    voice_profile: dict[str, Any] = field(default_factory=dict)
    conversation_state: dict[str, Any] = field(default_factory=dict)
    execution_status: dict[str, Any] = field(default_factory=dict)
    wolf_visible_until: int | None = None
    wolf_teammates_at_death: list[dict[str, Any]] = field(default_factory=list)
    role_state: dict[str, Any] = field(default_factory=dict)
    credential_id: str | None = None
    credential_owner_id: str | None = None
    model_id: str = ""
    model_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.voice_profile:
            self.voice_profile = {
                "voice_id": "",
                "rate": 0.9 + self.id * 0.035,
                "pitch": 0.8 + self.id * 0.08,
                "volume": 0.8,
            }


@dataclass
class PetAI:
    id: str = field(default_factory=lambda: secrets.token_hex(12))
    name: str = "月牙"
    avatar: str = "🐾"
    personality: str = "detective"
    provider: str = "mock"
    control_mode: str = "copilot"
    delegate_next: bool = False
    relationship_level: int = 1
    play_style: dict[str, Any] = field(default_factory=dict)
    private_chat_history: list[dict[str, Any]] = field(default_factory=list)
    current_game_memory: dict[str, Any] = field(default_factory=dict)
    agent_id: str = field(default_factory=lambda: secrets.token_hex(16))
    model_key: str = ""
    execution_status: dict[str, Any] = field(default_factory=dict)


@dataclass
class WerewolfGame:
    room_id: str
    host_id: str
    game_id: str = field(default_factory=lambda: secrets.token_hex(12))
    title: str = "月下狼人杀"
    pace: str = "standard"
    players: list[Player] = field(default_factory=list)
    pets: dict[str, PetAI] = field(default_factory=dict)
    day: int = 1
    phase: str = "lobby"
    current_turn_player_id: int | None = None
    turn_started_at: float | None = None
    turn_deadline: float | None = None
    turn_duration: float = 0
    turn_sequence: int = 0
    speech_queue: list[int] = field(default_factory=list)
    last_words_queue: list[int] = field(default_factory=list)
    after_last_words: str = "day_speech"
    current_speech: str = ""
    votes: dict[str, int | None] = field(default_factory=dict)
    night_choices: dict[str, int | None] = field(default_factory=dict)
    submitted: list[int] = field(default_factory=list)
    night_kill: int | None = None
    night_poison: int | None = None
    witch_antidote: bool = True
    witch_poison: bool = True
    events: list[dict[str, Any]] = field(default_factory=list)
    wolf_chat: list[dict[str, Any]] = field(default_factory=list)
    game_over: bool = False
    winner: str | None = None
    seq: int = 0
    event_seq: int = 0
    state_revision: int = 0
    seat_presets: dict[str, dict[str, Any]] = field(default_factory=dict)
    unique_model_per_ai_seat: bool = True
    processed_actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    lifecycle: str = "LOBBY"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    archived_at: float | None = None
    creation_action_id: str | None = None
    locked: bool = False
    password_hash: str = ""
    blocked_owners: dict[str, int] = field(default_factory=dict)
    mode: str = "quick6"
    player_count: int = 6
    role_roster: list[str] = field(default_factory=list)
    night_guards: dict[str, int | None] = field(default_factory=dict)
    night_saved: list[int] = field(default_factory=list)
    night_poisons: list[int] = field(default_factory=list)
    death_skill_queue: list[dict[str, Any]] = field(default_factory=list)
    death_context: dict[str, Any] = field(default_factory=dict)
    death_skill_action: str | None = None
    resume_phase: str | None = None
    resume_player_id: int | None = None
    action_history: list[dict[str, Any]] = field(default_factory=list)
    experiment_id: str | None = None
    game_seed: int | str | None = None
    rng_algorithm: str = SECURE_ALGORITHM
    rng_state: Any = None
    ruleset_version: str = RULESET_VERSION
    prompt_version: str = PROMPT_VERSION
    trace_schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (self.game_seed is None) != (self.experiment_id is None):
            raise ValueError("Experiment mode requires both experiment_id and game_seed")
        if self.game_seed is not None:
            if type(self.game_seed) not in (int, str) or self.game_seed == "":
                raise ValueError("Invalid game_seed")
            self.rng_algorithm = SEEDED_ALGORITHM
        # Modes are validated on construction and when lobby configuration changes.
        count = self.player_count if self.mode == "custom" else None
        self.mode, self.player_count, self.role_roster = validate_mode(self.mode, count, self.role_roster or None)

    def wolf_actors(self) -> list[Player]:
        regular = [p for p in self.alive_players() if ROLE_DEFINITIONS[p.role].participates_in_kill]
        return regular or [p for p in self.alive_players() if p.role == "hidden_wolf"]

    def in_wolf_channel(self, p: Player) -> bool:
        return ROLE_DEFINITIONS[p.role].wolf_channel or (
            p.role == "hidden_wolf"
            and not any(ROLE_DEFINITIONS[q.role].participates_in_kill for q in self.alive_players())
        )

    @property
    def turn_id(self) -> str:
        return f"{self.game_id}:{self.turn_sequence}"

    def player(self, pid: int) -> Player:
        return next(p for p in self.players if p.id == pid)

    def owned_player(self, owner_id: str) -> Player | None:
        return next((p for p in self.players if p.owner_id == owner_id), None)

    def alive_players(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def alive_ids(self) -> list[int]:
        return [p.id for p in self.alive_players()]

    def dump(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def restore(cls, value: dict[str, Any]) -> WerewolfGame:
        value = deepcopy(value)
        # Legacy snapshots remain unseeded and retain their historical versions.
        value.setdefault("ruleset_version", "werewolf-v3.0")
        value.setdefault("prompt_version", "autonomous-v3.0")
        migrated_real_seats = False
        defaults = {
            "openai": "gpt-4.1-mini",
            "anthropic": "claude-sonnet-4-20250514",
            "gemini": "gemini-2.5-flash",
            "dashscope": "qwen-plus",
        }
        for p in value["players"]:
            if "model_key" not in p and not p.get("owner_id"):
                provider = p.get("provider", "mock")
                model = os.getenv(f"{provider.upper()}_MODEL") or defaults.get(provider, "rule-based-mock")
                p["model_key"], p["model"] = f"{provider}:{model}", model
                migrated_real_seats |= provider != "mock"
            if not p.get("voice_profile"):
                seat = p["id"]
                p["voice_profile"] = {
                    "voice_id": "",
                    "rate": 0.9 + seat * 0.035,
                    "pitch": 0.8 + seat * 0.08,
                    "volume": 0.8,
                }
        if migrated_real_seats and value.get("phase") != "lobby":
            # Running old games already reuse a model. Disclose compatibility
            # instead of silently changing their binding in the middle of play.
            value["unique_model_per_ai_seat"] = False
        value["players"] = [Player(**p) for p in value["players"]]
        value["pets"] = {owner: PetAI(**pet) for owner, pet in value["pets"].items()}
        game = cls(**value)
        # Old saves used AI placeholders in a lobby; migrate them to presets once.
        if game.phase == "lobby":
            for p in game.players:
                if p.owner_id is None:
                    game.seat_presets.setdefault(
                        str(p.id),
                        {
                            "id": p.id,
                            "provider": p.provider,
                            "personality": p.personality,
                            "model_key": p.model_key if p.model != "mock" else "",
                            "model_locked": p.model_locked,
                        },
                    )
            game.players = [p for p in game.players if p.owner_id]
        if game.phase != "lobby" and game.lifecycle == "LOBBY":
            game.lifecycle = "FINISHED" if game.game_over else "ACTIVE"
        return game
