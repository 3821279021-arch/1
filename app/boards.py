"""Bounded, tag-constrained casual boards. Arena configurations stay fixed."""

from itertools import combinations, combinations_with_replacement

from .roles import GAME_MODES, ROLE_DEFINITIONS, ROLE_TAGS, validate_mode

BOARD_POLICIES = {"fixed", "constrained_random", "custom_random"}


def framework(count):
    if type(count) is not int or not 4 <= count <= 16:
        raise ValueError("随机板人数须为 4 到 16 人")
    wolves = max(1, count // 3)
    return {
        "wolves": wolves,
        "good": count - wolves,
        "min_information": 1,
        "min_protection": 1,
        "min_villagers": max(1, count // 4),
    }


def candidates(count, pool):
    frame = framework(count)
    if (
        not isinstance(pool, list)
        or not pool
        or any(not isinstance(role, str) or role not in ROLE_DEFINITIONS for role in pool)
    ):
        raise ValueError("随机池包含未知角色或为空")
    pool = list(dict.fromkeys(pool))
    if "villager" not in pool:
        raise ValueError("随机池须保留村民")
    wolves = [role for role in pool if ROLE_DEFINITIONS[role].faction == "wolves"]
    wolf_sets = [
        roster
        for roster in combinations_with_replacement(wolves, frame["wolves"])
        if roster.count("hidden_wolf") <= 1
        and any(ROLE_DEFINITIONS[role].participates_in_kill for role in roster)
        and sum("强狼" in ROLE_TAGS[role] for role in roster) <= 1
    ]
    specials = [role for role in pool if ROLE_DEFINITIONS[role].faction == "good" and role != "villager"]
    slots = min(4, max(2, count // 3), frame["good"] - frame["min_villagers"])
    good_sets = [
        roster
        for size in range(2, slots + 1)
        for roster in combinations(specials, size)
        if any("信息型" in ROLE_TAGS[role] for role in roster)
        and any("保护型" in ROLE_TAGS[role] for role in roster)
        and sum("保护型" in ROLE_TAGS[role] for role in roster) <= 2
        and sum("击杀型" in ROLE_TAGS[role] for role in roster) <= 2
        and sum("死亡触发型" in ROLE_TAGS[role] for role in roster) <= 2
    ]
    if not wolf_sets or not good_sets:
        raise ValueError("此角色池无法满足阵营、信息位、保护位及强度约束，请增加允许的角色")
    return wolf_sets, good_sets


def generate_board(count, pool, rng):
    wolf_sets, good_sets = candidates(count, pool)
    roles = list(rng.choice(wolf_sets)) + list(rng.choice(good_sets))
    roles += ["villager"] * (count - len(roles))
    validate_mode("custom", count, roles)
    return roles


def configuration(mode, count, roles, policy, pool):
    if policy not in BOARD_POLICIES:
        raise ValueError("未知板子策略")
    if policy == "fixed":
        mode, count, roles = validate_mode(mode, count, roles)
        return mode, count, roles, []
    count = count if count is not None else GAME_MODES.get(mode, GAME_MODES["standard12"]).player_count
    pool = list(ROLE_DEFINITIONS) if policy == "constrained_random" else pool
    candidates(count, pool)
    frame = framework(count)
    # This is only a construction placeholder. Actual roles are drawn at start,
    # never returned by lobby/API projections for a random board.
    roster = ["wolf"] * frame["wolves"] + ["seer", "witch"] + ["villager"] * (frame["good"] - 2)
    return "custom", count, roster, list(dict.fromkeys(pool))
