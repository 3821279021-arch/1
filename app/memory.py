"""Incremental memories built from events that InformationScope already permits.

The extractor never receives GameState and never guesses hidden identities.  A
public claim records what a player said, while only host facts or a player's own
private skill result can become a confirmed fact.  Stored semantic history is
independent of the short raw-event window used in model prompts.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

SCHEMA_VERSION = 1
ROLE_WORDS = {
    "预言家": "seer",
    "女巫": "witch",
    "村民": "villager",
    "狼人": "wolf",
    "猎人": "hunter",
    "守卫": "guard",
    "骑士": "knight",
    "白痴": "idiot",
    "狼王": "wolf_king",
    "白狼王": "white_wolf_king",
    "狼美人": "wolf_beauty",
    "隐狼": "hidden_wolf",
}
IGNORED_EVENTS = {
    "speech_chunk",
    "speech_started",
    "speech_finished",
    "timer_sync",
    "turn_started",
    "turn_finished",
    "phase_changed",
    "vote_started",
    "vote_submitted",
    "vote_result",
    "private_pet_message",
}

MEMORY_LIMITS = {
    "facts": 36,
    "claims": 12,
    "check_claims": 12,
    "stances": 12,
    "beliefs": 24,
    "contradictions": 12,
    "vote_history": 18,
    "received_votes": 18,
    "self_history": 18,
    "judgments": 10,
    "conjectures": 6,
    "speeches": 12,
    "recent_events": 12,
    "processed_event_ids": 512,
    "daily_summaries": 3,
}
MEMORY_LIMITS["semantic_hints"] = 12


def _summary() -> dict[str, Any]:
    return {
        "role_claims": {},
        "check_claims": {},
        "vote_summary": {},
        "stances": {},
        "confirmed_facts": [],
        "contradictions": [],
        "self_history": {},
        "judgments": {},
        "received_votes": {"rounds": 0, "total": 0, "from_player_ids": {}},
        "folded_counts": {},
        "self_plan": "核对已知事实与声称，再决定行动。",
    }


def compact_memory(memory: dict[str, Any], day: int | None = None) -> dict[str, Any]:
    """Bound every collection, folding older evidence into fixed-size indexes.

    Summary entries retain source/event IDs and confirmed/visibility flags.
    A public claim never becomes a host-confirmed identity during compression.
    """
    summary = memory.setdefault("summary", _summary())
    for key, default in _summary().items():
        summary.setdefault(key, deepcopy(default))
    previous_day = memory.get("current_day", day or 1)
    if day is not None and day > previous_day:
        roles = deepcopy(summary["role_claims"])
        for entry in memory.get("claims", []):
            state = roles.setdefault(str(entry["player_id"]), {"first": deepcopy(entry)})
            state["latest"] = deepcopy(entry)
        votes = deepcopy(summary["vote_summary"])
        for entry in memory.get("vote_history", []):
            for actor, target in entry.get("votes", {}).items():
                pair = f"{actor}:{target}"
                votes[pair] = votes.get(pair, 0) + 1
        daily = {
            "day": previous_day,
            "confirmed_facts": deepcopy(
                (summary["confirmed_facts"] + [f for f in memory.get("facts", []) if f.get("confirmed")])[-6:]
            ),
            "role_claims": roles,
            "vote_patterns": votes,
            "contradictions": deepcopy((summary["contradictions"] + memory.get("contradictions", []))[-6:]),
            "suspicions": deepcopy(memory.get("suspicions", {})),
            "key_relationships": deepcopy(memory.get("stances", [])[-6:]),
            "self_plan": summary["self_plan"],
        }
        memory.setdefault("daily_summaries", []).append(daily)
        memory["suspicions"] = {
            key: round(value * 0.65 ** (day - previous_day), 3) for key, value in memory.get("suspicions", {}).items()
        }
    memory["current_day"] = max(previous_day, day or previous_day)
    for key, limit in MEMORY_LIMITS.items():
        values = memory.setdefault(key, [])
        if len(values) <= limit:
            continue
        overflow = values[:-limit]
        summary["folded_counts"][key] = summary["folded_counts"].get(key, 0) + len(overflow)
        if key == "claims":
            for entry in overflow:
                pid = str(entry["player_id"])
                state = summary["role_claims"].setdefault(pid, {"first": deepcopy(entry), "latest": deepcopy(entry)})
                state["latest"] = deepcopy(entry)
        elif key == "check_claims":
            for entry in overflow:
                pair = f"{entry['player_id']}:{entry['target_player_id']}"
                state = summary["check_claims"].setdefault(pair, {"first": deepcopy(entry), "latest": deepcopy(entry)})
                state["latest"] = deepcopy(entry)
        elif key == "stances":
            for entry in overflow:
                summary["stances"][f"{entry['player_id']}:{entry['target_player_id']}"] = deepcopy(entry)
        elif key == "vote_history":
            for entry in overflow:
                for actor, target in entry.get("votes", {}).items():
                    pair = f"{actor}:{target}"
                    summary["vote_summary"][pair] = summary["vote_summary"].get(pair, 0) + 1
        elif key == "facts":
            for entry in overflow:
                if entry.get("confirmed"):
                    summary["confirmed_facts"] = (summary["confirmed_facts"] + [deepcopy(entry)])[-12:]
        elif key == "self_history":
            for entry in overflow:
                compact = deepcopy(entry)
                if isinstance(compact.get("text"), str):
                    compact["text"] = compact["text"][:160]
                category = entry.get("action") or entry.get("type", "unknown")
                state = summary["self_history"].setdefault(category, {"first": compact})
                state["latest"] = compact
        elif key == "judgments":
            for entry in overflow:
                summary["judgments"][entry.get("action", "unknown")] = deepcopy(entry)
        elif key == "received_votes":
            for entry in overflow:
                summary["received_votes"]["rounds"] += 1
                summary["received_votes"]["total"] += entry.get("count", 0)
                for pid in entry.get("from_player_ids", []):
                    counts = summary["received_votes"]["from_player_ids"]
                    counts[str(pid)] = counts.get(str(pid), 0) + 1
        elif key == "contradictions":
            summary["contradictions"] = (summary["contradictions"] + deepcopy(overflow))[-12:]
        memory[key] = values[-limit:]
    return memory


def new_memory() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "facts": [],
        "claims": [],
        "check_claims": [],
        "stances": [],
        "beliefs": [],
        "contradictions": [],
        "vote_history": [],
        "received_votes": [],
        "self_history": [],
        "judgments": [],
        "suspicions": {},
        "role_guesses": {},
        "conjectures": [],
        "speeches": [],
        "recent_events": [],
        "processed_event_ids": [],
        "observed_event_count": 0,
        "summary": _summary(),
        "daily_summaries": [],
        "current_day": 1,
        "semantic_hints": [],
    }


def _event_parts(event: dict[str, Any]) -> tuple[str, dict[str, Any], str, int | None, str]:
    data = event.get("data", event)
    if not isinstance(data, dict):
        data = {"text": str(data)}
    kind = event.get("type", data.get("kind", "system"))
    if kind == "chat_message":
        kind = data.get("kind", "system")
    audience = event.get("audience", data.get("visibility", "public"))
    pid = data.get("player_id", event.get("player_id"))
    event_id = event.get("event_id") or data.get("event_id")
    if not event_id:
        seq = data.get("seq", event.get("seq"))
        fingerprint = json.dumps(
            [kind, seq, data.get("day", event.get("day")), pid, data], sort_keys=True, ensure_ascii=False, default=str
        )
        event_id = f"memory:{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"
    return kind, data, audience, pid, str(event_id)


def _append_fact(
    memory: dict[str, Any],
    event_id: str,
    day: int,
    text: str,
    source_pid: int | None,
    visibility: str,
    *,
    kind: str,
    confirmed: bool,
    **details: Any,
) -> None:
    memory["facts"].append(
        {
            "id": f"{event_id}:f{len(memory['facts'])}",
            "event_id": event_id,
            "day": day,
            "text": text,
            "source_player_id": source_pid,
            "source": "host"
            if confirmed and visibility == "public"
            else "own_skill"
            if confirmed
            else "player_statement",
            "visibility": visibility,
            "kind": kind,
            "confirmed": confirmed,
            **details,
        }
    )


def _belief(
    memory: dict[str, Any], pid: int, role: str, confidence: float, event_id: str, *, confirmed: bool = False
) -> None:
    # Public speech only creates tentative beliefs. A real private check takes
    # priority over later claims; it must never be replaced by a public guess.
    previous = next((b for b in memory["beliefs"] if b["player_id"] == pid), None)
    if previous and previous.get("confirmed") and not confirmed:
        return
    belief = {
        "player_id": pid,
        "role_guess": role,
        "confidence": confidence,
        "confirmed": confirmed,
        "basis_event_id": event_id,
    }
    memory["beliefs"] = [b for b in memory["beliefs"] if b["player_id"] != pid] + [belief]
    memory["role_guesses"][str(pid)] = role if confirmed else f"{role}（待验证）"


def _contradiction(
    memory: dict[str, Any],
    pid: int,
    day: int,
    summary: str,
    earlier: str,
    later: str,
    *,
    change_only: bool = False,
    visibility: str = "public",
) -> None:
    memory["contradictions"].append(
        {
            "player_id": pid,
            "day": day,
            "summary": summary,
            "event_ids": [earlier, later],
            "confirmed": False,
            "requires_explanation": True,
            "visibility": visibility,
            "kind": "stance_change" if change_only else "inconsistent_claim",
        }
    )


def _quoted_claim(text: str, start: int) -> bool:
    prefix = re.split(r"[。；！？\n]", text[:start])[-1]
    # Repeating or questioning another player's report is not a new claim by
    # this speaker. The complete utterance remains in the public event layer.
    reported = re.search(r"[1-9][0-9]?号(?:曾|刚才|之前|上轮|今天|昨夜)?(?:自称|声称|说|称|报|提到|表示)", prefix)
    subject = re.fullmatch(r"\s*(?:昨天|昨晚|昨夜|今天|此前|上轮|请|追问|询问|问)?[1-9][0-9]?号[，,：:]?\s*", prefix)
    return bool(reported or subject)


def _speech(
    memory: dict[str, Any], text: str, pid: int | None, self_pid: int, day: int, event_id: str, visibility: str
) -> None:
    if pid is None:
        return
    entry = {"event_id": event_id, "day": day, "player_id": pid, "text": text, "visibility": visibility}
    memory["speeches"] = (memory["speeches"] + [deepcopy(entry)])[-12:]
    if pid == self_pid:
        memory["self_history"].append({**entry, "type": "speech"})
    hints = memory.setdefault("semantic_hints", [])
    for match in re.finditer(r"([1-9][0-9]?)号?(?:怎么看都不太对|大概率(?:进狼坑|是狼)|进狼坑|像狼)", text):
        hints.append(
            {
                "event_id": event_id,
                "day": day,
                "source_player_id": pid,
                "targets": [int(match.group(1))],
                "relation": "suspect",
                "confidence": 0.25,
                "confirmed": False,
                "visibility": visibility,
            }
        )
    for match in re.finditer(r"([1-9][0-9]?)[、，,]([1-9][0-9]?)(?:号)?至少出一狼", text):
        hints.append(
            {
                "event_id": event_id,
                "day": day,
                "source_player_id": pid,
                "targets": [int(match.group(1)), int(match.group(2))],
                "relation": "at_least_one_wolf",
                "confidence": 0.25,
                "confirmed": False,
                "visibility": visibility,
            }
        )
    # Explicit first-person declarations only. "2号像预言家" is not a claim by
    # the speaker to hold that role, and "我不是预言家" must not become one.
    for match in re.finditer(
        r"(?:我(?:是|就是|自称|这(?:张)?(?:牌)?是)|我的身份(?:是|为))\s*(?:个|一名|一张)?(预言家|女巫|村民|白狼王|狼美人|隐狼|狼人|狼王|猎人|守卫|骑士|白痴)",
        text,
    ):
        if _quoted_claim(text, match.start()):
            continue
        role = ROLE_WORDS[match.group(1)]
        previous = next((c for c in reversed(memory["claims"]) if c["player_id"] == pid), None)
        if previous is None:
            previous = memory.get("summary", {}).get("role_claims", {}).get(str(pid), {}).get("latest")
        claim = {
            "event_id": event_id,
            "player_id": pid,
            "claimed_role": role,
            "day": day,
            "text": match.group(0),
            "visibility": visibility,
            "confirmed": False,
        }
        memory["claims"].append(claim)
        _append_fact(
            memory,
            event_id,
            day,
            f"{pid}号声称自己是{match.group(1)}（未经确认）",
            pid,
            visibility,
            kind="reported_role_claim",
            confirmed=False,
            claimed_role=role,
        )
        _belief(memory, pid, role, 0.35, event_id)
        if previous and previous["claimed_role"] != role:
            _contradiction(
                memory,
                pid,
                day,
                f"{pid}号身份声称由{previous['claimed_role']}变为{role}，需追问原因。",
                previous["event_id"],
                event_id,
                visibility=visibility,
            )
    check_patterns = (
        (
            r"(?:查杀|查验(?:了|过)?|验了|验出|查了|验)([1-9][0-9]?)号(?:[^。；，]{0,10}?(?:是|为)?(狼人|好人|金水))?",
            None,
        ),
        (r"([1-9][0-9]?)号(?:是|为|给了我|给)?(?:我的)?(查杀|金水)", None),
        (r"(?:给|发)([1-9][0-9]?)号(?:一张|个)?(查杀|金水)", None),
    )
    extracted: set[tuple[int, str]] = set()
    for pattern, _ in check_patterns:
        for match in re.finditer(pattern, text):
            if _quoted_claim(text, match.start()):
                continue
            # A denial of performing a check is not a claimed check result.
            # Negating the result *after* the target is handled separately.
            prefix = re.split(r"[。；，,！？\n]", text[: match.start()])[-1]
            if re.search(r"(?:没(?:有)?|并未|从未|未曾|不是|不曾)$", prefix):
                continue
            target = int(match.group(1))
            wording = match.group(2) if len(match.groups()) > 1 else None
            after_target = text[match.start() :].split("号", 1)[-1][:24]
            clauses = re.split(r"[。，,；！？\n]", after_target)
            after_target = clauses[0]
            if len(clauses) > 1 and re.match(r"\s*(?:他|此人|结果)", clauses[1]):
                after_target += clauses[1]
            after_target = re.split(r"[1-9][0-9]?号", after_target)[0]
            negated_wolf = bool(re.search(r"(?:不是|并非|非)(?:狼人|狼)(?![坑队])", after_target))
            explicit_wolf = bool(re.search(r"(?:是|为)(?:狼人|狼)(?![坑队])", after_target))
            result = (
                "good"
                if negated_wolf
                else "wolf"
                if wording in {"狼人", "查杀"} or match.group(0).startswith("查杀") or explicit_wolf
                else "good"
                if wording in {"好人", "金水"}
                else "unreported"
            )
            if (target, result) in extracted:
                continue
            extracted.add((target, result))
            prior = next(
                (
                    c
                    for c in reversed(memory["check_claims"])
                    if c["player_id"] == pid and c["target_player_id"] == target and c["result"] != "unreported"
                ),
                None,
            )
            if prior is None:
                prior = memory.get("summary", {}).get("check_claims", {}).get(f"{pid}:{target}", {}).get("latest")
            check = {
                "event_id": event_id,
                "player_id": pid,
                "target_player_id": target,
                "result": result,
                "day": day,
                "text": match.group(0),
                "confirmed": False,
                "visibility": visibility,
            }
            memory["check_claims"].append(check)
            _append_fact(
                memory,
                event_id,
                day,
                f"{pid}号发言提到“{match.group(0)}”，涉及{target}号的{result}查验声称（未经确认）",
                pid,
                visibility,
                kind="reported_check_claim",
                confirmed=False,
                target_player_id=target,
                claimed_alignment=result,
            )
            if result != "unreported":
                _belief(memory, target, result, 0.3, event_id)
            if prior and result != "unreported" and prior["result"] != result:
                _contradiction(
                    memory,
                    pid,
                    day,
                    f"{pid}号对{target}号的查验声称前后不同，需核对查验时间。",
                    prior["event_id"],
                    event_id,
                    visibility=visibility,
                )
    stance_patterns = {
        "suspect": r"(?:最怀疑|更怀疑|主要怀疑|怀疑|不信|不相信|不支持|不站边|反对)([1-9][0-9]?)号",
        "support": r"(?<!不)(?:站边|支持|相信|信任|力保|跟随)([1-9][0-9]?)号",
    }
    for stance, pattern in stance_patterns.items():
        for match in re.finditer(pattern, text):
            # Negating a former stance is evidence of a possible contradiction,
            # not a new positive endorsement ("从未支持2号").
            if stance == "support" and re.search(
                r"(?:从未|没有|从来没|不再|并非|没)$", text[max(0, match.start() - 5) : match.start()]
            ):
                continue
            target = int(match.group(1))
            old = next(
                (
                    s
                    for s in reversed(memory["stances"])
                    if s["player_id"] == pid and (s["stance"] == stance or s["target_player_id"] == target)
                ),
                None,
            )
            if old is None:
                candidates = [
                    s
                    for s in memory.get("summary", {}).get("stances", {}).values()
                    if s["player_id"] == pid and (s["stance"] == stance or s["target_player_id"] == target)
                ]
                old = max(candidates, key=lambda s: s["day"]) if candidates else None
            current = {
                "event_id": event_id,
                "player_id": pid,
                "target_player_id": target,
                "stance": stance,
                "day": day,
                "text": match.group(0),
                "visibility": visibility,
            }
            memory["stances"].append(current)
            if stance == "suspect":
                memory["suspicions"][str(target)] = memory["suspicions"].get(str(target), 0) + 1
                _belief(memory, target, "wolf", 0.25, event_id)
            if old and (old["target_player_id"] != target or old["stance"] != stance):
                _contradiction(
                    memory,
                    pid,
                    day,
                    f"{pid}号立场由{old['stance']} {old['target_player_id']}号变为{stance} {target}号；立场变化本身不证明说谎。",
                    old["event_id"],
                    event_id,
                    change_only=True,
                    visibility=visibility,
                )
    if re.search(r"(?:从未|没(?:有)?|从来没)(?:相信|支持|站边|怀疑)", text):
        for old in [s for s in memory["stances"] if s["player_id"] == pid and s["event_id"] != event_id][-4:]:
            verb = "怀疑" if old["stance"] == "suspect" else "支持"
            if re.search(rf"(?:从未|没(?:有)?|从来没)(?:相信|支持|站边|怀疑){old['target_player_id']}号", text):
                _contradiction(
                    memory,
                    pid,
                    day,
                    f"{pid}号否认曾{verb}{old['target_player_id']}号，但此前已有对应发言记录。",
                    old["event_id"],
                    event_id,
                    visibility=visibility,
                )


def update_memory(memory: dict[str, Any] | None, event: dict[str, Any], self_pid: int) -> dict[str, Any]:
    """Return an independent updated memory; callers must pre-filter by scope.

    The additional player check prevents a accidentally forwarded private
    event from entering another player's memory. Wolf-event eligibility is
    necessarily determined by InformationScope because no role is passed here.
    """
    current = (
        deepcopy(memory)
        if isinstance(memory, dict) and memory.get("schema_version") == SCHEMA_VERSION
        else new_memory()
    )
    kind, data, audience, pid, event_id = _event_parts(event)
    if kind in IGNORED_EVENTS or audience == "player" and event.get("player_id", pid) != self_pid:
        return current
    if event_id in current["processed_event_ids"]:
        return current
    current["processed_event_ids"].append(event_id)
    current["observed_event_count"] += 1
    day = int(data.get("day", event.get("day", 1)))
    compact_memory(current, day)
    visibility = "private" if audience == "player" else "wolves" if audience == "wolves" else "public"
    text = str(data.get("speech", data.get("text", data.get("note", ""))))
    if kind in {"speech", "wolf_chat_message", "wolf_discussion"}:
        _speech(current, text, pid, self_pid, day, event_id, visibility)
    elif kind in {"death", "vote", "system", "game_finished"}:
        if text:
            _append_fact(current, event_id, day, text, pid, visibility, kind=kind, confirmed=True)
        if kind == "vote":
            votes = data.get("votes", {})
            current["vote_history"].append({"event_id": event_id, "day": day, "votes": deepcopy(votes)})
            received = [int(actor) for actor, target in votes.items() if target == self_pid]
            current["received_votes"].append(
                {
                    "event_id": event_id,
                    "day": day,
                    "target_player_id": self_pid,
                    "from_player_ids": received,
                    "count": len(received),
                }
            )
            if str(self_pid) in votes:
                current["self_history"].append(
                    {
                        "event_id": event_id,
                        "day": day,
                        "type": "vote",
                        "target": votes[str(self_pid)],
                        "visibility": "public",
                    }
                )
            for target in votes.values():
                if target is not None:
                    key = str(target)
                    current["suspicions"][key] = current["suspicions"].get(key, 0) + 1
    elif kind in {"private_note", "skill_result", "player_action"} and visibility == "private":
        action = data.get("action", "skill_result")
        if kind == "player_action":
            current["self_history"].append(
                {
                    "event_id": event_id,
                    "day": day,
                    "type": "action",
                    "action": action,
                    "target": data.get("target"),
                    "save": data.get("save"),
                    "poison_target": data.get("poison_target"),
                    "visibility": "private",
                }
            )
            current["judgments"].append(
                {
                    "event_id": event_id,
                    "day": day,
                    "action": action,
                    "target": data.get("target"),
                    "status": "自己的选择，不代表已确认身份",
                }
            )
        notes = data.get("notes", data.get("private_notes", [text] if text else []))
        if isinstance(notes, str):
            notes = [notes]
        for index, note in enumerate(notes):
            if not isinstance(note, str):
                continue
            _append_fact(
                current,
                f"{event_id}:note{index}",
                day,
                note,
                self_pid,
                visibility,
                kind="private_check" if "查验" in note else "own_skill_result",
                confirmed=True,
            )
            match = re.search(r"查验[：:]\s*([1-9][0-9]?)号是(狼人|好人)", note)
            if match:
                _belief(
                    current,
                    int(match.group(1)),
                    "wolf" if match.group(2) == "狼人" else "good",
                    1.0,
                    event_id,
                    confirmed=True,
                )
    if text or kind == "player_action":
        current["recent_events"] = (
            current["recent_events"]
            + [
                {
                    "event_id": event_id,
                    "day": day,
                    "kind": kind,
                    "player_id": pid,
                    "text": text[:500],
                    "visibility": visibility,
                }
            ]
        )[-12:]
    current["conjectures"] = [
        f"{pid}号被公开怀疑或收到投票{count}次；这不证明其身份。" for pid, count in current["suspicions"].items()
    ]
    return compact_memory(current, day)


def build_memory_from_view(view: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Use stored incremental memory, with a one-time old-save migration path."""
    stored = view.get("self", {}).get("memory", view.get("memory", previous))
    if isinstance(stored, dict) and stored.get("schema_version") == SCHEMA_VERSION:
        return compact_memory(deepcopy(stored))
    current = new_memory()
    for event in view.get("events", []):
        current = update_memory(current, event, view["self"]["id"])
    for event in view.get("wolf_chat", []):
        current = update_memory(
            current, {"type": "wolf_chat_message", "data": event, "audience": "wolves"}, view["self"]["id"]
        )
    for i, note in enumerate(view.get("self", {}).get("private_notes", [])):
        current = update_memory(
            current,
            {
                "type": "private_note",
                "event_id": f"legacy-note:{i}",
                "day": view.get("day", 1),
                "audience": "player",
                "player_id": view["self"]["id"],
                "data": {"note": note},
            },
            view["self"]["id"],
        )
    if isinstance(stored, dict):
        current["judgments"].extend(deepcopy(stored.get("judgments", [])))
    return compact_memory(current, view.get("day"))


def prompt_memory(memory: dict[str, Any]) -> dict[str, Any]:
    """Compact durable semantic history rather than replaying every raw speech."""
    result = compact_memory(deepcopy(memory))
    result.pop("processed_event_ids", None)
    result.pop("speeches", None)
    result.pop("recent_events", None)
    # Early role/check claims and all contradictions stay available, while
    # routine host announcements are represented by recent facts only.
    important = [
        fact
        for fact in result.get("facts", [])
        if fact.get("kind") in {"reported_role_claim", "reported_check_claim", "private_check"}
    ]
    ordinary = [
        fact
        for fact in result.get("facts", [])
        if fact.get("kind") not in {"reported_role_claim", "reported_check_claim", "private_check"}
    ][-12:]
    result["facts"] = important + ordinary
    # Full recent utterances live in the recent-events layer of the prompt.
    result.pop("daily_summaries", None)
    result.pop("conjectures", None)
    result.pop("role_guesses", None)
    result.pop("received_votes", None)
    result.pop("observed_event_count", None)
    result["self_history"] = result.get("self_history", [])[-6:]
    for key in ("facts", "self_history"):
        for item in result.get(key, []):
            if isinstance(item.get("text"), str):
                item["text"] = item["text"][:240]
    summary = result["summary"]
    summary["role_claims"] = {
        pid: {
            "first_role": entry["first"]["claimed_role"],
            "latest_role": entry["latest"]["claimed_role"],
            "event_ids": [entry["first"]["event_id"], entry["latest"]["event_id"]],
            "confirmed": False,
            "visibility": entry["latest"].get("visibility", "public"),
        }
        for pid, entry in summary["role_claims"].items()
    }
    summary["check_claims"] = {
        pair: {
            "first_result": entry["first"]["result"],
            "latest_result": entry["latest"]["result"],
            "event_ids": [entry["first"]["event_id"], entry["latest"]["event_id"]],
            "confirmed": False,
            "visibility": entry["latest"].get("visibility", "public"),
        }
        for pair, entry in summary["check_claims"].items()
    }
    summary.pop("stances", None)
    summary["contradictions"] = summary["contradictions"][-6:]
    return result
