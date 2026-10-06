extends Reference

# 知识库导出（票据 11）：从游戏运行时资源导出与锁定版本一致的静态数据 JSON。
#
# 数据源（运行时单例，均为游戏启动时已加载的资源对象）：
#   /root/ItemService.items / weapons / upgrades / characters / sets；
#   /root/ChallengeService.challenges（匹配英雄解锁条件，按 name 关联）。
# 输出：user://auto_brotato_knowledge/{items,weapons,upgrades,characters}.json。
# 条目构建在 knowledge_entries.gd；本文件负责触发入口、落盘与版本哈希。
#
# 稳定性约定：条目按 id 排序、字典键排序输出、不写时间戳/运行时状态字段；
# 同一游戏版本 + 同一 UI 语言重复导出逐字节一致（name 为 TranslationServer
# 译名，随语言变化；锁定环境 zh）。data_version = 规范 JSON（不含 data_version
# 与 mod_version）的 SHA-256，供决策层标识知识库版本（strategy.md §7）。
# 触发方式见 mod_main.gd 的 debug_export_knowledge 动作（tools/export_knowledge.py 收集）。

const SCHEMA_VERSION := 1
const OUTPUT_DIR := "user://auto_brotato_knowledge"
const FILE_NAMES := {
	"items": "items.json",
	"weapons": "weapons.json",
	"upgrades": "upgrades.json",
	"characters": "characters.json",
}

const KnowledgeEntries := preload(
	"res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/knowledge_entries.gd"
)

var _game_version := ""
var _mod_version := ""
var _entries


func _init(game_version: String, mod_version: String) -> void:
	_game_version = game_version
	_mod_version = mod_version
	_entries = KnowledgeEntries.new()


# 导出全部四类数据并写盘；返回 ack 载荷（ok/dir/files/counts/data_versions）。
func export_all() -> Dictionary:
	var item_service := _find_node("/root/ItemService")
	if item_service == null:
		return {"ok": false, "error": "item_service_missing"}
	var mkdir_error := _ensure_output_dir()
	if mkdir_error != "":
		return {"ok": false, "error": mkdir_error}
	var challenge_service := _find_node("/root/ChallengeService")
	# `_entries` 为无类型引用（脚本实例），返回值为 Variant，不能使用 `:=` 推断
	var challenges = _entries.index_challenges(challenge_service)
	var items = _entries.build_items(item_service.get("items"))
	var weapons = _entries.build_weapons(item_service.get("weapons"))
	var upgrades = _entries.build_upgrades(item_service.get("upgrades"))
	var characters = _entries.build_characters(item_service.get("characters"), challenges)
	var sets = _entries.build_sets(item_service.get("sets"))

	var files := {}
	var data_versions := {}
	for dataset in [
		["items", items, {}],
		["weapons", weapons, {"sets": sets}],
		["upgrades", upgrades, {}],
		["characters", characters, {}],
	]:
		var outcome: Dictionary = _write_dataset(dataset[0], dataset[1], dataset[2])
		if not outcome["ok"]:
			return outcome
		files[outcome["filename"]] = outcome["file"]
		data_versions[outcome["filename"]] = outcome["data_version"]

	return {
		"ok": true,
		"dir": ProjectSettings.globalize_path(OUTPUT_DIR),
		"files": files,
		"data_versions": data_versions,
		"counts": {
			"items": items.size(),
			"weapons": weapons.size(),
			"upgrades": upgrades.size(),
			"characters": characters.size(),
			"sets": sets.size(),
		},
	}


# ---- 落盘与版本哈希 ----

func _write_dataset(dataset: String, entries: Array, extra: Dictionary) -> Dictionary:
	var body := {
		"schema_version": SCHEMA_VERSION,
		"game_version": _game_version,
		"entries": entries,
	}
	for key in extra:
		body[key] = extra[key]
	var payload := body.duplicate(true)
	payload["data_version"] = JSON.print(body, "", true).sha256_text()
	payload["mod_version"] = _mod_version
	var filename: String = FILE_NAMES[dataset]
	var path := OUTPUT_DIR + "/" + filename
	var file := File.new()
	var error := file.open(path, File.WRITE)
	if error != OK:
		return {"ok": false, "error": "write_failed:%s" % filename}
	file.store_string(JSON.print(payload, "  ", true) + "\n")
	file.close()
	return {
		"ok": true,
		"filename": filename,
		"data_version": payload["data_version"],
		"file": {
			"name": filename,
			"sha256": file.get_sha256(path),
			"bytes": _file_size(path),
		},
	}


func _ensure_output_dir() -> String:
	var dir := Directory.new()
	var error := dir.make_dir_recursive(OUTPUT_DIR)
	if error != OK and error != ERR_ALREADY_EXISTS:
		return "mkdir_failed:%d" % error
	return ""


func _find_node(path: String) -> Node:
	var main_loop := Engine.get_main_loop()
	if main_loop is SceneTree:
		return main_loop.get_root().get_node_or_null(path)
	return null


func _file_size(path: String) -> int:
	var file := File.new()
	if file.open(path, File.READ) != OK:
		return -1
	var size := file.get_len()
	file.close()
	return size
