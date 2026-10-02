"""Small owner-scoped model preferences, never credentials or account balances."""

import re
from copy import deepcopy

from .game import PERSONALITIES


def route_key(value):
    if not isinstance(value, str) or len(value) > 340 or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", value):
        raise ValueError("模型标识格式无效")
    if value != "auto" and ":" not in value:
        raise ValueError("模型标识须包含供应商")
    return value


def validate_preferences(value):
    if not isinstance(value, dict) or set(value) - {"favorites", "lineups"}:
        raise ValueError("只允许保存模型收藏与阵容")
    result = {}
    if "favorites" in value:
        rows = value["favorites"]
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError("最多收藏 200 个模型")
        result["favorites"] = list(dict.fromkeys(route_key(key) for key in rows))
    if "lineups" in value:
        rows = value["lineups"]
        if not isinstance(rows, list) or len(rows) > 30:
            raise ValueError("最多保存 30 个阵容")
        lineups = []
        for row in rows:
            if not isinstance(row, dict) or set(row) - {"id", "name", "player_count", "seats"}:
                raise ValueError("阵容字段格式无效")
            name, count, seats = row.get("name"), row.get("player_count"), row.get("seats")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
                raise ValueError("阵容名称须为 1 到 40 字")
            if type(count) is not int or not 4 <= count <= 16 or not isinstance(seats, list) or len(seats) > count:
                raise ValueError("阵容人数或座位数量无效")
            occupied = set()
            for seat in seats:
                if not isinstance(seat, dict) or set(seat) - {"id", "model_key", "personality"}:
                    raise ValueError("阵容只保存座位、模型与性格")
                if type(seat.get("id")) is not int or not 1 <= seat["id"] <= count or seat["id"] in occupied:
                    raise ValueError("阵容座位无效或重复")
                occupied.add(seat["id"])
                route_key(seat.get("model_key"))
                if seat.get("personality", "detective") not in PERSONALITIES:
                    raise ValueError("未知阵容性格")
            lineup = deepcopy(row)
            lineup["name"] = name.strip()
            if not isinstance(lineup.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", lineup["id"]):
                raise ValueError("阵容标识无效")
            if any(old["id"] == lineup["id"] for old in lineups):
                raise ValueError("阵容标识重复")
            lineups.append(lineup)
        result["lineups"] = lineups
    return result


def call_status(success, reason=""):
    if success:
        return "success"
    reason = str(reason).lower()
    if any(word in reason for word in ("quota", "insufficient", "402", "余额", "额度")):
        return "quota_exhausted"
    if "429" in reason or "rate_limit" in reason:
        return "rate_limited"
    if "401" in reason or "403" in reason or "permission" in reason or "unauthorized" in reason:
        return "no_permission"
    if "404" in reason or "model_not_found" in reason:
        return "not_found"
    return "error"
