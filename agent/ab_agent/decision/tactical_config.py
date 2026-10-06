"""战术层参数配置：JSON 加载与结构校验（strategy.md §4/§8，票据 10）。

所有数值权重/阈值集中在 ``config/tactical.json``；本模块只负责读取与校验，不做决策。
运行期可用 CLI ``--tactical-config`` 指向替代文件，对同一回放调参对比。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .config_util import (
    DecisionConfigError,
    check_keys as _check_keys,
    integer as _integer,
    number as _number,
    numbers as _numbers,
    section as _section,
)

#: 默认参数文件（随包发布；所有键必填，缺失即报错）
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "tactical.json"

#: 配置分区名与各分区必填键（用于报错定位；多/少键均报错）
_SECTIONS = ("update", "cluster", "conservative", "consumable", "boss", "rhythm")
_SECTION_KEYS = {
    "update": ("interval_s",),
    "cluster": ("join_radius", "max_distance", "distance_decay", "danger_weight"),
    "conservative": (
        "enter_hp_ratio",
        "exit_hp_ratio",
        "danger_scale",
        "attraction_scale",
        "max_pickup_distance",
    ),
    "consumable": ("hp_ratio", "max_distance", "attraction_scale"),
    "boss": (
        "band",
        "candidates",
        "edge_margin",
        "edge_weight",
        "attraction_scale",
        "danger_scale",
    ),
    "rhythm": (
        "early_wave_max",
        "late_wave_min",
        "early_danger_scale",
        "early_attraction_scale",
        "mid_danger_scale",
        "mid_attraction_scale",
        "late_danger_scale",
        "late_attraction_scale",
        "clear_enemy_threshold",
        "clear_danger_scale",
        "clear_attraction_scale",
    ),
}


class TacticalConfigError(DecisionConfigError):
    """战术层配置缺失/类型非法/数值越界（中文信息，带字段路径）。"""


@dataclass(frozen=True)
class UpdateConfig:
    """战术层重算节拍（3–5Hz）。"""

    interval_s: float


@dataclass(frozen=True)
class ClusterConfig:
    """材料簇：聚合半径、距离上限、距离衰减与危险惩罚。"""

    join_radius: float
    max_distance: float
    distance_decay: float
    danger_weight: float


@dataclass(frozen=True)
class ConservativeConfig:
    """低血保守模式（迟滞阈值 + 权重缩放 + 放弃远端材料）。"""

    enter_hp_ratio: float
    exit_hp_ratio: float
    danger_scale: float
    attraction_scale: float
    max_pickup_distance: float


@dataclass(frozen=True)
class ConsumableConfig:
    """回血消耗品目标（低血优先）。"""

    hp_ratio: float
    max_distance: float
    attraction_scale: float


@dataclass(frozen=True)
class BossConfig:
    """Boss 应对：风筝距离带、开阔方向候选与权重。"""

    band: tuple[float, float]
    candidates: int
    edge_margin: float
    edge_weight: float
    attraction_scale: float
    danger_scale: float


@dataclass(frozen=True)
class RhythmConfig:
    """波次节奏：早期采集 / 中后期生存 / 末段清场拾取。"""

    early_wave_max: int
    late_wave_min: int
    early_danger_scale: float
    early_attraction_scale: float
    mid_danger_scale: float
    mid_attraction_scale: float
    late_danger_scale: float
    late_attraction_scale: float
    clear_enemy_threshold: int
    clear_danger_scale: float
    clear_attraction_scale: float


@dataclass(frozen=True)
class TacticalConfig:
    """战术层完整参数集。"""

    update: UpdateConfig
    cluster: ClusterConfig
    conservative: ConservativeConfig
    consumable: ConsumableConfig
    boss: BossConfig
    rhythm: RhythmConfig


def load_tactical_config(path: str | Path | None = None) -> TacticalConfig:
    """读取并校验配置文件；``path=None`` 用包内默认 ``config/tactical.json``。"""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TacticalConfigError("无法读取配置文件 %s：%s" % (config_path, exc)) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TacticalConfigError("配置文件 %s 非法 JSON：%s" % (config_path, exc)) from exc
    if not isinstance(raw, dict):
        raise TacticalConfigError("配置文件 %s 顶层必须是 JSON 对象" % config_path)
    return tactical_config_from_mapping(raw)


def tactical_config_from_mapping(raw: Mapping[str, Any]) -> TacticalConfig:
    """由 mapping 构造 :class:`TacticalConfig`；键缺失/类型或数值非法抛错。"""
    try:
        return _build_tactical_config(raw)
    except DecisionConfigError as exc:
        raise TacticalConfigError(str(exc)) from exc


def _build_tactical_config(raw: Mapping[str, Any]) -> TacticalConfig:
    if not isinstance(raw, Mapping):
        raise TacticalConfigError("配置必须是对象")
    _check_keys(raw, _SECTIONS, "顶层")
    for name in _SECTIONS:
        _check_keys(_section(raw, name), _SECTION_KEYS[name], name)
    update_raw = _section(raw, "update")
    cluster_raw = _section(raw, "cluster")
    conservative_raw = _section(raw, "conservative")
    consumable_raw = _section(raw, "consumable")
    boss_raw = _section(raw, "boss")
    rhythm_raw = _section(raw, "rhythm")

    update = UpdateConfig(
        interval_s=_number(update_raw, "interval_s", "update", minimum=0.05, maximum=1.0),
    )

    cluster = ClusterConfig(
        join_radius=_number(cluster_raw, "join_radius", "cluster", minimum=0.0),
        max_distance=_number(cluster_raw, "max_distance", "cluster", minimum=0.0),
        distance_decay=_number(
            cluster_raw, "distance_decay", "cluster", minimum=0.001
        ),
        danger_weight=_number(cluster_raw, "danger_weight", "cluster", minimum=0.0),
    )
    if cluster.join_radius <= 0.0:
        raise TacticalConfigError("cluster.join_radius 必须大于 0")

    conservative = ConservativeConfig(
        enter_hp_ratio=_number(
            conservative_raw, "enter_hp_ratio", "conservative", minimum=0.01, maximum=1.0
        ),
        exit_hp_ratio=_number(
            conservative_raw, "exit_hp_ratio", "conservative", minimum=0.01, maximum=1.0
        ),
        danger_scale=_number(
            conservative_raw, "danger_scale", "conservative", minimum=0.0
        ),
        attraction_scale=_number(
            conservative_raw, "attraction_scale", "conservative", minimum=0.0
        ),
        max_pickup_distance=_number(
            conservative_raw, "max_pickup_distance", "conservative", minimum=0.0
        ),
    )
    if conservative.enter_hp_ratio >= conservative.exit_hp_ratio:
        raise TacticalConfigError("conservative.enter_hp_ratio 必须小于 exit_hp_ratio（迟滞）")

    consumable = ConsumableConfig(
        hp_ratio=_number(
            consumable_raw, "hp_ratio", "consumable", minimum=0.01, maximum=1.0
        ),
        max_distance=_number(consumable_raw, "max_distance", "consumable", minimum=0.0),
        attraction_scale=_number(
            consumable_raw, "attraction_scale", "consumable", minimum=0.0
        ),
    )

    band = _numbers(boss_raw, "band", "boss", minimum=0.0)
    if len(band) != 2 or band[0] <= 0.0 or band[0] >= band[1]:
        raise TacticalConfigError("boss.band 必须是两个递增正数 [min, max]")
    boss = BossConfig(
        band=(band[0], band[1]),
        candidates=_integer(boss_raw, "candidates", "boss", minimum=4, maximum=64),
        edge_margin=_number(boss_raw, "edge_margin", "boss", minimum=0.0),
        edge_weight=_number(boss_raw, "edge_weight", "boss", minimum=0.0),
        attraction_scale=_number(boss_raw, "attraction_scale", "boss", minimum=0.0),
        danger_scale=_number(boss_raw, "danger_scale", "boss", minimum=0.0),
    )

    rhythm = RhythmConfig(
        early_wave_max=_integer(
            rhythm_raw, "early_wave_max", "rhythm", minimum=0, maximum=100
        ),
        late_wave_min=_integer(
            rhythm_raw, "late_wave_min", "rhythm", minimum=1, maximum=200
        ),
        early_danger_scale=_number(
            rhythm_raw, "early_danger_scale", "rhythm", minimum=0.0
        ),
        early_attraction_scale=_number(
            rhythm_raw, "early_attraction_scale", "rhythm", minimum=0.0
        ),
        mid_danger_scale=_number(rhythm_raw, "mid_danger_scale", "rhythm", minimum=0.0),
        mid_attraction_scale=_number(
            rhythm_raw, "mid_attraction_scale", "rhythm", minimum=0.0
        ),
        late_danger_scale=_number(rhythm_raw, "late_danger_scale", "rhythm", minimum=0.0),
        late_attraction_scale=_number(
            rhythm_raw, "late_attraction_scale", "rhythm", minimum=0.0
        ),
        clear_enemy_threshold=_integer(
            rhythm_raw, "clear_enemy_threshold", "rhythm", minimum=0, maximum=300
        ),
        clear_danger_scale=_number(
            rhythm_raw, "clear_danger_scale", "rhythm", minimum=0.0
        ),
        clear_attraction_scale=_number(
            rhythm_raw, "clear_attraction_scale", "rhythm", minimum=0.0
        ),
    )
    if rhythm.early_wave_max >= rhythm.late_wave_min:
        raise TacticalConfigError("rhythm.early_wave_max 必须小于 late_wave_min")

    return TacticalConfig(
        update=update,
        cluster=cluster,
        conservative=conservative,
        consumable=consumable,
        boss=boss,
        rhythm=rhythm,
    )
