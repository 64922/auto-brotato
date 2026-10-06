"""测试用协议载荷构造（字段结构对齐 docs/protocol.md §3/§7，取自票据 04–06 实机样本）。"""
from __future__ import annotations

from typing import Optional


def hello(**overrides) -> dict:
    payload = {
        "protocol_version": 2,
        "mod_version": "0.2.0",
        "game_version": "1.1.15.4",
        "session_id": "test-session",
        "capabilities": {"snapshot_hz": 60, "move_analog": True},
    }
    payload.update(overrides)
    return payload


def difficulty_options(unlocked=(0, 1), total=7) -> list:
    options = []
    for value in range(total):
        options.append(
            {
                "my_id": "difficulty_%d" % value,
                "name": "DANGER_%d" % value,
                "tier": value,
                "value": value,
                "is_locked": value not in unlocked,
                "unlocked_by_default": value == 0,
                "current_number": 1 if value == 0 else 0,
            }
        )
    return options


def difficulty(
    *,
    hero: str = "character_ranger",
    unlocked=(0, 1),
    selected: Optional[str] = "difficulty_0",
    modes: Optional[dict] = None,
    total: int = 7,
    can_start: bool = True,
) -> dict:
    options = difficulty_options(unlocked=unlocked, total=total)
    return {
        "phase": "difficulty_select",
        "character": {"id": hero},
        "weapons": [
            {"my_id": "weapon_pistol_1", "weapon_id": "weapon_pistol", "tier": 1}
        ],
        "difficulty": {
            "options": options,
            "selected": selected,
            "displayed": [option["my_id"] for option in options],
        },
        "modes": modes
        if modes is not None
        else {"endless": False, "ban": False, "coop": False},
        "can_start": can_start,
    }


def snapshot(
    *,
    wave_index: Optional[int] = 1,
    phase: str = "combat",
    player: bool = True,
    gold: int = 25,
    materials: int = 3,
    weapons: Optional[list] = None,
    items: Optional[list] = None,
    arena: bool = True,
    alive: bool = True,
) -> dict:
    return {
        "t": 1.0,
        "wave": (
            {"index": wave_index, "phase": phase, "time_left": 17.3, "time_total": 20}
            if wave_index is not None
            else None
        ),
        "player": (
            {
                "pos": [120.0, 140.0],
                "vel": [0.0, 0.0],
                "hp": 10.0,
                "max_hp": 10.0,
                "alive": alive,
            }
            if player
            else None
        ),
        "weapons": [],
        "enemies": [],
        "projectiles": [],
        "pickups": [],
        "hazards": [],
        "arena": {"min": [0.0, 0.0], "max": [1000.0, 800.0]} if arena else None,
        "inventory": {
            "weapons": weapons if weapons is not None else [{"slot": 1, "id": "weapon_pistol", "tier": 1}],
            "items": items if items is not None else [{"id": "item_helmet", "count": 2}],
        },
        "economy": {"gold": gold, "materials_this_wave": materials},
    }


def shop(*, wave_next: int = 2, gold: int = 30, can_leave: bool = True) -> dict:
    return {
        "wave_next": wave_next,
        "gold": gold,
        "slots": [
            {"slot": 1, "kind": "item", "id": "item_helmet", "tier": 0, "price": 12, "sold": False, "locked": False}
        ],
        "inventory": {"weapons": [], "items": []},
        "stats": {},
        "reroll": {"cost": 5, "count": 1},
        "can_leave": can_leave,
    }


def level_up(*, wave: int = 1, picks=(True, True, True, False)) -> dict:
    ids = [
        "upgrade_hp_regeneration_1",
        "upgrade_elemental_damage_1",
        "upgrade_engineering_1",
        "upgrade_ranged_damage_1",
    ]
    return {
        "phase": "level_up",
        "player": 0,
        "wave": wave,
        "options": [
            {"slot": index + 1, "kind": "upgrade", "id": ids[index], "tier": 0, "can_pick": picks[index]}
            for index in range(4)
        ],
    }


def run_end(*, result: str = "defeat", wave: int = 6, title: str = "战败 - 碰撞区域") -> dict:
    return {
        "phase": "run_end",
        "result": result,
        "wave": wave,
        "title": title,
        "stats": {
            "CURRENT_LEVEL": "12",
            "STAT_MAX_HP": "40",
            "STAT_RANGED_DAMAGE": "12",
        },
    }
