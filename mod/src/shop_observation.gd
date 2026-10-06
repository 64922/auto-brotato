extends Reference

# 商店观测（票据 06）：商店打开/内容变化时提供与 docs/architecture.md §4.5 对齐的
# 快照（波次/金币/槽位/背包/属性/刷新/离开）。
#
# 纯只读：定位游戏商店场景（Shop 场景根，脚本继承 BaseShop）后，从
# ShopItem/PlayerGearContainer 等节点读取真实 UI 状态。商店场景每波加载/释放，
# 本模块按 RESCAN_INTERVAL_MS 重新定位；未打开时 latest() 返回 null。
#
# 引擎兼容（ADR-0006）：动态节点统一用 get()/call()/has_method()，不依赖 class_name；
# JSON 数字统一为 float，整数字段经 engine_compat.int_arg 收敛。

const LOG_NAME := "BrotatoPlayer-AutoBrotato:ShopObservation"
const RESCAN_INTERVAL_MS := 500
const PLAYER_INDEX := 0

# 协议 stats 字段 → 游戏内 Keys.generate_hash 使用的键（与票据 03 实机验证一致）
const STAT_KEYS := {
	"melee_damage": "melee_damage",
	"ranged_damage": "ranged_damage",
	"attack_speed": "attack_speed",
	"crit_chance": "crit_chance",
	"harvesting": "harvesting",
	"luck": "luck",
	"engineering": "engineering",
	"range": "range",
	"max_hp": "max_hp",
	"hp_regen": "hp_regeneration",
	"lifesteal": "lifesteal",
	"armor": "armor",
	"dodge": "dodge",
	"speed": "speed",
}

const Compat := preload("res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/engine_compat.gd")

var _shop = null
var _container = null
var _gear = null
var _reroll_button = null
var _go_button = null
var _item_popup = null
var _next_scan_ms := 0
var _latest = null


# 按需采样（mod_main 以 ~10Hz 调用；动作执行前也会调用以刷新节点缓存）。
func sample() -> void:
	var now := OS.get_ticks_msec()
	if not _shop_valid():
		# 立即清引用：避免后续访问已释放实例（场景切换期崩溃防护）
		_clear_shop_refs()
		_latest = null
		if now < _next_scan_ms:
			return
		_next_scan_ms = now + RESCAN_INTERVAL_MS
		_find_shop()
		if _shop == null:
			return
	_ensure_nodes()
	_latest = _build_payload()


func latest():
	return _latest


func is_open() -> bool:
	return _shop_valid()


# 连接断开/游戏侧重置时调用：丢弃节点缓存，强制重新扫描。
func invalidate() -> void:
	_clear_shop_refs()
	_latest = null
	_next_scan_ms = 0


func _clear_shop_refs() -> void:
	_shop = null
	_container = null
	_gear = null
	_reroll_button = null
	_go_button = null
	_item_popup = null


func shop_node():
	_ensure_nodes()
	return _shop


func container():
	_ensure_nodes()
	return _container


func item_popup():
	_ensure_nodes()
	return _item_popup


func reroll_button():
	_ensure_nodes()
	return _reroll_button


func go_button():
	_ensure_nodes()
	return _go_button


# 商店槽位节点列表（按 UI 顺序，索引即动作参数 slot）。
func slot_nodes() -> Array:
	_ensure_nodes()
	var nodes := []
	if _container == null:
		return nodes
	for child in _container.get_children():
		if child.has_method("set_shop_item"):
			nodes.append(child)
	return nodes


# 背包装备元素列表（inv_kind=item|weapon，索引即动作参数 index），跳过空元素。
# InventoryContainer 结构：<Container>/ScrollSizeContainer/ScrollContainer/Elements（Inventory）。
func inventory_elements(inv_kind: String) -> Array:
	_ensure_nodes()
	var elements := []
	if _gear == null:
		return elements
	var node_name := "ItemsContainer" if inv_kind == "item" else "WeaponsContainer"
	var node = _gear.get_node_or_null(node_name)
	if node == null:
		return elements
	var inventory = node.get_node_or_null("ScrollSizeContainer/ScrollContainer/Elements")
	if inventory == null:
		return elements
	for child in inventory.get_children():
		if (
			child != null
			and is_instance_valid(child)
			and not child.is_queued_for_deletion()
			and child.get("item") != null
		):
			elements.append(child)
	return elements


func gold() -> int:
	if RunData == null or RunData.get_player_count() <= 0:
		return 0
	var value = RunData.get_player_gold(PLAYER_INDEX)
	var value_type := typeof(value)
	if value_type == TYPE_INT or value_type == TYPE_REAL:
		return int(value)
	return 0


func reroll_cost() -> int:
	if _shop != null and is_instance_valid(_shop):
		# 实机确认：BaseShop._reroll_price 为按玩家索引的数组
		var value = _player_value(_shop.get("_reroll_price"))
		if typeof(value) == TYPE_INT or typeof(value) == TYPE_REAL:
			return int(value)
	if _reroll_button != null and is_instance_valid(_reroll_button):
		var button_value = _reroll_button.get("_value")
		if typeof(button_value) == TYPE_INT or typeof(button_value) == TYPE_REAL:
			return int(button_value)
	return -1


func reroll_count() -> int:
	if _shop == null or not is_instance_valid(_shop):
		return 0
	var count = _player_value(_shop.get("_reroll_count"))
	if typeof(count) != TYPE_INT and typeof(count) != TYPE_REAL:
		count = _player_value(_shop.get("_paid_reroll_count"))
	if typeof(count) == TYPE_INT or typeof(count) == TYPE_REAL:
		return int(count)
	return 0


func free_rerolls() -> int:
	if _shop == null or not is_instance_valid(_shop):
		return 0
	var value = _player_value(_shop.get("_free_rerolls"))
	if typeof(value) == TYPE_INT or typeof(value) == TYPE_REAL:
		return int(value)
	return 0


func _player_value(value):
	if typeof(value) == TYPE_ARRAY and value.size() > PLAYER_INDEX:
		return value[PLAYER_INDEX]
	return value


# 槽位状态签名（用于动作后差分验证与变化检测）。
func offer_signature() -> String:
	if _latest == null:
		return ""
	return JSON.print(_latest.get("slots", []))


func inventory() -> Dictionary:
	if _latest == null:
		return {"weapons": [], "items": []}
	return _latest.get("inventory", {"weapons": [], "items": []})


func _shop_valid() -> bool:
	return _shop != null and is_instance_valid(_shop) and _shop.is_inside_tree()


func _find_shop() -> void:
	_clear_shop_refs()
	var tree := Engine.get_main_loop() as SceneTree
	if tree == null:
		return
	var stack := [tree.root]
	while not stack.empty():
		var node = stack.pop_back()
		if (
			node != null
			and node.has_method("fill_shop_items")
			and node.has_method("on_shop_item_bought")
		):
			_shop = node
			return
		if node is Node:
			for child in node.get_children():
				stack.push_back(child)


func _ensure_nodes() -> void:
	if _shop == null or not is_instance_valid(_shop):
		return
	if _container == null or not is_instance_valid(_container):
		_container = _shop.get_node_or_null("%ShopItemsContainer")
		if _container == null and _shop.has_method("_get_shop_items_container"):
			_container = _shop.call("_get_shop_items_container")
	if _gear == null or not is_instance_valid(_gear):
		_gear = _shop.get_node_or_null("%GearContainer")
		if _gear == null and _shop.has_method("_get_gear_container"):
			_gear = _shop.call("_get_gear_container")
	if _reroll_button == null or not is_instance_valid(_reroll_button):
		_reroll_button = _shop.get_node_or_null("%RerollButton")
		if _reroll_button == null and _shop.has_method("_get_reroll_button"):
			_reroll_button = _shop.call("_get_reroll_button")
	if _go_button == null or not is_instance_valid(_go_button):
		_go_button = _shop.get_node_or_null("%GoButton")
		if _go_button == null and _shop.has_method("_get_go_button"):
			_go_button = _shop.call("_get_go_button")
	if _item_popup == null or not is_instance_valid(_item_popup):
		_item_popup = _shop.get_node_or_null("%ItemPopup")
		if _item_popup == null and _shop.has_method("_get_item_popup"):
			_item_popup = _shop.call("_get_item_popup")


func _build_payload() -> Dictionary:
	var slots := []
	var nodes := slot_nodes()
	var index := 0
	while index < nodes.size():
		slots.append(_slot_payload(index, nodes[index]))
		index += 1
	return {
		"wave_next": _wave_next(),
		"gold": gold(),
		"slots": slots,
		"inventory": _inventory_payload(),
		"stats": _stats_payload(),
		"reroll": {"cost": reroll_cost(), "count": reroll_count()},
		"can_leave": _can_leave(),
	}


func _slot_payload(slot_index: int, node) -> Dictionary:
	var item = node.get("item_data")
	var kind := "item"
	var item_id := ""
	var tier := 0
	if item != null:
		# 武器判定优先看 weapon_id（与票据 03 的背包采集一致），get_category 兜底
		var weapon_id = item.get("weapon_id")
		if typeof(weapon_id) == TYPE_STRING and weapon_id != "":
			kind = "weapon"
		elif item.has_method("get_category") and item.call("get_category") == Category.WEAPON:
			kind = "weapon"
		item_id = str(item.get("my_id"))
		tier = Compat.int_arg(item.get("tier"))
	var locked = node.get("locked")
	if locked == null and item != null:
		locked = item.get("is_locked")
	var price = node.get("value")
	var active = node.get("active")
	return {
		"slot": slot_index,
		"kind": kind,
		"id": item_id,
		"tier": tier,
		"price": Compat.int_arg(price),
		"sold": active == false or item == null,
		"locked": locked == true,
	}


func _inventory_payload() -> Dictionary:
	var weapons := []
	var elements := inventory_elements("weapon")
	var index := 0
	while index < elements.size():
		var item = elements[index].get("item")
		weapons.append(
			{
				"slot": index,
				"id": str(item.get("my_id")),
				"tier": Compat.int_arg(item.get("tier")),
			}
		)
		index += 1
	var items := []
	elements = inventory_elements("item")
	index = 0
	while index < elements.size():
		var item = elements[index].get("item")
		var count := 1
		var raw_count = elements[index].get("current_number")
		if (
			(typeof(raw_count) == TYPE_INT or typeof(raw_count) == TYPE_REAL)
			and int(raw_count) >= 1
		):
			count = int(raw_count)
		items.append({"id": str(item.get("my_id")), "count": count})
		index += 1
	return {"weapons": weapons, "items": items}


func _stats_payload() -> Dictionary:
	var stats := {}
	if RunData == null or RunData.get_player_count() <= 0:
		for field_name in STAT_KEYS:
			stats[field_name] = 0.0
		return stats
	for field_name in STAT_KEYS:
		# 实机验证签名（票据 03）：Utils.get_stat(stat_hash, player_index)
		var value = Utils.get_stat(Keys.generate_hash(STAT_KEYS[field_name]), PLAYER_INDEX)
		if typeof(value) == TYPE_INT or typeof(value) == TYPE_REAL:
			stats[field_name] = value
		else:
			stats[field_name] = 0.0
	return stats


func _wave_next() -> int:
	var wave = RunData.current_wave
	if typeof(wave) == TYPE_INT or typeof(wave) == TYPE_REAL:
		return int(wave) + 1
	return 0


func _can_leave() -> bool:
	if _go_button == null or not is_instance_valid(_go_button):
		return false
	return _go_button.visible and _go_button.get("disabled") != true

