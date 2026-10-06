extends Reference

# ⚠ 临时探针（票据 04，一次性 spike）的自动导航模块——不属于正式发布代码。
# 由 spike_menu_probe.gd 动态加载；仅配置 navigate=true 时启用（调试辅助取证）。
# 与主探针、spike_menu_probe_dump.gd 一起在票据 05/06 落地时删除。


func navigate(probe, now: int) -> void:
	if probe._phase == "" or probe._phase_root == null or probe._nav_done.has(probe._phase):
		return
	if now < probe._nav_next_ms:
		return
	if probe._phase == "main_menu":
		_nav_press_named(probe, probe._phase_root, "StartButton", "主菜单 StartButton")
	elif probe._phase == "character_select":
		_nav_character(probe)
	elif probe._phase == "weapon_select":
		_nav_weapon(probe)
	elif probe._phase == "difficulty_select":
		_nav_difficulty(probe)
	elif probe._phase == "shop" or probe._phase == "shop_coop":
		_nav_shop(probe)
	elif probe._phase == "level_up":
		_nav_level_up(probe)
	elif probe._phase == "run_end" or probe._phase == "run_end_coop":
		probe._nav_done[probe._phase] = true
		probe._write("NAV %s：终局出现，停止导航（evidence only）" % probe._phase)


func _nav_press_named(probe, root, node_name: String, label: String) -> bool:
	var node = _find_descendant(root, node_name)
	if node == null:
		probe._write("NAV %s：未找到节点 %s" % [label, node_name])
		probe._nav_next_ms = OS.get_ticks_msec() + 2000
		return false
	if not _is_visible(node):
		probe._write("NAV %s：节点不可见，稍后重试" % label)
		probe._nav_next_ms = OS.get_ticks_msec() + 2000
		return false
	_press(node)
	probe._write("NAV %s：已发射 pressed（path=%s）" % [label, str(root.get_path_to(node))])
	probe._nav_done[probe._phase] = true
	return true


func _nav_character(probe) -> void:
	var tree := Engine.get_main_loop() as SceneTree
	var search_root = probe._phase_root
	if tree != null:
		search_root = tree.root
	var elements := _find_item_elements(probe, search_root)
	if elements.empty():
		probe._write("NAV character_select：未找到带 item 的 InventoryElement，稍后重试")
		probe._nav_next_ms = OS.get_ticks_msec() + 2000
		return
	var listing := []
	var target = null
	var fallback = null
	for node in elements:
		var item = node.get("item")
		var cid := _item_id(item)
		listing.append("%s=%s" % [str(node.name), cid])
		if fallback == null:
			fallback = node
		if probe.hero != "" and cid.to_lower().find(probe.hero.to_lower()) >= 0:
			target = node
	if target == null:
		target = fallback
	if _double_press(probe, target):
		return
	probe._write("NAV character_select：元素清单 [%s]" % PoolStringArray(listing).join(", "))
	probe._nav_next_ms = OS.get_ticks_msec() + 1200


func _double_press(probe, node) -> bool:
	if node == null or not is_instance_valid(node):
		return false
	var node_path := str(node.get_path())
	if probe._confirm_path == node_path:
		_press(node)
		probe._write("NAV %s：二次确认 %s（path=%s）" % [
			probe._phase, _item_id(node.get("item")), node_path
		])
		probe._nav_done[probe._phase] = true
		return true
	if not _press_element(node):
		probe._write("NAV %s：元素无 pressed 信号（path=%s）" % [probe._phase, node_path])
		return false
	probe._confirm_path = node_path
	probe._write("NAV %s：首次选择 %s（path=%s），等待二次确认" % [
		probe._phase, _item_id(node.get("item")), node_path
	])
	return false


func _find_item_elements(probe, search_root) -> Array:
	var found := []
	for node in probe._walk(search_root):
		if not _is_visible(node):
			continue
		if not probe._script_path(node).ends_with("inventory_element.gd"):
			continue
		var item = node.get("item")
		if item == null or typeof(item) != TYPE_OBJECT or not is_instance_valid(item):
			continue
		found.append(node)
	return found


func _item_id(value) -> String:
	if typeof(value) == TYPE_STRING:
		return str(value)
	if value != null and typeof(value) == TYPE_OBJECT and is_instance_valid(value):
		for key in ["character_id", "weapon_id", "my_id", "name"]:
			var key_value = value.get(key)
			if typeof(key_value) == TYPE_STRING and str(key_value) != "":
				return str(key_value)
	return ""


func _nav_weapon(probe) -> void:
	var choice = _find_visible_choose_button(probe)
	if choice != null:
		_press(choice)
		var choice_path := str(probe._phase_root.get_path_to(choice))
		probe._write("NAV weapon_select：已按 ChooseButton（path=%s）" % choice_path)
		probe._nav_done[probe._phase] = true
		return
	var inventories = _find_descendant(probe._phase_root, "Inventories")
	if inventories == null:
		inventories = probe._phase_root
	var elements := _find_item_elements(probe, inventories)
	if elements.empty():
		probe._write("NAV weapon_select：未找到 ChooseButton/可选项，稍后重试")
		probe._nav_next_ms = OS.get_ticks_msec() + 2000
		return
	var listing := []
	for node in elements:
		listing.append(_item_id(node.get("item")))
	_double_press(probe, elements[0])
	probe._write("NAV weapon_select：可选项 [%s]" % PoolStringArray(listing).join(", "))
	probe._nav_next_ms = OS.get_ticks_msec() + 1200


func _nav_difficulty(probe) -> void:
	# 实机结论：难度页不是 ZoneUI 滑杆，而是 base_selection 式 InventoryElement
	# （item=difficulty_data，my_id=difficulty_0..6），选中后二次确认即 run_started。
	var target = null
	var listing := []
	for node in _find_item_elements(probe, probe._phase_root):
		var mid := _item_id(node.get("item"))
		listing.append(mid)
		if mid == "difficulty_%d" % probe.target_difficulty:
			target = node
	if target == null:
		probe._write("NAV difficulty_select：未找到 difficulty_%d，清单 [%s]（稍后重试）" % [
			probe.target_difficulty, PoolStringArray(listing).join(", ")
		])
		probe._nav_next_ms = OS.get_ticks_msec() + 2000
		return
	_double_press(probe, target)
	probe._write("NAV difficulty_select：目标 difficulty_%d（path=%s）" % [
		probe.target_difficulty, str(probe._phase_root.get_path_to(target))
	])
	probe._nav_next_ms = OS.get_ticks_msec() + 1200


func _nav_shop(probe) -> void:
	var go = _find_descendant(probe._phase_root, "GoButton")
	if go == null:
		probe._write("NAV shop：未找到 GoButton，稍后重试")
		probe._nav_next_ms = OS.get_ticks_msec() + 2000
		return
	if not _is_visible(go):
		probe._write("NAV shop：GoButton 不可见，稍后重试")
		probe._nav_next_ms = OS.get_ticks_msec() + 2000
		return
	_press(go)
	probe._write("NAV shop：已按 GoButton（path=%s）" % str(probe._phase_root.get_path_to(go)))
	# 不置 nav_done：动画期间可能按不生效，下一轮继续按直到 phase 结束
	probe._nav_next_ms = OS.get_ticks_msec() + 2000


func _nav_level_up(probe) -> void:
	# 可能连续升多级：不置 nav_done，反复选择直到升级 UI 关闭
	var take = _find_descendant(probe._phase_root, "TakeButton")
	if take != null and _is_visible(take):
		_press(take)
		probe._write("NAV level_up：已按 TakeButton（path=%s）" % str(probe._phase_root.get_path_to(take)))
	var card = _find_visible_upgrade_card(probe)
	if card != null:
		var choose = card.get_node_or_null("MarginContainer/VBoxContainer/ChooseButton")
		if choose != null and _is_visible(choose):
			_press(choose)
			var choose_path := str(probe._phase_root.get_path_to(choose))
			probe._write("NAV level_up：已按 UpgradeUI ChooseButton（path=%s）" % choose_path)
			probe._nav_next_ms = OS.get_ticks_msec() + 1500
			return
	probe._write("NAV level_up：未找到可见升级卡，稍后重试")
	probe._nav_next_ms = OS.get_ticks_msec() + 1500


func _find_visible_choose_button(probe):
	var stack := [probe._phase_root]
	while not stack.empty():
		var node = stack.pop_back()
		if str(node.name) == "ChooseButton" and node is Button and _is_visible(node):
			return node
		for child in node.get_children():
			stack.push_back(child)
	return null


func _find_visible_upgrade_card(probe):
	var stack := [probe._phase_root]
	while not stack.empty():
		var node = stack.pop_back()
		if _is_visible(node) and probe._script_path(node).ends_with("upgrade_ui.gd"):
			return node
		for child in node.get_children():
			stack.push_back(child)
	return null


func _press_element(node) -> bool:
	var current = node
	var depth := 0
	while current != null and depth < 3:
		if current.has_signal("pressed"):
			_press(current)
			return true
		current = current.get_parent()
		depth += 1
	return false


func _press(node) -> void:
	if node == null or not is_instance_valid(node):
		return
	node.emit_signal("pressed")


func _find_descendant(root, node_name: String):
	var stack := [root]
	while not stack.empty():
		var node = stack.pop_back()
		if str(node.name) == node_name:
			return node
		for child in node.get_children():
			stack.push_back(child)
	return null


func _is_visible(node) -> bool:
	if node == null or not is_instance_valid(node):
		return false
	if node is CanvasItem:
		return node.is_visible_in_tree()
	return true
