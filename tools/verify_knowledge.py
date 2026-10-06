#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库抽样校验（票据 11）：以游戏 PCK 静态资源为基准，对照 docs/knowledge/*.json。

校验内容：
  1. 覆盖性：四类数据集的 id 集合与游戏 ``item_service.tscn`` 引用的资源逐一对应；
  2. 抽样字段：按固定种子抽取条目，对照资源文件的 tier/price、武器 stats
     （damage/cooldown/range 等）、升级首效果、英雄初始武器；
  3. 版本整备：四文件 game_version/schema_version 一致。

用法（conda 环境 brotato）：
  python tools/verify_knowledge.py
  python tools/verify_knowledge.py --sample 20 --pck "E:\\...\\Brotato.pck"

退出码：0 全部通过；1 存在失败项；2 PCK/知识库不可读。
"""
import argparse
import json
import random
import re
import struct
import sys
from pathlib import Path

DEFAULT_PCK = Path(r"E:\SteamLibrary\steamapps\common\Brotato\Brotato.pck")
DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "docs" / "knowledge"
SERVICE_TSCN = "res://singletons/item_service.tscn"
DATASETS = ("items", "weapons", "upgrades", "characters")
SAMPLE_SEED = 20261006
#: 抽样必含的关键条目（存在才校验）
REFERENCE_IDS = {
    "items": ("item_mushroom",),
    "weapons": ("weapon_pistol_1", "weapon_smg_1"),
    "upgrades": ("upgrade_attack_speed_1",),
    "characters": ("character_ranger",),
}
#: 武器 stats 对照字段
WEAPON_STATS_KEYS = ("damage", "cooldown", "min_range", "max_range", "nb_projectiles", "piercing")

_EXT_RES_RE = re.compile(r'\[ext_resource path="(?P<path>[^"]+)"[^]]*id=(?P<id>\d+)\]')
_ARRAY_RE = r"^%s\s*=\s*\[(?P<body>.*?)\]"
_EXT_REF_RE = re.compile(r"ExtResource\(\s*(\d+)\s*\)")
_SCALAR_RE = re.compile(r"^(?P<key>\w+)\s*=\s*(?P<value>.+?)\s*$", re.M)
_ASSIGN_EXT_RE = re.compile(r"^(?P<key>\w+)\s*=\s*ExtResource\(\s*(?P<id>\d+)\s*\)", re.M)


# ---- PCK 读取 ----

def read_pck_index(pck_path):
    """返回 (bytes, {res_path: (offset, size)})；仅供只读校验。"""
    data = Path(pck_path).read_bytes()
    magic, fmt_ver, vmaj, vmin, vpatch = struct.unpack_from("<IIIII", data, 0)
    if struct.pack("<I", magic) != b"GDPC":
        raise ValueError("不是 Godot PCK：%s" % pck_path)
    offset = 20 + 16 * 4
    (count,) = struct.unpack_from("<I", data, offset)
    offset += 4
    files = {}
    for _ in range(count):
        (path_len,) = struct.unpack_from("<I", data, offset)
        offset += 4
        raw = data[offset:offset + path_len]
        path = raw.split(b"\x00")[0].decode("utf-8", "replace")
        offset += (path_len + 3) & ~3
        file_offset, size = struct.unpack_from("<QQ", data, offset)
        offset += 16 + 16  # offset/size + md5
        if fmt_ver >= 2:
            offset += 4  # flags
        files[path] = (file_offset, size)
    return data, files


def extract_resource(data, files, res_path):
    if res_path not in files:
        return None
    offset, size = files[res_path]
    return data[offset:offset + size].decode("utf-8", "replace")


# ---- .tres / .tscn 文本解析（纯函数，便于测试） ----

def parse_ext_resources(text):
    """``[ext_resource ...]`` → {id: path}。"""
    return {int(match.group("id")): match.group("path") for match in _EXT_RES_RE.finditer(text)}


def parse_service_arrays(text):
    """item_service.tscn 的 items/weapons/upgrades/characters 数组 → {name: [资源路径]}。"""
    ext_map = parse_ext_resources(text)
    arrays = {}
    for name in DATASETS:
        match = re.search(_ARRAY_RE % name, text, re.M | re.S)
        if not match:
            continue
        arrays[name] = [
            ext_map[int(ref)]
            for ref in _EXT_REF_RE.findall(match.group("body"))
            if int(ref) in ext_map
        ]
    return arrays


def parse_scalars(text):
    """``key = value`` 标量 → dict（字符串/布尔/数字）。"""
    result = {}
    for match in _SCALAR_RE.finditer(text):
        result[match.group("key")] = _parse_scalar(match.group("value"))
    return result


def _parse_scalar(raw):
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return raw


def parse_ext_refs(text):
    """``key = ExtResource( N )`` → {key: id}。"""
    return {match.group("key"): int(match.group("id")) for match in _ASSIGN_EXT_RE.finditer(text)}


def sample_ids(ids, count, seed=SAMPLE_SEED):
    """固定种子抽样（结果可复现）；ids 已排序。"""
    ordered = sorted(ids)
    if len(ordered) <= count:
        return ordered
    return sorted(random.Random(seed).sample(ordered, count))


# ---- 校验 ----

class Checker:
    def __init__(self, data, files):
        self.data = data
        self.files = files
        self.failures = []
        self.checks = 0

    def check(self, condition, message):
        self.checks += 1
        if not condition:
            self.failures.append(message)
            print("[失败] %s" % message)

    def resource_text(self, res_path):
        text = extract_resource(self.data, self.files, res_path)
        if text is None:
            raise ValueError("PCK 中缺少资源：%s" % res_path)
        return text


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify(pck_path, knowledge_dir, sample_count):
    checker = Checker(*read_pck_index(pck_path))
    tscn = checker.resource_text(SERVICE_TSCN)
    arrays = parse_service_arrays(tscn)
    payloads = {name: load_json(knowledge_dir / (name + ".json")) for name in DATASETS}
    entries = {name: {entry["id"]: entry for entry in payloads[name]["entries"]} for name in DATASETS}

    print("[版本] game_version=%s schema_version=%s" % (
        payloads["items"]["game_version"], payloads["items"]["schema_version"]))
    for name in DATASETS:
        payload = payloads[name]
        checker.check(
            payload["game_version"] == payloads["items"]["game_version"],
            "%s.json game_version 与 items.json 不一致" % name,
        )
        checker.check(
            payload["schema_version"] == payloads["items"]["schema_version"],
            "%s.json schema_version 与 items.json 不一致" % name,
        )
        checker.check(
            isinstance(payload.get("data_version"), str) and len(payload["data_version"]) == 64,
            "%s.json data_version 缺失或长度不为 64" % name,
        )
        print("[版本] %s.json data_version=%s（%d 条）" % (
            name, payload["data_version"][:16], len(payload["entries"])))

    # 1) 覆盖性：PCK 资源 id 全集 vs JSON id 全集
    for name in DATASETS:
        pck_ids = set()
        for res_path in arrays.get(name, []):
            text = checker.resource_text(res_path)
            resource_id = parse_scalars(text).get("my_id")
            if isinstance(resource_id, str):
                pck_ids.add(resource_id)
        json_ids = set(entries[name])
        missing = sorted(pck_ids - json_ids)
        extra = sorted(json_ids - pck_ids)
        checker.check(
            not missing,
            "%s 缺少 PCK 中的条目 %d 个：%s%s" % (
                name, len(missing), missing[:8], "..." if len(missing) > 8 else ""),
        )
        checker.check(
            not extra,
            "%s 存在 PCK 之外的条目 %d 个：%s%s" % (
                name, len(extra), extra[:8], "..." if len(extra) > 8 else ""),
        )
        print("[覆盖] %s：PCK %d 条 / JSON %d 条" % (name, len(pck_ids), len(json_ids)))

    # 2) 抽样字段对照
    for name, res_paths in arrays.items():
        by_id = {}
        for res_path in res_paths:
            text = checker.resource_text(res_path)
            resource_id = parse_scalars(text).get("my_id")
            if isinstance(resource_id, str):
                by_id[resource_id] = res_path
        sample = set(REFERENCE_IDS.get(name, ()))
        sample.update(sample_ids(by_id, sample_count))
        for resource_id in sorted(sample & set(by_id)):
            entry = entries[name][resource_id]
            _verify_entry(checker, name, entry, by_id[resource_id])

    # 3) tier_list 引用合法（结构性；由 agent 加载器再告警）
    tier_path = knowledge_dir / "tier_list.json"
    if tier_path.is_file():
        tier_payload = load_json(tier_path)
        known = set(entries["items"]) | set(entries["weapons"])
        known |= {
            entry.get("weapon_id")
            for entry in entries["weapons"].values()
            if isinstance(entry.get("weapon_id"), str)
        }
        for rating_id in (tier_payload.get("ratings") or {}):
            checker.check(rating_id in known, "tier_list 引用了未知条目：%s" % rating_id)
    else:
        checker.check(False, "缺少 tier_list.json")

    print("[结论] %d 项检查，%s" % (checker.checks, "全部通过" if not checker.failures else "%d 项失败" % len(checker.failures)))
    return 1 if checker.failures else 0


def _verify_entry(checker, name, entry, res_path):
    text = checker.resource_text(res_path)
    scalars = parse_scalars(text)
    checker.check(scalars.get("my_id") == entry["id"], "%s my_id 不一致" % res_path)
    checker.check(scalars.get("tier") == entry.get("tier"), "%s tier %s != %s" % (res_path, scalars.get("tier"), entry.get("tier")))
    if name in ("items", "weapons"):
        checker.check(scalars.get("value") == entry.get("price"), "%s value %s != price %s" % (res_path, scalars.get("value"), entry.get("price")))
    if name == "weapons":
        ext_map = parse_ext_resources(text)
        stats_id = parse_ext_refs(text).get("stats")
        if stats_id is not None and stats_id in ext_map:
            stats_text = checker.resource_text(ext_map[stats_id])
            stats = parse_scalars(stats_text)
            json_stats = entry.get("stats") or {}
            for key in WEAPON_STATS_KEYS:
                if key in stats:
                    checker.check(
                        key in json_stats and _same_number(json_stats[key], stats[key]),
                        "%s stats.%s 缺失或不等（PCK=%r JSON=%r）" % (entry["id"], key, stats[key], json_stats.get(key)),
                    )
        next_id = parse_ext_refs(text).get("upgrades_into")
        expected_next = entry.get("upgrades_into")
        if next_id is not None and next_id in ext_map:
            next_scalars = parse_scalars(checker.resource_text(ext_map[next_id]))
            checker.check(next_scalars.get("my_id") == expected_next, "%s upgrades_into %s != %s" % (entry["id"], next_scalars.get("my_id"), expected_next))
    if name == "upgrades":
        ext_map = parse_ext_resources(text)
        effect_ids = _EXT_REF_RE.findall(re.search(_ARRAY_RE % "effects", text, re.M | re.S).group("body"))
        effects = entry.get("effects") or []
        checker.check(len(effect_ids) == len(effects), "%s effects 数量 %d != %d" % (entry["id"], len(effect_ids), len(effects)))
        if effect_ids and effects:
            effect_scalars = parse_scalars(checker.resource_text(ext_map[int(effect_ids[0])]))
            checker.check(effect_scalars.get("key") == effects[0].get("key"), "%s effect.key 不一致" % entry["id"])
            checker.check(_same_number(effect_scalars.get("value"), effects[0].get("value")), "%s effect.value 不一致" % entry["id"])
    if name == "characters":
        ext_map = parse_ext_resources(text)
        match = re.search(_ARRAY_RE % "starting_weapons", text, re.M | re.S)
        count = len(_EXT_REF_RE.findall(match.group("body"))) if match else 0
        json_weapons = entry.get("starting_weapons") or []
        checker.check(count == len(json_weapons), "%s starting_weapons 数量 %d != %d" % (entry["id"], count, len(json_weapons)))
        if match:
            refs = [int(ref) for ref in _EXT_REF_RE.findall(match.group("body"))]
            expected = sorted(json_weapons)
            actual = sorted(
                parse_scalars(checker.resource_text(ext_map[ref])).get("my_id")
                for ref in refs
                if ref in ext_map
            )
            checker.check(actual == expected, "%s starting_weapons 列表不一致" % entry["id"])


def _same_number(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) < 1e-9
    return left == right


def main():
    parser = argparse.ArgumentParser(description="知识库抽样校验（票据 11）")
    parser.add_argument("--pck", default=str(DEFAULT_PCK), help="Brotato.pck 路径")
    parser.add_argument("--knowledge-dir", default=str(DEFAULT_KNOWLEDGE_DIR), help="知识库目录")
    parser.add_argument("--sample", type=int, default=10, help="每类抽样条数（默认 10）")
    args = parser.parse_args()
    knowledge_dir = Path(args.knowledge_dir)
    try:
        return verify(Path(args.pck), knowledge_dir, args.sample)
    except (OSError, ValueError, KeyError) as exc:
        print("[失败] 校验无法执行：%s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
