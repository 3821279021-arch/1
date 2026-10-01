"""Explainable beliefs and decisions from an actor's InformationScope only."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class GameBeliefState:
    alignment_probabilities: dict[int, float] = field(default_factory=dict)
    role_probabilities: dict[int, dict[str, float]] = field(default_factory=dict)
    credibility: dict[int, float] = field(default_factory=dict)
    claim_consistency: dict[int, float] = field(default_factory=dict)
    vote_pressure: dict[int, float] = field(default_factory=dict)
    relationships: dict[int, str] = field(default_factory=dict)
    evidence: dict[int, list[str]] = field(default_factory=dict)
    strategy_plan: str = ""
    wolf_plan: dict[str, Any] | None = None
    own_checks: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_view(cls, view: dict[str, Any], memory: dict[str, Any]):
        state = cls()
        day = view["day"]
        me = view["self"]["id"]
        ids = [p["id"] for p in view["players"]]
        for pid in ids:
            state.alignment_probabilities[pid] = 0.33
            state.credibility[pid] = 0.6
            state.claim_consistency[pid] = 1.0
            state.vote_pressure[pid] = 0.0
            state.evidence[pid] = []
            state.relationships[pid] = "自己" if pid == me else "待观察"
        summary = memory.get("summary", {})
        claims = {
            int(pid): entry["latest"]
            for pid, entry in summary.get("role_claims", {}).items()
            if entry["latest"].get("visibility", "public") == "public"
        }
        for entry in memory.get("claims", []):
            if entry.get("visibility", "public") == "public":
                claims[entry["player_id"]] = entry
        for role in ("seer", "witch"):
            claimants = [pid for pid, entry in claims.items() if entry["claimed_role"] == role and pid in ids]
            if len(claimants) > 1:
                for pid in claimants:
                    state.alignment_probabilities[pid] += 0.2
                    state.credibility[pid] -= 0.15
                    state.evidence[pid].append(f"公开有多个{role}声明，身份尚未确认。")
        contradictions = summary.get("contradictions", []) + memory.get("contradictions", [])
        for entry in contradictions:
            pid = entry["player_id"]
            if pid not in ids:
                continue
            weight = 0.65 ** max(0, day - entry["day"])
            score = 0.06 if entry.get("kind") == "stance_change" else 0.16
            state.alignment_probabilities[pid] += score * weight
            state.credibility[pid] -= score * weight
            state.claim_consistency[pid] -= score * weight
            state.evidence[pid].append(
                entry["summary"] + ("（私有信息）" if entry.get("visibility", "public") != "public" else "")
            )
        for entry in memory.get("stances", []):
            pid = entry["target_player_id"]
            if pid in ids:
                weight = 0.65 ** max(0, day - entry["day"])
                state.alignment_probabilities[pid] += (0.035 if entry["stance"] == "suspect" else -0.015) * weight
        for entry in memory.get("semantic_hints", []):
            for pid in entry["targets"]:
                if pid in ids:
                    state.alignment_probabilities[pid] += (
                        0.02 * entry["confidence"] * 0.65 ** max(0, day - entry["day"])
                    )
        for entry in memory.get("vote_history", []):
            for actor, target in entry.get("votes", {}).items():
                if target not in ids:
                    continue
                weight = 0.65 ** max(0, day - entry["day"])
                state.vote_pressure[target] += weight
                state.alignment_probabilities[target] += 0.035 * weight
                state.evidence[target].append(f"D{entry['day']} 收到{actor}号公开投票；票型不证明身份。")
        # Own confirmed checks override every public accusation or claim.
        for belief in memory.get("beliefs", []):
            pid = belief["player_id"]
            if pid in ids and belief.get("confirmed"):
                state.alignment_probabilities[pid] = 0.99 if belief["role_guess"] == "wolf" else 0.01
                state.evidence[pid] = ["自己的已确认查验（私有信息）。"]
                if view["self"].get("role_key") == "seer":
                    state.own_checks.append(
                        {
                            "player_id": pid,
                            "alignment": "wolf" if belief["role_guess"] == "wolf" else "good",
                            "source_event_id": belief.get("basis_event_id"),
                            "confirmed": True,
                        }
                    )
        if view["self"].get("role_key") == "wolf":
            teammates = {me, *(p["id"] for p in view.get("wolf_teammates", []))}
            for pid in teammates & set(ids):
                state.alignment_probabilities[pid] = 1.0
                state.relationships[pid] = "狼队"
            enemies = [p["id"] for p in view["players"] if p["alive"] and p["id"] not in teammates]
            target = (
                max(
                    enemies,
                    key=lambda pid: (claims.get(pid, {}).get("claimed_role") == "seer", state.credibility[pid], -pid),
                )
                if enemies
                else None
            )
            state.wolf_plan = {
                "kill_target": target,
                "fake_claim_plan": "避免编造系统查验",
                "teammate_distance_strategy": "公开讨论使用公开证据，不暴露队友",
                "push_target": min(enemies) if enemies else None,
                "risk_level": "moderate",
            }
        for pid in ids:
            probability = round(max(0.01, min(0.99, state.alignment_probabilities[pid])), 3)
            state.alignment_probabilities[pid] = probability
            good_weights = {"seer": 0.25, "witch": 0.25, "villager": 0.5}
            claimed = claims.get(pid, {}).get("claimed_role")
            if claimed in good_weights:
                good_weights = {role: 0.6 if role == claimed else 0.2 for role in good_weights}
            state.role_probabilities[pid] = {
                "wolf": probability,
                **{role: round((1 - probability) * weight, 3) for role, weight in good_weights.items()},
            }
            state.credibility[pid] = round(max(0.05, state.credibility[pid]), 3)
            state.claim_consistency[pid] = round(max(0.05, state.claim_consistency[pid]), 3)
            state.evidence[pid] = state.evidence[pid][-3:]
        own_role = view["self"].get("role_key")
        if own_role in {"wolf", "seer", "witch", "villager"}:
            state.alignment_probabilities[me] = 0.99 if own_role == "wolf" else 0.01
            state.role_probabilities[me] = {
                role: float(role == own_role) for role in ("wolf", "seer", "witch", "villager")
            }
        state.strategy_plan = "优先自己的查验，再核对身份冲突、前后矛盾与公开票型；旧怀疑随天数衰减。"
        return state

    def decision(self, action: str, options: list[int], pending: dict[str, Any]) -> dict[str, Any]:
        if action == "witch":
            killed = pending.get("killed")
            save = bool(
                pending.get("antidote") and killed is not None and self.alignment_probabilities.get(killed, 0.33) < 0.65
            )
            poison = max(options, key=lambda pid: self.alignment_probabilities.get(pid, 0.33)) if options else None
            if not pending.get("poison") or poison is None or self.alignment_probabilities.get(poison, 0.33) < 0.85:
                poison = None
            return {"save": save, "poison_target": poison}
        if not options:
            return {"target": None}
        if action == "wolf_kill" and self.wolf_plan:
            target = self.wolf_plan["kill_target"]
        elif action == "seer_inspect":
            target = min(options, key=lambda pid: (abs(self.alignment_probabilities.get(pid, 0.33) - 0.5), pid))
        else:
            candidates = [
                pid for pid in options if not self.wolf_plan or self.relationships.get(pid) != "狼队"
            ] or options
            target = max(
                candidates,
                key=lambda pid: (self.alignment_probabilities.get(pid, 0.33), -self.credibility.get(pid, 0.6), -pid),
            )
        return {"target": target if target in options else options[0]}

    def speech_plan(self, me: int, alive: list[int]) -> dict[str, Any]:
        options = [pid for pid in alive if pid != me and self.relationships.get(pid) != "狼队"]
        target = max(options, key=lambda pid: (self.alignment_probabilities.get(pid, 0.33), -pid)) if options else None
        # General public points exclude private information. The seer's own
        # results are disclosed through a separate, verified speech prefix.
        points = [point for point in self.evidence.get(target, []) if "私有" not in point]
        return {
            "stance": "suspect" if points else "observe",
            "targets": [target] if target else [],
            "confidence": min(0.75, self.alignment_probabilities.get(target, 0.33)),
            "intent": "ask_evidence",
            "own_checks": self.own_checks,
            "points": points or ["目前公开证据不足，先追问判断依据。"],
        }

    def dump(self):
        return asdict(self)
