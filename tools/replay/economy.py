"""回放经济层报告（票据 12）：在同一录制上对比不同参数集的商店决策。

口径（README 级说明，写入报告头）：

- 从录制中提取商店视图（``shop`` 消息，连续相同内容去重）与末快照背包/属性；
- 对每个参数集用 :class:`~ab_agent.decision.economy.EconomyPlanner` 重放这些视图：
  记录计划购买序列（id/价格/评分）、计划累计花费与按观测金币绘制的金币曲线；
- 最终构建强度按"录制末快照的真实背包"在各自参数集下评分（参数敏感性对比），
  同时给出"按计划购买推演的影子背包"强度（影子购买仅做合并/加件，近似值）；
- 观测金币是真实对局事实（可能包含历史消费差异），计划花费仅用于参数集**相对**对比。

确定性：同一录制 + 同一参数集 → 相同决策序列（无随机、无时间依赖，ADR-0008）。

用法（仓库根目录）::

    python -m tools.replay.economy recordings/xxxx.ndjson
    python -m tools.replay.economy recordings/xxxx.ndjson --config a.json --config b.json --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from .recording import RecordingError, iter_records, open_recording

#: agent 包路径（tools/replay 从仓库根运行时 `ab_agent` 不在 sys.path）
_AGENT_ROOT = Path(__file__).resolve().parents[2] / "agent"
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from ab_agent.decision.economy import ACTION_BUY, EconomyPlanner  # noqa: E402
from ab_agent.decision.economy_config import (  # noqa: E402
    EconomyConfigError,
    load_economy_config,
)
from ab_agent.decision.economy_model import EconomyContext, as_int, items  # noqa: E402
from ab_agent.knowledge import DEFAULT_KNOWLEDGE_DIR, KnowledgeBase, KnowledgeError, load_knowledge  # noqa: E402

DEFAULT_LABEL = "默认配置"


@dataclass(frozen=True)
class ShopFrame:
    """一次商店视图（连续相同内容已去重）。"""

    ts: float
    wave: int
    payload: dict


@dataclass(frozen=True)
class ReplayFacts:
    """录制中与经济层相关的事实。"""

    path: Path
    hero_id: str
    shop_frames: tuple[ShopFrame, ...]
    initial_inventory: dict
    final_inventory: dict
    final_stats: dict


@dataclass(frozen=True)
class Purchase:
    wave: int
    slot: int
    id: str
    price: int
    score: float


@dataclass(frozen=True)
class SimResult:
    """一个参数集的重放结果。"""

    label: str
    purchases: tuple[Purchase, ...]
    buy_count: int
    score_sum: float
    score_mean: float
    planned_spend: int
    final_strength: float
    shadow_strength: float
    gold_curve: tuple[tuple[int, int, int], ...]  # (wave, 观测金币, 计划累计花费)


@dataclass(frozen=True)
class ConfigChoice:
    label: str
    path: Optional[str]
    config: Any


def collect_facts(recording) -> ReplayFacts:
    """流式扫描录制，提取商店视图与末背包/属性。"""
    frames: list[ShopFrame] = []
    last_signature: Optional[str] = None
    first_inventory: dict = {}
    final_inventory: dict = {}
    final_stats: dict = {}
    for record in iter_records(recording):
        if record.kind != "in":
            continue
        envelope = record.data.get("envelope") or {}
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            continue
        msg_type = envelope.get("type")
        if msg_type == "shop":
            signature = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            if signature == last_signature:
                continue  # 心跳/未变化的重复推送
            last_signature = signature
            wave = as_int(payload.get("wave_next")) or 0
            frames.append(ShopFrame(ts=record.ts, wave=wave, payload=payload))
            inventory = payload.get("inventory")
            if not first_inventory and isinstance(inventory, dict):
                first_inventory = inventory
        elif msg_type == "snapshot":
            inventory = payload.get("inventory")
            if isinstance(inventory, dict) and (inventory.get("weapons") or inventory.get("items")):
                final_inventory = inventory
            stats = payload.get("stats")
            if isinstance(stats, dict) and stats:
                final_stats = stats
    if not final_inventory and frames:
        final_inventory = frames[-1].payload.get("inventory") or {}
    if not first_inventory:
        first_inventory = final_inventory
    header_hero = recording.header.get("hero_id")
    return ReplayFacts(
        path=recording.path,
        hero_id=str(header_hero) if isinstance(header_hero, str) else "",
        shop_frames=tuple(frames),
        initial_inventory=first_inventory,
        final_inventory=final_inventory,
        final_stats=final_stats,
    )


def simulate(facts: ReplayFacts, planner: EconomyPlanner, label: str) -> SimResult:
    """按参数集重放商店视图（纯函数；同输入同输出）。"""
    shadow = _clone_inventory(facts.initial_inventory)
    purchases: list[Purchase] = []
    gold_curve: list[tuple[int, int, int]] = []
    planned_spend = 0
    for frame in facts.shop_frames:
        context = EconomyContext(
            wave=frame.wave,
            gold=as_int(frame.payload.get("gold")) or 0,
            stats=frame.payload.get("stats") if isinstance(frame.payload.get("stats"), dict) else {},
            inventory=shadow,
            hero_id=facts.hero_id,
        )
        plan = planner.plan_shop(frame.payload, context)
        if plan.action == ACTION_BUY:
            appraisal = next(
                (item for item in plan.appraisals if item.slot == plan.params.get("slot")),
                None,
            )
            if appraisal is not None:
                purchases.append(
                    Purchase(
                        wave=frame.wave,
                        slot=appraisal.slot,
                        id=appraisal.id,
                        price=appraisal.price,
                        score=appraisal.score.total,
                    )
                )
                planned_spend += appraisal.price
                shadow = _apply_purchase(shadow, appraisal, planner)
        gold_curve.append((frame.wave, context.gold, planned_spend))
    scores = [purchase.score for purchase in purchases]
    context = EconomyContext(
        wave=0,
        gold=0,
        stats=facts.final_stats,
        inventory=facts.final_inventory,
        hero_id=facts.hero_id,
    )
    shadow_context = EconomyContext(
        wave=0,
        gold=0,
        stats=facts.final_stats,
        inventory=shadow,
        hero_id=facts.hero_id,
    )
    return SimResult(
        label=label,
        purchases=tuple(purchases),
        buy_count=len(purchases),
        score_sum=sum(scores),
        score_mean=(sum(scores) / len(scores)) if scores else 0.0,
        planned_spend=planned_spend,
        final_strength=planner.inventory_strength(facts.final_inventory, context),
        shadow_strength=planner.inventory_strength(shadow, shadow_context),
        gold_curve=tuple(gold_curve),
    )


def _apply_purchase(
    inventory: dict, appraisal, planner: EconomyPlanner
) -> dict:
    """影子背包：买入一件（武器同族同阶时按合并链升阶的近似处理）。"""
    result = _clone_inventory(inventory)
    if appraisal.kind == "weapon":
        weapons = result.setdefault("weapons", [])
        family = planner.scorer.family(appraisal.id)
        merged = False
        for index, weapon in enumerate(weapons):
            if (
                planner.scorer.family(str(weapon.get("id") or "")) == family
                and (as_int(weapon.get("tier")) or 0) == appraisal.tier
            ):
                upgraded = planner.scorer.weapon_entry(family, appraisal.tier + 1)
                weapons.pop(index)
                if upgraded is not None:
                    weapons.append(
                        {"slot": len(weapons), "id": upgraded.get("id"), "tier": appraisal.tier + 1}
                    )
                else:
                    weapons.append({"slot": len(weapons), "id": appraisal.id, "tier": appraisal.tier})
                merged = True
                break
        if not merged:
            weapons.append({"slot": len(weapons), "id": appraisal.id, "tier": appraisal.tier})
    else:
        result.setdefault("items", []).append({"id": appraisal.id, "count": 1})
    return result


def _clone_inventory(inventory: Any) -> dict:
    if not isinstance(inventory, dict):
        return {"weapons": [], "items": []}
    return {
        "weapons": [dict(weapon) for weapon in items(inventory.get("weapons"))],
        "items": [dict(item) for item in items(inventory.get("items"))],
    }


def build_configs(config_paths: Sequence[str]) -> list[ConfigChoice]:
    """构建参数集列表（无 --config 时为默认配置；重复路径去重）。"""
    choices: "OrderedDict[str, ConfigChoice]" = OrderedDict()
    if not config_paths:
        return [ConfigChoice(DEFAULT_LABEL, None, load_economy_config())]
    for path in config_paths:
        if path in choices:
            continue
        choices[path] = ConfigChoice(
            label=Path(path).name, path=path, config=load_economy_config(path)
        )
    return list(choices.values())


def format_report(facts: ReplayFacts, results: Sequence[SimResult]) -> str:
    waves = [frame.wave for frame in facts.shop_frames if frame.wave]
    lines = [
        "回放经济层报告（票据 12）",
        "录制：%s" % facts.path,
        "商店视图：%d 次%s"
        % (
            len(facts.shop_frames),
            "（第 %d–%d 波）" % (min(waves), max(waves)) if waves else "",
        ),
        "口径：观测金币为真实事实；计划花费/购买序列为参数集反事实（影子背包为近似值）",
        "",
        _format_table(results),
    ]
    for result in results:
        lines.append("")
        lines.append("[购买序列] %s" % result.label)
        if not result.purchases:
            lines.append("  无购买")
            continue
        lines.append(
            "  "
            + " · ".join(
                "第%d波 槽%d %s 评分 %.1f 价 %d"
                % (purchase.wave, purchase.slot, purchase.id, purchase.score, purchase.price)
                for purchase in result.purchases
            )
        )
    lines.append("")
    lines.append("[金币曲线] 波次: 观测金币 / 计划累计花费")
    for result in results:
        curve = " · ".join(
            "第%d波 %d/%d" % (wave, gold, spend) for wave, gold, spend in result.gold_curve
        )
        lines.append("  %s：%s" % (result.label, curve or "无商店视图"))
    return "\n".join(lines)


def _format_table(results: Sequence[SimResult]) -> str:
    header = "%-22s %14s %14s %12s" % (
        "指标",
        results[0].label,
        results[1].label if len(results) > 1 else "",
        "差值",
    )
    lines = ["[参数集对比]", header, "-" * len(header)]
    first = results[0]
    second = results[1] if len(results) > 1 else None

    def row(label: str, a: float, b: Optional[float], fmt: str, delta: str) -> str:
        if b is None:
            return "%-22s %14s" % (label, fmt % a)
        return "%-22s %14s %14s %12s" % (label, fmt % a, fmt % b, delta % (a - b))

    lines.append(row("购买次数", first.buy_count, second.buy_count if second else None, "%d", "%+d"))
    lines.append(
        row("购买评分合计", first.score_sum, second.score_sum if second else None, "%.1f", "%+.1f")
    )
    lines.append(
        row(
            "购买评分均值",
            first.score_mean,
            second.score_mean if second else None,
            "%.2f",
            "%+.2f",
        )
    )
    lines.append(
        row("计划花费", first.planned_spend, second.planned_spend if second else None, "%d", "%+d")
    )
    lines.append(
        row(
            "最终构建强度(录制末背包)",
            first.final_strength,
            second.final_strength if second else None,
            "%.1f",
            "%+.1f",
        )
    )
    lines.append(
        row(
            "影子背包强度(按计划购买)",
            first.shadow_strength,
            second.shadow_strength if second else None,
            "%.1f",
            "%+.1f",
        )
    )
    return "\n".join(lines)


def json_payload(facts: ReplayFacts, results: Sequence[SimResult]) -> dict:
    return {
        "recording": str(facts.path),
        "hero_id": facts.hero_id,
        "shop_views": len(facts.shop_frames),
        "waves": [frame.wave for frame in facts.shop_frames],
        "configs": [
            {
                "label": result.label,
                "buy_count": result.buy_count,
                "score_sum": round(result.score_sum, 3),
                "score_mean": round(result.score_mean, 3),
                "planned_spend": result.planned_spend,
                "final_strength": round(result.final_strength, 3),
                "shadow_strength": round(result.shadow_strength, 3),
                "purchases": [
                    {
                        "wave": purchase.wave,
                        "slot": purchase.slot,
                        "id": purchase.id,
                        "price": purchase.price,
                        "score": round(purchase.score, 3),
                    }
                    for purchase in result.purchases
                ],
                "gold_curve": [list(point) for point in result.gold_curve],
            }
            for result in results
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.replay.economy", description="回放经济层报告（票据 12）"
    )
    parser.add_argument("recording", help="录制文件路径（.ndjson 或 .ndjson.gz）")
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help="经济层参数文件（可重复；默认使用包内默认配置）",
    )
    parser.add_argument("--knowledge-dir", default=None, help="知识库目录（默认 docs/knowledge）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于留档对比）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        recording = open_recording(args.recording)
    except RecordingError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        return 1
    try:
        knowledge: Optional[KnowledgeBase] = load_knowledge(args.knowledge_dir)
    except KnowledgeError as exc:
        print("[错误] 知识库加载失败：%s" % exc, file=sys.stderr)
        return 1
    try:
        choices = build_configs(args.config)
    except EconomyConfigError as exc:
        print("[错误] 经济层参数非法：%s" % exc, file=sys.stderr)
        return 1
    try:
        facts = collect_facts(recording)
        results = [
            simulate(facts, EconomyPlanner(knowledge, choice.config), choice.label)
            for choice in choices
        ]
    except RecordingError as exc:
        print("[错误] 录制文件损坏：%s" % exc, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(json_payload(facts, results), ensure_ascii=False, indent=2))
        return 0
    print(format_report(facts, results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
