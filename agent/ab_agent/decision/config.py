"""反射层参数配置：JSON 加载与结构校验（strategy.md §8，ADR-0008）。

所有数值权重/阈值集中在 ``config/reflex.json``；本模块只负责读取与校验，不做决策。
运行期可用 CLI ``--reflex-config`` 指向替代文件，对同一回放调参对比。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

#: 默认参数文件（随包发布；所有键必填，缺失即报错）
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "reflex.json"

#: 配置分区名与各分区必填键（用于报错定位；多/少键均报错）
_SECTIONS = (
    "decision",
    "danger",
    "boundary",
    "tactical",
    "kiting",
    "emergency",
    "invuln",
    "player",
)
_SECTION_KEYS = {
    "decision": (
        "interval_s",
        "candidate_count",
        "turn_penalty",
        "smoothing_alpha",
        "min_magnitude",
        "max_magnitude",
    ),
    "danger": (
        "enemy_weight",
        "enemy_buffer",
        "enemy_default_radius",
        "projectile_weight",
        "projectile_buffer",
        "projectile_default_radius",
        "hazard_weight",
        "hazard_buffer",
        "hazard_default_radius",
        "sample_times_s",
        "sample_decay",
        "truncated_scale",
        "consider_radius",
        "max_threats_per_kind",
    ),
    "boundary": ("margin", "weight"),
    "tactical": (
        "expected_weight",
        "center_weight",
        "pickup_weight",
        "consumable_weight",
        "pickup_max_distance",
        "attraction_range",
    ),
    "kiting": ("default_band", "band_weight", "far_weight"),
    "emergency": ("hp_threshold", "danger_scale", "attraction_scale"),
    "invuln": ("enabled", "crossing_scale", "trapped_distance"),
    "player": ("radius", "fallback_speed"),
}


class ReflexConfigError(ValueError):
    """配置缺失/类型非法/数值越界（中文信息，带字段路径）。"""


@dataclass(frozen=True)
class DecisionConfig:
    """决策节奏与输出平滑。"""

    interval_s: float
    candidate_count: int
    turn_penalty: float
    smoothing_alpha: float
    min_magnitude: float
    max_magnitude: float


@dataclass(frozen=True)
class DangerConfig:
    """危险场：各危险源权重、缓冲、外推采样时间窗。"""

    enemy_weight: float
    enemy_buffer: float
    enemy_default_radius: float
    projectile_weight: float
    projectile_buffer: float
    projectile_default_radius: float
    hazard_weight: float
    hazard_buffer: float
    hazard_default_radius: float
    sample_times_s: tuple[float, ...]
    sample_decay: tuple[float, ...]
    truncated_scale: float
    consider_radius: float
    max_threats_per_kind: int


@dataclass(frozen=True)
class BoundaryConfig:
    """场地边界排斥。"""

    margin: float
    weight: float


@dataclass(frozen=True)
class TacticalConfig:
    """战术层期望位置与默认目标（材料/消耗品/场地中心）的吸引代价。"""

    expected_weight: float
    center_weight: float
    pickup_weight: float
    consumable_weight: float
    pickup_max_distance: float
    attraction_range: float


@dataclass(frozen=True)
class KitingConfig:
    """风筝距离带（由武器射程或默认值决定）。"""

    default_band: tuple[float, float]
    band_weight: float
    far_weight: float


@dataclass(frozen=True)
class EmergencyConfig:
    """低血紧急策略。"""

    hp_threshold: float
    danger_scale: float
    attraction_scale: float


@dataclass(frozen=True)
class InvulnConfig:
    """受伤无敌帧穿越弹幕开关。"""

    enabled: bool
    crossing_scale: float
    trapped_distance: float


@dataclass(frozen=True)
class PlayerConfig:
    """玩家几何与速度估计兜底。"""

    radius: float
    fallback_speed: float


@dataclass(frozen=True)
class ReflexConfig:
    """反射层完整参数集。"""

    decision: DecisionConfig
    danger: DangerConfig
    boundary: BoundaryConfig
    tactical: TacticalConfig
    kiting: KitingConfig
    emergency: EmergencyConfig
    invuln: InvulnConfig
    player: PlayerConfig


def load_reflex_config(path: str | Path | None = None) -> ReflexConfig:
    """读取并校验配置文件；``path=None`` 用包内默认 ``config/reflex.json``。"""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReflexConfigError("无法读取配置文件 %s：%s" % (config_path, exc)) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReflexConfigError("配置文件 %s 非法 JSON：%s" % (config_path, exc)) from exc
    if not isinstance(raw, dict):
        raise ReflexConfigError("配置文件 %s 顶层必须是 JSON 对象" % config_path)
    return reflex_config_from_mapping(raw)


def reflex_config_from_mapping(raw: Mapping[str, Any]) -> ReflexConfig:
    """由 mapping 构造 :class:`ReflexConfig`；键缺失/类型或数值非法抛错。"""
    if not isinstance(raw, Mapping):
        raise ReflexConfigError("配置必须是对象")
    _check_keys(raw, _SECTIONS, "顶层")
    for name in _SECTIONS:
        _check_keys(_section(raw, name), _SECTION_KEYS[name], name)
    decision_raw = _section(raw, "decision")
    danger_raw = _section(raw, "danger")
    boundary_raw = _section(raw, "boundary")
    tactical_raw = _section(raw, "tactical")
    kiting_raw = _section(raw, "kiting")
    emergency_raw = _section(raw, "emergency")
    invuln_raw = _section(raw, "invuln")
    player_raw = _section(raw, "player")

    decision = DecisionConfig(
        interval_s=_number(decision_raw, "interval_s", "decision", minimum=0.001),
        candidate_count=_integer(decision_raw, "candidate_count", "decision", minimum=8, maximum=64),
        turn_penalty=_number(decision_raw, "turn_penalty", "decision", minimum=0.0),
        smoothing_alpha=_number(decision_raw, "smoothing_alpha", "decision", minimum=0.01, maximum=1.0),
        min_magnitude=_number(decision_raw, "min_magnitude", "decision", minimum=0.01, maximum=1.0),
        max_magnitude=_number(decision_raw, "max_magnitude", "decision", minimum=0.01, maximum=1.0),
    )
    if decision.min_magnitude > decision.max_magnitude:
        raise ReflexConfigError("decision.min_magnitude 不能大于 max_magnitude")

    sample_times = _numbers(danger_raw, "sample_times_s", "danger", minimum=0.0)
    sample_decay = _numbers(danger_raw, "sample_decay", "danger", minimum=0.0)
    if len(sample_times) != len(sample_decay):
        raise ReflexConfigError("danger.sample_times_s 与 danger.sample_decay 长度必须一致")
    if any(later <= earlier for earlier, later in zip(sample_times, sample_times[1:])):
        raise ReflexConfigError("danger.sample_times_s 必须严格递增")

    danger = DangerConfig(
        enemy_weight=_number(danger_raw, "enemy_weight", "danger", minimum=0.0),
        enemy_buffer=_number(danger_raw, "enemy_buffer", "danger", minimum=0.0),
        enemy_default_radius=_number(danger_raw, "enemy_default_radius", "danger", minimum=0.0),
        projectile_weight=_number(danger_raw, "projectile_weight", "danger", minimum=0.0),
        projectile_buffer=_number(danger_raw, "projectile_buffer", "danger", minimum=0.0),
        projectile_default_radius=_number(
            danger_raw, "projectile_default_radius", "danger", minimum=0.0
        ),
        hazard_weight=_number(danger_raw, "hazard_weight", "danger", minimum=0.0),
        hazard_buffer=_number(danger_raw, "hazard_buffer", "danger", minimum=0.0),
        hazard_default_radius=_number(danger_raw, "hazard_default_radius", "danger", minimum=0.0),
        sample_times_s=sample_times,
        sample_decay=sample_decay,
        truncated_scale=_number(danger_raw, "truncated_scale", "danger", minimum=1.0),
        consider_radius=_number(danger_raw, "consider_radius", "danger", minimum=0.0),
        max_threats_per_kind=_integer(
            danger_raw, "max_threats_per_kind", "danger", minimum=1, maximum=1000
        ),
    )

    boundary = BoundaryConfig(
        margin=_number(boundary_raw, "margin", "boundary", minimum=0.0),
        weight=_number(boundary_raw, "weight", "boundary", minimum=0.0),
    )

    tactical = TacticalConfig(
        expected_weight=_number(tactical_raw, "expected_weight", "tactical", minimum=0.0),
        center_weight=_number(tactical_raw, "center_weight", "tactical", minimum=0.0),
        pickup_weight=_number(tactical_raw, "pickup_weight", "tactical", minimum=0.0),
        consumable_weight=_number(tactical_raw, "consumable_weight", "tactical", minimum=0.0),
        pickup_max_distance=_number(tactical_raw, "pickup_max_distance", "tactical", minimum=0.0),
        attraction_range=_number(tactical_raw, "attraction_range", "tactical", minimum=0.001),
    )

    band = _numbers(kiting_raw, "default_band", "kiting", minimum=0.0)
    if len(band) != 2 or band[0] >= band[1] or band[0] <= 0.0:
        raise ReflexConfigError("kiting.default_band 必须是两个递增正数 [min, max]")
    kiting = KitingConfig(
        default_band=(band[0], band[1]),
        band_weight=_number(kiting_raw, "band_weight", "kiting", minimum=0.0),
        far_weight=_number(kiting_raw, "far_weight", "kiting", minimum=0.0),
    )

    emergency = EmergencyConfig(
        hp_threshold=_number(emergency_raw, "hp_threshold", "emergency", minimum=0.01, maximum=1.0),
        danger_scale=_number(emergency_raw, "danger_scale", "emergency", minimum=1.0),
        attraction_scale=_number(
            emergency_raw, "attraction_scale", "emergency", minimum=0.0, maximum=1.0
        ),
    )

    invuln = InvulnConfig(
        enabled=_boolean(invuln_raw, "enabled", "invuln"),
        crossing_scale=_number(invuln_raw, "crossing_scale", "invuln", minimum=0.0, maximum=1.0),
        trapped_distance=_number(invuln_raw, "trapped_distance", "invuln", minimum=0.0),
    )

    player = PlayerConfig(
        radius=_number(player_raw, "radius", "player", minimum=0.0),
        fallback_speed=_number(player_raw, "fallback_speed", "player", minimum=0.001),
    )

    return ReflexConfig(
        decision=decision,
        danger=danger,
        boundary=boundary,
        tactical=tactical,
        kiting=kiting,
        emergency=emergency,
        invuln=invuln,
        player=player,
    )


# ---- 校验辅助 ----


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name)
    if not isinstance(value, Mapping):
        raise ReflexConfigError("缺少配置分区 %s（必须是对象）" % name)
    return value


def _check_keys(section: Mapping[str, Any], expected: Sequence[str], where: str) -> None:
    expected_set = set(expected)
    missing = [key for key in expected if key not in section]
    extra = [key for key in section if key not in expected_set]
    if missing:
        raise ReflexConfigError("%s 缺少键：%s" % (where, "、".join(sorted(missing))))
    if extra:
        raise ReflexConfigError("%s 存在未知键：%s" % (where, "、".join(sorted(extra))))


def _number(
    section: Mapping[str, Any],
    key: str,
    where: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReflexConfigError("%s.%s 必须是数字，得到 %r" % (where, key, value))
    number = float(value)
    if number < minimum or (maximum is not None and number > maximum):
        bound = "[%g, %g]" % (minimum, maximum) if maximum is not None else ">= %g" % minimum
        raise ReflexConfigError("%s.%s=%g 超出范围 %s" % (where, key, number, bound))
    return number


def _integer(
    section: Mapping[str, Any],
    key: str,
    where: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReflexConfigError("%s.%s 必须是整数，得到 %r" % (where, key, value))
    if value < minimum or value > maximum:
        raise ReflexConfigError(
            "%s.%s=%d 超出范围 [%d, %d]" % (where, key, value, minimum, maximum)
        )
    return value


def _boolean(section: Mapping[str, Any], key: str, where: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise ReflexConfigError("%s.%s 必须是布尔值，得到 %r" % (where, key, value))
    return value


def _numbers(
    section: Mapping[str, Any], key: str, where: str, *, minimum: float
) -> tuple[float, ...]:
    value = section.get(key)
    if not isinstance(value, (list, tuple)):
        raise ReflexConfigError("%s.%s 必须是数字数组，得到 %r" % (where, key, value))
    result = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ReflexConfigError(
                "%s.%s[%d] 必须是数字，得到 %r" % (where, key, index, item)
            )
        number = float(item)
        if number < minimum:
            raise ReflexConfigError("%s.%s[%d]=%g 超出范围 >= %g" % (where, key, index, number, minimum))
        result.append(number)
    if not result:
        raise ReflexConfigError("%s.%s 不能为空" % (where, key))
    return tuple(result)
