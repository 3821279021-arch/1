"""Role catalogue and composable skill hooks used by the authoritative engine.

A role defines visibility, targets, effects and death triggers here. The state
machine only schedules hooks; adding a night role does not add engine branches.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .game import Player, WerewolfGame
    from .rules import RuleEngine

TargetHook = Callable[["WerewolfGame", "Player"], list[int]]
ActionHook = Callable[["RuleEngine", "Player", dict[str, Any]], None]


def other_alive(g, p):
    return [q.id for q in g.alive_players() if q.id != p.id]


def good_targets(g, p):
    # Ordinary wolves do not know an unawakened hidden wolf exists at a seat.
    return [
        q.id
        for q in g.alive_players()
        if q.id != p.id
        and (ROLE_DEFINITIONS[q.role].faction != "wolves" or (q.role == "hidden_wolf" and not g.in_wolf_channel(q)))
    ]


def guard_targets(g, p):
    return [q.id for q in g.alive_players() if q.id != p.role_state.get("last_guard_target")]


def charm_targets(g, p):
    return [pid for pid in good_targets(g, p) if pid != p.role_state.get("last_charm_target")]


def wolf_action(e, p, payload):
    target = payload.get("target")
    e.g.night_choices[str(p.id)] = target
    p.private_notes.append(f"第{e.g.day}夜，你的夜杀选择为 {target or '弃权'}；这不是查验，不确认目标身份。")


def seer_action(e, p, payload):
    target = payload.get("target")
    if target is not None:
        alignment = "狼人" if ROLE_DEFINITIONS[e.g.player(target).role].inspection == "wolf" else "好人"
        p.private_notes.append(f"第{e.g.day}夜查验：{target}号是{alignment}。")


def witch_action(e, p, payload):
    if payload.get("save"):
        target = e.g.night_kill
        e.g.night_saved.append(target)
        e.g.night_kill = None
        p.role_state["antidote"] = False
        e.g.witch_antidote = False
        p.private_notes.append(f"第{e.g.day}夜你救了 {target} 号。")
    if payload.get("poison_target") is not None:
        target = payload["poison_target"]
        e.g.night_poison = target
        e.g.night_poisons.append(target)
        p.role_state["poison"] = False
        e.g.witch_poison = False
        p.private_notes.append(f"第{e.g.day}夜你毒了 {target} 号。")


def guard_action(e, p, payload):
    target = payload.get("target")
    e.g.night_guards[str(p.id)] = target
    p.role_state["last_guard_target"] = target
    p.private_notes.append(f"第{e.g.day}夜守护：{target or '不守护'}。")


def charm_action(e, p, payload):
    target = payload.get("target")
    p.role_state["charmed_target"] = target
    p.role_state["last_charm_target"] = target
    p.private_notes.append(f"第{e.g.day}夜魅惑：{target or '不魅惑'}。魅惑不确认目标身份。")


def shoot_action(e, p, payload):
    p.role_state["shot_used"] = True
    target = payload.get("target")
    e.record(
        "skill",
        f"{p.id}号发动{ROLE_DEFINITIONS[p.role].display_name}技能："
        + (f"带走 {target} 号。" if target is not None else "不开枪。"),
        p.id,
        action=payload["action"],
        target=target,
    )
    if target is not None:
        e.kill_player(target, cause="shot")
        e.record("death", f"{target}号被技能带走。", target)


def shoot_on_death(e, p, cause):
    if cause not in {"poison", "charm", "dream"} and not p.role_state.get("shot_used"):
        e.g.death_skill_queue.append(
            {"player_id": p.id, "action": "hunter_shoot" if p.role == "hunter" else "wolf_king_shoot"}
        )


def charm_on_death(e, p, cause):
    target = p.role_state.get("charmed_target")
    if target is not None and e.g.player(target).alive:
        e.kill_player(target, cause="charm")
        e.record("death", f"{target}号因狼美人魅惑殉情。", target)


def no_action(e, p, payload):
    return None


def dead_targets(g, p):
    inspected = p.role_state.get("grave_inspected", [])
    return [q.id for q in g.players if not q.alive and q.id not in inspected]


def grave_action(e, p, payload):
    target = payload.get("target")
    if target is not None:
        faction = "狼人" if ROLE_DEFINITIONS[e.g.player(target).role].faction == "wolves" else "好人"
        p.role_state.setdefault("grave_inspected", []).append(target)
        p.private_notes.append(f"第{e.g.day}夜守墓查验：已出局的{target}号属于{faction}阵营。")


def dream_action(e, p, payload):
    target = payload.get("target")
    p.role_state["dream_repeat"] = target is not None and target == p.role_state.get("last_dream_target")
    p.role_state.update(dream_target=target, last_dream_target=target, dream_day=e.g.day)
    p.private_notes.append(f"第{e.g.day}夜梦游目标：{target or '跳过'}。梦游不确认身份。")


def dream_on_death(e, p, cause):
    night = e.g.phase.startswith("night_") or (
        e.g.phase == "death_skill" and e.g.death_context.get("after") == "day_speech"
    )
    target = p.role_state.get("dream_target")
    if night and p.role_state.get("dream_day") == e.g.day and target is not None and e.g.player(target).alive:
        e.kill_player(target, cause="dream")


def crow_targets(g, p):
    return [pid for pid in other_alive(g, p) if pid != p.role_state.get("last_crow_target")]


def crow_action(e, p, payload):
    target = payload.get("target")
    p.role_state.update(crow_target=target, last_crow_target=target, crow_day=e.g.day)
    p.private_notes.append(f"第{e.g.day}夜标记：{target or '跳过'}。标记者不获知目标身份。")


def dawn_effects(e, *, final=False):
    """Only the four new roles use these hooks; old rosters are unchanged."""
    g = e.g
    if not final:
        for p in g.players:
            if p.role == "dreamer" and p.role_state.get("dream_day") == g.day and p.role_state.get("dream_repeat"):
                target = p.role_state.get("dream_target")
                if target is not None:
                    e.kill_player(target, cause="dream")
        return
    alive = sorted(g.alive_players(), key=lambda p: p.id)
    for p in alive:
        if p.role == "bear_trainer" and p.role_state.get("bear_day") != g.day:
            index = alive.index(p)
            neighbors = {alive[(index - 1) % len(alive)].id, alive[(index + 1) % len(alive)].id} - {p.id}
            growl = any(ROLE_DEFINITIONS[g.player(pid).role].faction == "wolves" for pid in neighbors)
            p.role_state.update(bear_day=g.day, bear_growled=growl)
            p.private_notes.append(f"第{g.day}天熊{'咆哮' if growl else '没有咆哮'}；相邻指最近的两个存活座位。")
            e.record("skill", "天亮时熊咆哮了。" if growl else "天亮时熊没有咆哮。", bear_growled=growl)


@dataclass(frozen=True)
class RoleDefinition:
    key: str
    display_name: str
    faction: str
    rules: str
    night_order: int | None = None
    phase: str | None = None
    action: str | None = None
    action_schema: dict[str, Any] = field(default_factory=lambda: {"target": "integer|null"})
    legal_targets: TargetHook = other_alive
    apply_action: ActionHook = no_action
    death_trigger: Callable[..., None] | None = None
    private_information: Callable[..., dict[str, Any]] = lambda g, p: deepcopy(p.role_state)
    win_condition_modifier: Callable[..., bool] | None = None
    inspection: str = "good"
    wolf_channel: bool = False
    participates_in_kill: bool = False

    def public(self):
        return {
            "key": self.key,
            "display_name": self.display_name,
            "faction": self.faction,
            "rules": self.rules,
            "night_order": self.night_order,
            "action_schema": dict(self.action_schema),
            "tags": list(ROLE_TAGS.get(self.key, ())),
        }


def wolf(key, name, rules, **extra):
    return RoleDefinition(
        key,
        name,
        "wolves",
        rules,
        night_order=20,
        phase="night_wolves",
        action="wolf_kill",
        legal_targets=good_targets,
        apply_action=wolf_action,
        inspection="wolf",
        wolf_channel=True,
        participates_in_kill=True,
        **extra,
    )


ROLE_DEFINITIONS = {
    "villager": RoleDefinition("villager", "村民", "good", "无夜间技能，公开发言并参与放逐投票。"),
    "wolf": wolf("wolf", "狼人", "可见普通狼队，夜间共同选择刀人；严格多数同一目标才会击杀，平票或弃权不刀。"),
    "seer": RoleDefinition(
        "seer",
        "预言家",
        "good",
        "每夜查验一名其他存活玩家的阵营；隐狼查验显示好人，可选择跳过。",
        40,
        "night_seer",
        "seer_inspect",
        apply_action=seer_action,
    ),
    "witch": RoleDefinition(
        "witch",
        "女巫",
        "good",
        "各有一瓶解药和毒药；有解药时获知刀口，可自救；同夜只能使用一种药，毒杀无视守护且被毒者不能开枪。",
        50,
        "night_witch",
        "witch",
        {"save": "boolean", "poison_target": "integer|null"},
        apply_action=witch_action,
    ),
    "hunter": RoleDefinition(
        "hunter",
        "猎人",
        "good",
        "被刀、放逐、决斗或枪击死亡可开枪带走一名存活玩家；被毒或殉情不能开枪。",
        death_trigger=shoot_on_death,
    ),
    "guard": RoleDefinition(
        "guard",
        "守卫",
        "good",
        "每夜守护一人（可自己），不能连续两夜守同一目标；阻挡狼刀，不阻挡毒药和技能。守护与解药同时作用仍存活。",
        10,
        "night_guard",
        "guard_protect",
        legal_targets=guard_targets,
        apply_action=guard_action,
    ),
    "knight": RoleDefinition(
        "knight", "骑士", "good", "白天发言阶段可发动一次决斗：目标为狼阵营则目标死亡，否则骑士死亡。"
    ),
    "idiot": RoleDefinition(
        "idiot", "白痴", "good", "第一次被放逐翻牌免死，从此失去投票权，仍可发言；被狼刀、毒药和其他技能击杀正常死亡。"
    ),
    "wolf_king": wolf(
        "wolf_king", "狼王", "普通狼队技能；死亡时可开枪带走一人，被毒或殉情时不能开枪。", death_trigger=shoot_on_death
    ),
    "white_wolf_king": wolf(
        "white_wolf_king", "白狼王", "普通狼队技能；白天发言阶段可一次自爆，与一名存活玩家同时死亡并立即结束白天。"
    ),
    "wolf_beauty": wolf(
        "wolf_beauty",
        "狼美人",
        "普通狼队技能；每夜额外魅惑一人，不能连续魅惑同一人；自己死亡时最近魅惑的存活目标殉情，殉情不触发开枪。",
        death_trigger=charm_on_death,
    ),
    "hidden_wolf": RoleDefinition(
        "hidden_wolf",
        "隐狼",
        "wolves",
        "属于狼阵营，查验显示好人；知道普通狼队但普通狼不知道隐狼，不能使用狼频道或刀人。普通刀狼全部死亡后觉醒，获得狼频道与刀人。",
    ),
    "dreamer": RoleDefinition(
        "dreamer",
        "摄梦人",
        "good",
        "每夜选择一名其他存活者或跳过。当晚梦游目标免疫狼刀与毒药；连续两夜梦游同一人，该人梦死。摄梦人夜间死亡，当晚目标同时梦死；梦死不能开枪。白天死亡不牵连。",
        5,
        "night_dreamer",
        "dream_visit",
        apply_action=dream_action,
        death_trigger=dream_on_death,
    ),
    "gravekeeper": RoleDefinition(
        "gravekeeper",
        "守墓人",
        "good",
        "每夜私下查验一名已出局玩家的真实阵营（含隐狼），每人只能查验一次，可跳过；没有未查验的死者时不行动。信息仅本人可见。",
        65,
        "night_grave",
        "grave_inspect",
        legal_targets=dead_targets,
        apply_action=grave_action,
    ),
    "crow": RoleDefinition(
        "crow",
        "乌鸦",
        "good",
        "每夜标记一名其他存活者或跳过，不可连续标记同一人。当日放逐结算额外增加该目标一票；标记提交后即使乌鸦死亡仍有效，目标已死则不计票。标记不确认身份。",
        60,
        "night_crow",
        "crow_mark",
        legal_targets=crow_targets,
        apply_action=crow_action,
    ),
    "bear_trainer": RoleDefinition(
        "bear_trainer",
        "驯熊师",
        "good",
        "无主动夜间动作。夜间死亡全部结算后，存活驯熊师最近的左右两个存活座位若含狼阵营（含隐狼），熊公开咆哮；不公开驯熊师座位或哪一侧是狼。技能与死亡结算完成后才判胜。",
    ),
}

ROLE_TAGS = {
    "villager": ("基础",),
    "wolf": ("基础狼",),
    "seer": ("信息型",),
    "witch": ("保护型", "资源型", "击杀型"),
    "guard": ("保护型",),
    "hunter": ("击杀型", "死亡触发型"),
    "knight": ("击杀型", "控制型"),
    "idiot": ("控制型",),
    "wolf_king": ("强狼", "死亡触发型"),
    "white_wolf_king": ("强狼", "击杀型"),
    "wolf_beauty": ("强狼", "死亡触发型"),
    "hidden_wolf": ("隐蔽狼",),
    "dreamer": ("保护型", "死亡触发型"),
    "gravekeeper": ("信息型",),
    "crow": ("控制型",),
    "bear_trainer": ("信息型",),
}

# Extra night actions belong to a role without replacing its faction action.
NIGHT_SKILLS = {
    "night_guard": ("guard", "guard_protect", guard_targets, guard_action),
    "night_beauty": ("wolf_beauty", "wolf_beauty_charm", charm_targets, charm_action),
    "night_seer": ("seer", "seer_inspect", other_alive, seer_action),
    "night_witch": ("witch", "witch", other_alive, witch_action),
    "night_dreamer": ("dreamer", "dream_visit", other_alive, dream_action),
    "night_grave": ("gravekeeper", "grave_inspect", dead_targets, grave_action),
    "night_crow": ("crow", "crow_mark", crow_targets, crow_action),
}


@dataclass(frozen=True)
class GameMode:
    key: str
    display_name: str
    player_count: int
    roles: tuple[str, ...]
    phases: tuple[str, ...] = (
        "night_discussion",
        "night_guard",
        "night_wolves",
        "night_beauty",
        "night_seer",
        "night_witch",
        "day_speech",
        "day_vote",
    )
    speaking_rules: str = "存活玩家按座位依次发言，提交结束立即下一位。"
    voting_rules: str = "不能投自己，可弃票；所有有投票权玩家提交立即结算，最高票平票无人出局。"
    special_rules: str = "狼阵营全部死亡好人胜；狼人数达到或超过好人人数狼人胜。"

    def public(self):
        return {
            "key": self.key,
            "display_name": self.display_name,
            "player_count": self.player_count,
            "roles": list(self.roles),
            "phases": list(
                dict.fromkeys(
                    (*self.phases, *(ROLE_DEFINITIONS[r].phase for r in self.roles if ROLE_DEFINITIONS[r].phase))
                )
            ),
            "speaking_rules": self.speaking_rules,
            "voting_rules": self.voting_rules,
            "special_rules": self.special_rules,
        }


GAME_MODES = {
    "quick6": GameMode("quick6", "6 人极速", 6, ("wolf", "wolf", "seer", "witch", "villager", "villager")),
    "standard9": GameMode("standard9", "9 人标准", 9, ("wolf",) * 3 + ("seer", "witch", "hunter") + ("villager",) * 3),
    "standard12": GameMode(
        "standard12", "12 人标准", 12, ("wolf",) * 4 + ("seer", "witch", "hunter", "guard") + ("villager",) * 4
    ),
    "wolfking12": GameMode(
        "wolfking12",
        "12 人狼王板",
        12,
        ("wolf_king",) + ("wolf",) * 3 + ("seer", "witch", "hunter", "guard") + ("villager",) * 4,
    ),
    "beautyknight12": GameMode(
        "beautyknight12",
        "12 人狼美人骑士板",
        12,
        ("wolf_beauty",) + ("wolf",) * 3 + ("seer", "witch", "knight", "guard") + ("villager",) * 4,
    ),
    "hidden12": GameMode(
        "hidden12",
        "12 人隐狼板",
        12,
        ("hidden_wolf",) + ("wolf",) * 3 + ("seer", "witch", "hunter", "guard") + ("villager",) * 4,
    ),
    "advanced9": GameMode(
        "advanced9", "9 人进阶板", 9, ("wolf_king", "wolf", "wolf", "seer", "witch", "knight") + ("villager",) * 3
    ),
    "fun6": GameMode("fun6", "6 人娱乐板", 6, ("wolf", "wolf", "seer", "hunter", "villager", "villager")),
    "special12": GameMode(
        "special12",
        "12 人梦熊鸦墓板",
        12,
        ("wolf",) * 4 + ("dreamer", "bear_trainer", "crow", "gravekeeper") + ("villager",) * 4,
    ),
}
MODE_ALIASES = {
    "6": "quick6",
    "9": "standard9",
    "12": "standard12",
    "quick": "quick6",
    "standard_9": "standard9",
    "standard_12": "standard12",
}


def validate_mode(mode="quick6", player_count=None, roles=None):
    mode = MODE_ALIASES.get(str(mode), str(mode))
    if player_count is not None and type(player_count) is not int:
        raise ValueError("人数须为整数")
    if roles is not None and not isinstance(roles, (list, tuple)):
        raise ValueError("角色组合须为数组")
    if mode in GAME_MODES and roles is not None and list(roles) == list(GAME_MODES[mode].roles):
        definition = GAME_MODES[mode]
        if player_count is not None and player_count != definition.player_count:
            raise ValueError("人数与模式不一致")
        return mode, definition.player_count, list(definition.roles)
    if roles is not None and (mode == "custom" or roles):
        if not isinstance(roles, (list, tuple)) or not all(
            isinstance(role, str) and role in ROLE_DEFINITIONS for role in roles
        ):
            raise ValueError("自定义角色组合包含未知身份")
        count = len(roles) if player_count is None else player_count
        if type(count) is not int or not 4 <= count <= 16 or len(roles) != count:
            raise ValueError("自定义板子人数须为 4 到 16 人，角色数量须等于人数")
        wolves = sum(ROLE_DEFINITIONS[role].faction == "wolves" for role in roles)
        if not 0 < wolves < count - wolves:
            raise ValueError("板子须同时包含狼人与好人，且狼人少于好人")
        if all(role == "hidden_wolf" for role in roles if ROLE_DEFINITIONS[role].faction == "wolves"):
            raise ValueError("隐狼板子须至少包含一名普通刀狼")
        return "custom", count, list(roles)
    if mode not in GAME_MODES:
        raise ValueError("未知游戏模式")
    definition = GAME_MODES[mode]
    if player_count is not None and player_count != definition.player_count:
        raise ValueError("人数与模式不一致")
    return mode, definition.player_count, list(definition.roles)
