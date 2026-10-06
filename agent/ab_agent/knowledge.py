"""知识库加载与版本校验（票据 11，strategy.md §7）。

知识库由 mod 侧导出脚本从游戏运行时资源生成（``tools/export_knowledge.py``
收集到 ``docs/knowledge/``），本模块负责读取、按 id 索引、版本整备检查：

- 每个数据集文件含 ``schema_version`` / ``game_version`` / ``data_version`` / ``entries``；
  ``data_version`` 是 mod 侧对规范 JSON（排序 + 紧凑输出，不含 data_version/mod_version）
  计算的 SHA-256，本模块按同一规范重算校验（不一致进入 ``warnings``，提示文件被改动
  或非本工具导出）；
- ``game_version`` 与锁定版本（ADR-0005）不匹配时进入 ``warnings``（不抛异常，
  由调用方决定告警展示），供决策引擎启动时提示重新导出；
- ``tier_list.json`` 为人工标注，可缺失（缺失时告警并退化为无 Tier）；
  其 ``game_version`` 与数据集不一致时同样告警。

经济层（票据 12）通过本模块读取；参数外置原则（ADR-0008）不适用于知识库
（数据随游戏版本重新导出，而非手工调参）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .protocol import GAME_VERSION

#: 仓库内知识库默认目录（``docs/knowledge``）
DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "docs" / "knowledge"

#: 四类自动数据集（文件名 = 数据集名 + .json）
DATASETS = ("items", "weapons", "upgrades", "characters")
FILE_NAMES = {name: name + ".json" for name in DATASETS}

#: 人工 Tier 标注文件与允许的评级
TIER_LIST_FILE = "tier_list.json"
TIER_GRADES = ("S", "A", "B", "C", "D")

_WRAPPER_KEYS = ("schema_version", "game_version", "data_version", "mod_version")


class KnowledgeError(ValueError):
    """知识库文件缺失/损坏/结构非法。"""


@dataclass(frozen=True)
class Dataset:
    """一个数据集：版本信息 + id → 条目索引 + 数据集级附加内容（如武器套装）。"""

    game_version: str
    schema_version: int
    data_version: str
    entries: Mapping[str, dict]
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeBase:
    """加载完成的知识库快照。"""

    directory: Path
    game_version: str
    schema_version: int
    datasets: Mapping[str, Dataset]
    tier_ratings: Mapping[str, dict]
    tier_updated_at: str
    warnings: tuple[str, ...]

    @property
    def items(self) -> Mapping[str, dict]:
        return self.datasets["items"].entries

    @property
    def weapons(self) -> Mapping[str, dict]:
        return self.datasets["weapons"].entries

    @property
    def upgrades(self) -> Mapping[str, dict]:
        return self.datasets["upgrades"].entries

    @property
    def characters(self) -> Mapping[str, dict]:
        return self.datasets["characters"].entries

    @property
    def sets(self) -> Mapping[str, Any]:
        """武器套装（weapons.json 顶层 sets 数组，按 id 索引）。"""
        return self.datasets["weapons"].extra.get("sets", {})

    @property
    def data_versions(self) -> dict[str, str]:
        return {name: dataset.data_version for name, dataset in self.datasets.items()}

    def tier_rating(self, entry: Mapping[str, Any]) -> Optional[dict]:
        """条目的人工 Tier 标注：先按精确 id，再按武器族 ``weapon_id``。"""
        for key in (entry.get("id"), entry.get("weapon_id")):
            if isinstance(key, str) and key in self.tier_ratings:
                return self.tier_ratings[key]
        return None


def load_knowledge(
    directory: Optional[Path] = None, *, expected_game_version: Optional[str] = GAME_VERSION
) -> KnowledgeBase:
    """加载 ``docs/knowledge``（或指定目录）并返回 :class:`KnowledgeBase`。

    文件缺失/JSON 非法/结构非法抛 :class:`KnowledgeError`；版本不匹配等可容忍
    问题写入 ``warnings``（中文），由调用方决定展示方式。
    """
    base = Path(directory) if directory is not None else DEFAULT_KNOWLEDGE_DIR
    warnings: list[str] = []
    datasets: dict[str, Dataset] = {}
    schema_versions: set[int] = set()
    game_versions: set[str] = set()
    for name in DATASETS:
        path = base / FILE_NAMES[name]
        raw = _read_json(path)
        schema_version = _required_int(raw, "schema_version", path)
        game_version = _required_str(raw, "game_version", path)
        data_version = _required_str(raw, "data_version", path)
        entries = _index_entries(path, raw.get("entries"))
        if _content_hash(raw) != data_version:
            warnings.append(
                "%s 的 data_version 校验失败（内容与哈希不一致：文件被改动或非本工具导出）"
                % FILE_NAMES[name]
            )
        extra = {key: value for key, value in raw.items() if key not in _WRAPPER_KEYS and key != "entries"}
        if "sets" in extra and isinstance(extra["sets"], list):
            extra["sets"] = {
                item["id"]: item
                for item in extra["sets"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        datasets[name] = Dataset(
            game_version=game_version,
            schema_version=schema_version,
            data_version=data_version,
            entries=entries,
            extra=extra,
        )
        schema_versions.add(schema_version)
        game_versions.add(game_version)

    if len(game_versions) > 1:
        warnings.append("知识库各文件 game_version 不一致：%s" % "、".join(sorted(game_versions)))
    if len(schema_versions) > 1:
        warnings.append(
            "知识库各文件 schema_version 不一致：%s" % "、".join(str(v) for v in sorted(schema_versions))
        )
    game_version = sorted(game_versions)[0] if game_versions else ""
    schema_version = max(schema_versions) if schema_versions else 0
    if expected_game_version and game_version != expected_game_version:
        warnings.append(
            "知识库游戏版本 %s 与锁定版本 %s 不匹配（需用当前游戏重新导出）"
            % (game_version, expected_game_version)
        )

    tier_ratings, tier_updated_at = _load_tier_list(base, datasets, warnings)
    return KnowledgeBase(
        directory=base,
        game_version=game_version,
        schema_version=schema_version,
        datasets=datasets,
        tier_ratings=tier_ratings,
        tier_updated_at=tier_updated_at,
        warnings=tuple(warnings),
    )


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise KnowledgeError("知识库文件不存在：%s（请运行 tools/export_knowledge.py）" % path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeError("知识库文件无法解析：%s（%s）" % (path, exc)) from exc
    if not isinstance(raw, dict):
        raise KnowledgeError("知识库文件必须是 JSON 对象：%s" % path)
    return raw


def _content_hash(raw: Mapping[str, Any]) -> str:
    """按 mod 侧规范重算内容哈希（strategy.md §7）：主体 dict 排序后紧凑序列化。

    主体 = schema_version / game_version / entries + 数据集级附加内容（如 weapons 的 sets）；
    ``ensure_ascii=False`` 与 Godot ``JSON.print`` 的原文输出对齐（中文不转义）。
    """
    body: dict[str, Any] = {key: raw[key] for key in ("schema_version", "game_version", "entries")}
    for key, value in raw.items():
        if key not in _WRAPPER_KEYS and key != "entries":
            body[key] = value
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _required_str(raw: Mapping[str, Any], key: str, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise KnowledgeError("%s 缺少非空字符串字段 %s" % (path, key))
    return value


def _required_int(raw: Mapping[str, Any], key: str, path: Path) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise KnowledgeError("%s 缺少整数字段 %s" % (path, key))
    return value


def _index_entries(path: Path, raw_entries: Any) -> dict[str, dict]:
    if not isinstance(raw_entries, list):
        raise KnowledgeError("%s 的 entries 必须是数组" % path)
    entries: dict[str, dict] = {}
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise KnowledgeError("%s 的条目必须是对象" % path)
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise KnowledgeError("%s 存在缺少 id 的条目" % path)
        if entry_id in entries:
            raise KnowledgeError("%s 存在重复 id：%s" % (path, entry_id))
        entries[entry_id] = entry
    return entries


def _load_tier_list(
    base: Path, datasets: Mapping[str, Dataset], warnings: list[str]
) -> tuple[dict[str, dict], str]:
    path = base / TIER_LIST_FILE
    if not path.is_file():
        warnings.append("未找到 %s（人工 Tier 标注缺失，评分将退化为无 Tier）" % TIER_LIST_FILE)
        return {}, ""
    raw = _read_json(path)
    ratings = raw.get("ratings")
    if not isinstance(ratings, dict):
        raise KnowledgeError("%s 缺少 ratings 对象" % path)
    tier_game_version = raw.get("game_version")
    dataset_game_version = next(iter(datasets.values())).game_version
    if isinstance(tier_game_version, str) and tier_game_version != dataset_game_version:
        warnings.append(
            "%s 的游戏版本 %s 与数据集 %s 不一致（标注可能已过期）"
            % (TIER_LIST_FILE, tier_game_version, dataset_game_version)
        )
    known_ids = set(datasets["items"].entries) | set(datasets["weapons"].entries)
    known_ids |= {
        entry.get("weapon_id")
        for entry in datasets["weapons"].entries.values()
        if isinstance(entry.get("weapon_id"), str)
    }
    valid: dict[str, dict] = {}
    for entry_id, rating in ratings.items():
        if not isinstance(rating, dict):
            warnings.append("%s 的 %s 标注不是对象，已忽略" % (TIER_LIST_FILE, entry_id))
            continue
        grade = rating.get("tier")
        if grade not in TIER_GRADES:
            warnings.append(
                "%s 的 %s tier=%r 非法（允许 %s），标注已忽略"
                % (TIER_LIST_FILE, entry_id, grade, "/".join(TIER_GRADES))
            )
            continue
        if entry_id not in known_ids:
            warnings.append("%s 的 %s 不在导出条目中（已失效？）" % (TIER_LIST_FILE, entry_id))
        valid[entry_id] = rating
    updated_at = raw.get("updated_at")
    return valid, updated_at if isinstance(updated_at, str) else ""
