"""Post-game observations from public speech, never an in-game strategy input.

These are explicit language declarations, not access to a model's private
beliefs. Claims stay unconfirmed even after the game has revealed actual roles.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .memory import _quoted_claim, new_memory, update_memory

SEAT = r"([1-9][0-9]?)号"
STANCE_PATTERNS = {
    "support": r"(?<!不)(?:站边|支持|相信|信任|力保|跟随)" + SEAT,
    "suspect": r"(?:最怀疑|更怀疑|主要怀疑|怀疑|不相信|不信)" + SEAT,
    "oppose": r"(?:不支持|不站边|反对)" + SEAT,
    "neutral": r"(?:不(?:再)?怀疑|(?:不再|停止)(?:支持|相信|站边|信任))" + SEAT,
}


def _reported_or_past(text: str, start: int) -> bool:
    if _quoted_claim(text, start):
        return True
    prefix = re.split(r"[。；！？\n]", text[:start])[-1]
    if re.search(r"(?:他|她|有人|队友)(?:刚才|之前|曾)?(?:说|声称|表示|认为)", prefix):
        return True
    return bool(re.search(r"(?:从未|没有|从来没|并非|并未|没|不是|不|未|不再|曾|曾经|以前|此前|之前|原先|一度|过去|最初|当时|昨天|昨晚)$", prefix))


def _stances(text: str) -> list[dict[str, Any]]:
    declarations = []
    for stance, pattern in STANCE_PATTERNS.items():
        for match in re.finditer(pattern, text):
            if _reported_or_past(text, match.start()):
                continue
            declarations.append({"target_player_id": int(match.group(1)), "stance": stance,
                                 "text": match.group(0), "offset": match.start()})
    return sorted(declarations, key=lambda item: item["offset"])


def analyze_public_speeches(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract per-seat public declaration history and explicit target changes.

    Accepts persisted event envelopes or their unwrapped chat-message data.
    Only changes in stance *about the same target* count as judgment changes;
    supporting an additional player does not imply abandoning an earlier one.
    """
    players: dict[str, dict[str, Any]] = {}
    last_stances: dict[tuple[int, int], dict[str, Any]] = {}
    last_roles: dict[int, dict[str, Any]] = {}
    last_checks: dict[tuple[int, int], dict[str, Any]] = {}
    seen: set[str] = set()
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        data = event.get("data", event)
        if not isinstance(data, dict) or event.get("audience", data.get("visibility", "public")) != "public":
            continue
        kind = data.get("kind", event.get("type"))
        if kind != "speech":
            continue
        pid = data.get("player_id", event.get("player_id"))
        if type(pid) is not int:
            continue
        text = data.get("speech", data.get("text", ""))
        if not isinstance(text, str):
            continue
        event_id = str(event.get("event_id") or data.get("event_id") or f"public-speech:{index}")
        if event_id in seen:
            continue
        seen.add(event_id)
        day = data.get("day", event.get("day", 1))
        provenance = {"event_id": event_id, "day": day, "player_id": pid,
                      "visibility": "public", "confirmed": False}
        actor = players.setdefault(str(pid), {"speech_count": 0, "stance_history": [], "judgment_changes": [],
            "judgment_change_count": 0, "role_claims": [], "check_claims": [], "claim_changes": []})
        actor["speech_count"] += 1
        for stance in _stances(text):
            stance.pop("offset")
            current = {**provenance, **stance}
            actor["stance_history"].append(current)
            pair = (pid, current["target_player_id"])
            previous = last_stances.get(pair)
            if previous and previous["stance"] != current["stance"]:
                actor["judgment_changes"].append({**provenance,
                    "target_player_id": current["target_player_id"], "previous_stance": previous["stance"],
                    "stance": current["stance"], "previous_event_id": previous["event_id"], "text": current["text"]})
            last_stances[pair] = current
        parsed = update_memory(new_memory(), {"type": "speech", "event_id": event_id,
            "day": day, "audience": "public", "data": {"day": day, "player_id": pid, "speech": text}}, pid)
        for claim in parsed["claims"]:
            actor["role_claims"].append(deepcopy(claim))
            previous = last_roles.get(pid)
            if previous and previous["claimed_role"] != claim["claimed_role"]:
                actor["claim_changes"].append({**provenance, "type": "role_claim_change",
                    "previous_role": previous["claimed_role"], "claimed_role": claim["claimed_role"],
                    "previous_event_id": previous["event_id"]})
            last_roles[pid] = claim
        for claim in parsed["check_claims"]:
            actor["check_claims"].append(deepcopy(claim))
            if claim["result"] == "unreported":
                continue
            pair = (pid, claim["target_player_id"])
            previous = last_checks.get(pair)
            if previous and previous["result"] != claim["result"]:
                actor["claim_changes"].append({**provenance, "type": "check_claim_change",
                    "target_player_id": claim["target_player_id"], "previous_result": previous["result"],
                    "result": claim["result"], "previous_event_id": previous["event_id"]})
            last_checks[pair] = claim
        actor["judgment_change_count"] = len(actor["judgment_changes"])
    return {"players": players, "speech_count": len(seen),
            "judgment_change_count": sum(actor["judgment_change_count"] for actor in players.values()),
            "note": "仅统计公开发言中明确的立场和声明；不代表模型私有判断，不确认身份或查验真实性。"}
