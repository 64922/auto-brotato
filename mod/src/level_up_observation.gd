extends Reference

# 波间升级选卡观测（票据 06）：UpgradesUI 出现时提供与 docs/protocol.md §7.1.2 对齐的
# 只读快照，并向 menu_actions 暴露卡片节点/可用性/选项签名访问器。
#
# 实机结论来源（票据 04）：升级 UI 常驻 /root/Main/UI/UpgradesUI（upgrades_ui.tscn），
# 仅升级时显示；单人容器 UpgradesUIPlayerContainer1（player_index=0），卡片
# UpgradeUI、UpgradeUI2..4 的 UpgradeDescription.item 为 upgrade_data（my_id/tier），
# ChooseButton（my_menu_button.gd）的 disabled/可见性决定 can_pick。
#
# 引擎兼容（ADR-0006）：动态 get()/get_node_or_null()，不依赖 class_name；整数字段经
# engine_compat.int_arg 收敛；场景根按「实机路径 + 场景文件名全树扫描」双路径获取，
# 并以节点名排除 coop 变体（coop_upgrades_ui.tscn）。

const LOG_NAME := "BrotatoPlayer-AutoBrotato:LevelUpObservation"
const RESCAN_INTERVAL_MS := 500
const PLAYER_INDEX := 0
const SCENE := "upgrades_ui.tscn"
const ROOT_PATH := "Main/UI/UpgradesUI"
const ROOT_NAME := "UpgradesUI"

# 升级页内固定路径（实机票据 04；单人 player_index=0 对应 UpgradesUIPlayerContainer1）
const CONTAINER_PATH := "MarginContainer/VBoxContainer/HBoxContainer2"
const CARDS_CONTAINER_PATH := "UpgradesContainer/HBoxContainer"
const CHOOSE_BUTTON_PATH := "MarginContainer/VBoxContainer/ChooseButton"
const DESCRIPTION_PATH := "MarginContainer/VBoxContainer/UpgradeDescription"
const CARD_SLOTS := 4

const Compat := preload("res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/engine_compat.gd")

var _root = null
var _next_scan_ms := 0
var _latest = null


# 按需采样（mod_main 以 ~10Hz 调用）；未处于升级页时 latest() 返回 null。
func sample() -> void:
	var now := OS.get_ticks_msec()
	if not _root_valid(_root):
		_root = null
	if _root == null:
		if now < _next_scan_ms:
			_latest = null
			return
		_next_scan_ms = now + RESCAN_INTERVAL_MS
		_find_root()
	if _root != null:
		_latest = _build_payload()
	else:
		_latest = null


func latest():
	return _latest


# 连接断开/游戏侧重置时调用：丢弃节点缓存，强制重新扫描。
func invalidate() -> void:
	_root = null
	_latest = null
	_next_scan_ms = 0


func in_level_up() -> bool:
	return _root_valid(_root)


# 升级卡节点（index 1..4，与 UI 命名 UpgradeUI/UpgradeUI2..4 对应）。
func card(index: int):
	if index < 1 or index > CARD_SLOTS:
		return null
	var container = _container()
	if container == null:
		return null
	var node = container.get_node_or_null(CARDS_CONTAINER_PATH + "/" + _card_name(index))
	if not _live(node):
		return null
	return node


func choose_button(index: int):
	var card_node = card(index)
	if card_node == null:
		return null
	var button = card_node.get_node_or_null(CHOOSE_BUTTON_PATH)
	if not _live(button):
		return null
	return button


# 卡片当前能否选择：卡片与 ChooseButton 均在屏幕上且按钮未禁用（协议 §7.1.2 can_pick）。
func pickable(index: int) -> bool:
	var card_node = card(index)
	if card_node == null or not card_node.is_visible_in_tree():
		return false
	var button = choose_button(index)
	if button == null or not button.is_visible_in_tree():
		return false
	return button.get("disabled") != true


# 当前升级选项签名（供 menu_actions 选卡后差分验证；无可选项时为 "[]"）。
func signature() -> String:
	return JSON.print(_options())


func _find_root() -> void:
	var tree := Engine.get_main_loop() as SceneTree
	if tree == null:
		return
	_root = _find_scene_root(tree)


# 双路径：先按实机路径直取，再按场景文件名全树扫描；只接受「在屏幕上」的实例
# （UpgradesUI 常驻树中，仅升级时显示），并以节点名排除 coop 变体。
func _find_scene_root(tree: SceneTree):
	var direct = tree.root.get_node_or_null(ROOT_PATH)
	if _root_valid(direct) and _matches(direct):
		return direct
	var stack := [tree.root]
	while not stack.empty():
		var node = stack.pop_back()
		if node is Node:
			if _matches(node) and _root_valid(node):
				return node
			for child in node.get_children():
				stack.push_back(child)
	return null


func _matches(node) -> bool:
	if node == null or not is_instance_valid(node) or not (node is Node):
		return false
	if str(node.name) != ROOT_NAME:
		return false
	return str(node.get_filename()).ends_with(SCENE)


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


func _build_payload() -> Dictionary:
	return {
		"phase": "level_up",
		"player": PLAYER_INDEX,
		"wave": _run_wave(),
		"options": _options(),
	}


func _container():
	if _root == null:
		return null
	var path := CONTAINER_PATH + "/UpgradesUIPlayerContainer%d" % (PLAYER_INDEX + 1)
	var container = _root.get_node_or_null(path)
	if _live(container):
		return container
	# 回退：根脚本变量 _player_container1..4（票据 04 实机记录）
	var direct = _root.get("_player_container%d" % (PLAYER_INDEX + 1))
	if _live(direct):
		return direct
	return null


func _card_name(index: int) -> String:
	if index <= 1:
		return "UpgradeUI"
	return "UpgradeUI%d" % index


func _options() -> Array:
	var options := []
	var index := 1
	while index <= CARD_SLOTS:
		var data = _upgrade_data(card(index))
		var item_id := ""
		var kind := ""
		var tier := 0
		if data != null:
			item_id = _string_of(data.get("my_id"))
			kind = _upgrade_kind(data)
			tier = Compat.int_arg(data.get("tier"))
		options.append(
			{
				"slot": index,
				"kind": kind,
				"id": item_id,
				"tier": tier,
				"can_pick": pickable(index),
			}
		)
		index += 1
	return options


# 双路径读取卡片数据：优先卡片脚本变量 upgrade_data（票据 04 命名），
# 回退 UpgradeDescription.item（实机 dump 证据：upgrade_data.gd 资源）。
func _upgrade_data(card_node):
	if card_node == null:
		return null
	var data = card_node.get("upgrade_data")
	if data != null and typeof(data) == TYPE_OBJECT and is_instance_valid(data):
		return data
	var description = card_node.get_node_or_null(DESCRIPTION_PATH)
	if description == null:
		return null
	data = description.get("item")
	if data != null and typeof(data) == TYPE_OBJECT and is_instance_valid(data):
		return data
	return null


func _upgrade_kind(data) -> String:
	var weapon_id = data.get("weapon_id")
	if typeof(weapon_id) == TYPE_STRING and weapon_id != "":
		return "weapon"
	var script = data.get_script()
	if script != null and str(script.resource_path).ends_with("upgrade_data.gd"):
		return "upgrade"
	return "item"


func _run_wave():
	if RunData == null:
		return null
	return Compat.int_arg(RunData.get("current_wave"))


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
