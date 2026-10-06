extends Reference

# 菜单观测（票据 05）：难度选择页与终局页出现时，提供与 docs/protocol.md §7.1 对齐的
# 只读快照；推送策略由 mod_main 与 shop 一致处理（内容变化 + 1s 心跳，不在菜单时不推送）。
# 本模块同时向 menu_actions（票据 06）暴露难度页只读访问器（元素/焦点/可开始判定）；
# 动作执行仍由 menu_actions 经 UI 信号完成。升级页观测见 level_up_observation.gd。
#
# 实机结论来源（票据 04）：
# - 难度页 /root/DifficultySelection（difficulty_selection.tscn）：CharacterPanel/WeaponPanel
#   的 item_data、Inventory1 的 difficulty_data 列表、根变量 _latest_focused_element /
#   displayed_elements / add_random_element / enable_coop_panels；模式开关在 RunOptionsPanel。
# - 终局页 /root/EndRun（end_run.tscn）：RunData.run_won / current_wave 与 StatsContainer
#   各 stat_container.gd 格子的 key/Value 文本。
#
# 引擎兼容（ADR-0006）：动态 get()/get_node_or_null()，不依赖 class_name；整数字段经
# engine_compat.int_arg 收敛；场景根按「实机路径 + 场景文件名全树扫描」双路径获取。

const LOG_NAME := "BrotatoPlayer-AutoBrotato:MenuObservation"
const RESCAN_INTERVAL_MS := 500
const PLAYER_INDEX := 0
const DIFFICULTY_SCENE := "difficulty_selection.tscn"
const RUN_END_SCENE := "end_run.tscn"
const DIFFICULTY_ROOT_PATH := "DifficultySelection"
const RUN_END_ROOT_PATH := "EndRun"

# 难度页内固定路径（实机票据 04；游戏版本锁定 ADR-0005）
const DIFFICULTY_PANEL_PATH := "MarginContainer/VBoxContainer/DescriptionContainer"
const DIFFICULTY_INVENTORY_PATH := (
	"MarginContainer/VBoxContainer/ScrollContainer/Inventories/Inventory1"
)

const Compat := preload("res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/engine_compat.gd")

var _difficulty = null
var _end_run = null
var _next_scan_ms := 0
var _latest = null


# 按需采样（mod_main 以 ~10Hz 调用）；未处于菜单时 latest() 返回 null。
func sample() -> void:
	var now := OS.get_ticks_msec()
	if not _root_valid(_difficulty):
		_difficulty = null
	if not _root_valid(_end_run):
		_end_run = null
	if _difficulty == null and _end_run == null:
		if now < _next_scan_ms:
			_latest = null
			return
		_next_scan_ms = now + RESCAN_INTERVAL_MS
		_find_roots()
	if _difficulty != null:
		_latest = _build_difficulty_payload()
	elif _end_run != null:
		_latest = _build_run_end_payload()
	else:
		_latest = null


func latest():
	return _latest


# 连接断开/游戏侧重置时调用：丢弃节点缓存，强制重新扫描。
func invalidate() -> void:
	_difficulty = null
	_end_run = null
	_latest = null
	_next_scan_ms = 0


func _find_roots() -> void:
	var tree := Engine.get_main_loop() as SceneTree
	if tree == null:
		return
	_difficulty = _find_scene_root(tree, DIFFICULTY_ROOT_PATH, DIFFICULTY_SCENE)
	_end_run = _find_scene_root(tree, RUN_END_ROOT_PATH, RUN_END_SCENE)


# 双路径：先按实机路径直取，再按场景文件名全树扫描；只接受「在屏幕上」的实例，
# 排除被菜单系统移出屏幕但仍留在树中的滞留页面（离场判定依赖该检查）。
func _find_scene_root(tree: SceneTree, root_path: String, scene_suffix: String):
	var direct = tree.root.get_node_or_null(root_path)
	if _root_valid(direct) and _matches_scene(direct, scene_suffix):
		return direct
	var stack := [tree.root]
	while not stack.empty():
		var node = stack.pop_back()
		if node is Node:
			if _matches_scene(node, scene_suffix) and _root_valid(node):
				return node
			for child in node.get_children():
				stack.push_back(child)
	return null


func _matches_scene(node, suffix: String) -> bool:
	if node == null or not is_instance_valid(node) or not (node is Node):
		return false
	return str(node.get_filename()).ends_with(suffix)


func _root_valid(node) -> bool:
	if node == null or not is_instance_valid(node) or not (node is Node):
		return false
	if not node.is_inside_tree():
		return false
	if node is CanvasItem and not node.is_visible_in_tree():
		return false
	if node is Control:
		var viewport = node.get_viewport()
		if viewport != null:
			return node.get_global_rect().intersects(viewport.get_visible_rect())
	return true


func _build_difficulty_payload() -> Dictionary:
	return {
		"phase": "difficulty_select",
		"character": {"id": _character_id()},
		"weapons": _weapons_payload(),
		"difficulty": {
			"options": _difficulty_options(),
			"selected": selected_difficulty_id(),
			"displayed": _displayed_difficulty_ids(),
		},
		"modes": _modes_payload(),
		"can_start": can_start(),
	}


# —— 只读访问器（menu_actions 使用） ——

func in_difficulty() -> bool:
	return _root_valid(_difficulty)


# 难度元素（item.my_id == "difficulty_<value>"）；不存在/不在页面时返回 null。
func difficulty_element(value: int):
	if not in_difficulty():
		return null
	var inventory = _difficulty.get_node_or_null(DIFFICULTY_INVENTORY_PATH)
	if inventory == null:
		return null
	var target := "difficulty_%d" % value
	for child in inventory.get_children():
		if _live(child) and _element_item_id(child) == target:
			return child
	return null


# 当前预选/高亮元素（根脚本变量 _latest_focused_element[0]）；无人聚焦时返回 null。
func focused_element():
	if _difficulty == null:
		return null
	var focused = _difficulty.get("_latest_focused_element")
	if typeof(focused) != TYPE_ARRAY or focused.size() <= PLAYER_INDEX:
		return null
	var element = focused[PLAYER_INDEX]
	if element == null or not is_instance_valid(element):
		return null
	return element


func selected_difficulty_id():
	var element = focused_element()
	if element == null:
		return null
	return _element_item_id(element)


# 实机（反编译 difficulty_selection.gd）：pressed 被接受的条件就是元素非 special
# （锁定与随机元素 is_special=true，其 pressed 被直接忽略、无副作用）。
func can_start() -> bool:
	var element = focused_element()
	if element == null:
		return false
	return element.get("is_special") != true


# 开局离场判定（durable）：游戏 `_on_element_pressed` 切到 `main.tscn`（/root/Main 存在）；
# BackButton `_go_back` 切回选武器/选人页（无 Main）。不能依赖 RunData.menu_selection_back：
# 目标菜单页 `_ready` 会把它复位为 false，跨帧判定失效（票据 06 实机修正）。
func in_run_scene() -> bool:
	var tree := Engine.get_main_loop() as SceneTree
	if tree == null:
		return false
	return tree.root.get_node_or_null("Main") != null


func _character_id() -> String:
	var panel = _difficulty.get_node_or_null(DIFFICULTY_PANEL_PATH + "/CharacterPanel")
	if panel == null:
		return ""
	# character_panel_ui.gd 的 item_data = character_data
	var item = panel.get("item_data")
	if item == null or not is_instance_valid(item):
		return ""
	return _string_of(item.get("my_id"))


func _weapons_payload() -> Array:
	var weapons := []
	var panel = _difficulty.get_node_or_null(DIFFICULTY_PANEL_PATH + "/WeaponPanel")
	if panel == null:
		return weapons
	# item_panel_ui.gd 的 item_data = weapon_data（当前单武器；保留数组以对齐协议字段）
	var item = panel.get("item_data")
	if item == null or not is_instance_valid(item):
		return weapons
	weapons.append(
		{
			"my_id": _string_of(item.get("my_id")),
			"weapon_id": _string_of(item.get("weapon_id")),
			"tier": Compat.int_arg(item.get("tier")),
		}
	)
	return weapons


func _difficulty_options() -> Array:
	var options := []
	var inventory = _difficulty.get_node_or_null(DIFFICULTY_INVENTORY_PATH)
	if inventory == null:
		return options
	for child in inventory.get_children():
		if not _live(child):
			continue
		var item = child.get("item")
		if item == null or typeof(item) != TYPE_OBJECT or not is_instance_valid(item):
			continue
		# difficulty_data.gd：my_id=difficulty_0..6；元素自身 current_number 语义未定，仅记录
		options.append(
			{
				"my_id": _string_of(item.get("my_id")),
				"name": _string_of(item.get("name")),
				"tier": Compat.int_arg(item.get("tier")),
				"value": Compat.int_arg(item.get("value")),
				"is_locked": item.get("is_locked") == true,
				"unlocked_by_default": item.get("unlocked_by_default") == true,
				"current_number": Compat.int_arg(child.get("current_number")),
			}
		)
	return options


# 根脚本变量 displayed_elements（每玩家一个 difficulty_data 数组）；报告玩家 0 的 my_id 列表。
func _displayed_difficulty_ids() -> Array:
	var ids := []
	var displayed = _difficulty.get("displayed_elements")
	if typeof(displayed) != TYPE_ARRAY or displayed.size() <= PLAYER_INDEX:
		return ids
	var player_list = displayed[PLAYER_INDEX]
	if typeof(player_list) != TYPE_ARRAY:
		return ids
	for item in player_list:
		if item == null or not is_instance_valid(item):
			continue
		ids.append(_string_of(item.get("my_id")))
	return ids


func _modes_payload() -> Dictionary:
	# 模式开关的权威状态在 RunData（选人页 RunOptionsPanel 只负责开关 UI，离开选人页后
	# 面板即释放；实机票据 05 验证：在选人页开启无尽后，难度页 RunData.is_endless_run=true）。
	return {
		"endless": _run_data_bool("is_endless_run"),
		"ban": _run_data_bool("is_ban_mode_active"),
		"coop": _run_data_bool("is_coop_run"),
		# 难度页根脚本变量（票据 04 实机确认存在；语义未证实，仅记录）
		"add_random_element": _difficulty.get("add_random_element") == true,
		"enable_coop_panels": _difficulty.get("enable_coop_panels") == true,
	}


func _run_data_bool(field: String) -> bool:
	if RunData == null:
		return false
	return RunData.get(field) == true


func _build_run_end_payload() -> Dictionary:
	return {
		"phase": "run_end",
		"result": _run_result(),
		"wave": _run_wave(),
		"title": _end_run_title(),
		"stats": _stats_payload(),
	}


# 结构化来源（票据 04 实机）：RunData.run_won（胜利 true / 战败 false）；读不到给 null。
func _run_result():
	if RunData == null:
		return null
	var won = RunData.get("run_won")
	if typeof(won) == TYPE_BOOL:
		return "victory" if won else "defeat"
	return null


func _run_wave():
	if RunData == null:
		return null
	return Compat.int_arg(RunData.get("current_wave"))


# 双路径（ADR-0006）：`%Title`（票据 04 实机）优先，回退固定路径（票据 05 实机验证）。
func _end_run_title() -> String:
	var label = _end_run.get_node_or_null("%Title")
	if label == null:
		label = _end_run.get_node_or_null("MarginContainer/VBoxContainer/HBoxContainer/Title")
	if label == null:
		return ""
	return str(label.get("text"))


# 终局统计摘要：StatsContainer 下各 stat_container.gd 格子的 key -> Value 文本（原样展示串）。
func _stats_payload() -> Dictionary:
	var stats := {}
	var container = (
		_end_run.get_node_or_null(
			"MarginContainer/VBoxContainer/PanelContainer/HBoxContainer/StatsContainer"
		)
	)
	if container == null:
		return stats
	var stack := [container]
	while not stack.empty():
		var node = stack.pop_back()
		if not _live(node):
			continue
		var key = node.get("key")
		if typeof(key) == TYPE_STRING and str(key) != "":
			var value_label = node.get("_value")
			if value_label == null or not is_instance_valid(value_label):
				value_label = node.get_node_or_null("HBoxContainer/Value")
			if value_label != null and is_instance_valid(value_label):
				stats[str(key)] = str(value_label.get("text"))
		for child in node.get_children():
			stack.push_back(child)
	return stats


func _element_item_id(element) -> String:
	var item = element.get("item")
	if item == null or not is_instance_valid(item):
		return ""
	return _string_of(item.get("my_id"))


func _string_of(value) -> String:
	if typeof(value) == TYPE_STRING:
		return str(value)
	return ""


func _live(node) -> bool:
	return (
		node != null
		and is_instance_valid(node)
		and node is Node
		and not node.is_queued_for_deletion()
	)
