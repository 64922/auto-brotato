extends Reference

# ⚠ 临时探针（票据 04，一次性 spike）的 dump/反射模块——不属于正式发布代码。
# 由 spike_menu_probe.gd 动态加载；与主探针一起在票据 05/06 落地时删除。

const USAGE_SCRIPT_VARIABLE := 4096
# 该引擎构建给脚本变量打的标记是 0x2000（标准 Godot 为 4096），两者都认。
const USAGE_SCRIPT_VARIABLE_ENGINE := 8192

# 关键属性名（全局扫描与对象摘要时优先输出）
const INTERESTING := [
	"character_id", "weapon_id", "my_id", "tier", "type", "panel",
	"zone_id", "difficulty_selected_value", "max_selectable_difficulty",
	"difficulty_unlocked", "max_difficulty_beaten", "max_endless_wave_beaten",
	"selected", "item", "item_data", "locked", "current_number", "value",
	"endless", "ban", "zone_is_random", "zone_selected", "coop", "id", "data",
]


func dump_phase(probe, reason: String) -> void:
	if probe._phase_root == null or not is_instance_valid(probe._phase_root):
		return
	probe._dump_count += 1
	probe._last_dump_ms = OS.get_ticks_msec()
	var lines := []
	lines.append("----- DUMP #%d（%s）phase=%s t=%d.%03d scene=%s root_name=%s -----" % [
		probe._dump_count, reason, probe._phase, OS.get_unix_time(), OS.get_ticks_msec() % 1000,
		str(probe._phase_root.get_filename()), str(probe._phase_root.name)
	])
	var nodes = probe._walk(probe._phase_root)
	lines.append("NODES=%d" % nodes.size())
	for node in nodes:
		lines.append(_node_line(probe, node))
	lines.append("PROPERTIES(script vars of nodes with script):")
	for node in nodes:
		var summary := _script_vars(node)
		if summary != "":
			lines.append("%s | %s" % [str(probe._phase_root.get_path_to(node)), summary])
	lines.append("INTERESTING(global scan):")
	for node in nodes:
		var hit := _interesting_hits(node)
		if hit != "":
			lines.append("%s | %s" % [str(probe._phase_root.get_path_to(node)), hit])
	lines.append("SPECIAL SCAN(carousel/character/weapon/end_run/difficulty scripts):")
	var tree := Engine.get_main_loop() as SceneTree
	var special_nodes = nodes
	if tree != null:
		special_nodes = probe._walk(tree.root)
	for node in special_nodes:
		var script_path = probe._script_path(node)
		var summary := _script_vars(node)
		if summary == "":
			continue
		var lower = script_path.to_lower()
		if (
			lower.find("carousel") >= 0
			or lower.find("character") >= 0
			or lower.find("weapon") >= 0
			or lower.find("end_run") >= 0
			or lower.find("zone_ui") >= 0
			or lower.find("difficulty") >= 0
			or lower.find("menus/menus.gd") >= 0
			or lower.find("title_screen") >= 0
			or lower.find("main_menu") >= 0
			or lower.find("upgrade_ui") >= 0
		):
			lines.append("%s | %s | %s" % [str(probe._phase_root.get_path_to(node)), script_path, summary])
	lines.append("SCENE ROOTS(tree-wide):")
	if tree != null:
		var scene_stack := [tree.root]
		while not scene_stack.empty():
			var scene_node = scene_stack.pop_back()
			if scene_node is Node and is_instance_valid(scene_node):
				var scene_file := str(scene_node.get_filename())
				if scene_file != "":
					var on_screen = probe._is_on_screen(scene_node)
					var rect_text := ""
					if scene_node is Control:
						rect_text = " rect=%s" % str(scene_node.get_global_rect())
					var parent_path := "-"
					if scene_node.get_parent() != null:
						parent_path = str(scene_node.get_parent().get_path())
					lines.append("%s | %s | on_screen=%s | parent=%s%s" % [
						str(tree.root.get_path_to(scene_node)), scene_file,
						str(on_screen), parent_path, rect_text
					])
				for scene_child in scene_node.get_children():
					scene_stack.push_back(scene_child)
	lines.append("CAROUSEL TREE SCAN:")
	if tree != null:
		var carousel_stack := [tree.root]
		while not carousel_stack.empty():
			var carousel_node = carousel_stack.pop_back()
			if carousel_node is Node and is_instance_valid(carousel_node):
				var carousel_script = probe._script_path(carousel_node)
				var carousel_name := str(carousel_node.name)
				if (
					carousel_name.to_lower().find("carousel") >= 0
					or carousel_script.to_lower().find("carousel") >= 0
				):
					lines.append("%s | name=%s | script=%s | %s" % [
						str(tree.root.get_path_to(carousel_node)), carousel_name,
						carousel_script, _script_vars(carousel_node)
					])
					var carousel_children = carousel_node.get_children()
					for carousel_child in carousel_children:
						lines.append("  child %s | class=%s | script=%s | %s" % [
							str(carousel_child.name), str(carousel_child.get_class()),
							probe._script_path(carousel_child), _script_vars(carousel_child)
						])
				for carousel_child in carousel_node.get_children():
					carousel_stack.push_back(carousel_child)
	probe._write(PoolStringArray(lines).join("\n"))


func dump_props(probe, header: String, obj) -> void:
	probe._write("%s | class=%s script=%s" % [header, str(obj.get_class()), probe._script_path(obj)])
	for prop in obj.get_property_list():
		var prop_name := str(prop.get("name", ""))
		var value = obj.get(prop_name)
		probe._write("PROP %s | type=%d usage=%d value=%s" % [
			prop_name, int(prop.get("type", 0)), int(prop.get("usage", 0)), _serialize(value, 0)
		])


func _node_line(probe, node) -> String:
	var path := "."
	if node != probe._phase_root:
		path = str(probe._phase_root.get_path_to(node))
	var script = probe._script_path(node)
	var scene := str(node.get_filename())
	var uniq := ""
	var percent_name := "%" + str(node.name)
	if node.owner == probe._phase_root and probe._phase_root.get_node_or_null(percent_name) == node:
		uniq = " uniq"
	var extra := _control_summary(node)
	var conns := _connection_summary(probe, node)
	var line := "%s | %s%s | script=%s" % [path, str(node.get_class()), uniq, script]
	if scene != "":
		line += " | scene=" + scene
	if extra != "":
		line += " | " + extra
	if conns != "":
		line += " | conns=" + conns
	return line


func _control_summary(node) -> String:
	var parts := []
	if node is Button or str(node.get_class()).find("Button") >= 0:
		parts.append("text=%s" % _serialize(node.get("text"), 0))
		parts.append("disabled=%s" % _serialize(node.get("disabled"), 0))
	if node is Label or str(node.get_class()) == "Label":
		parts.append("text=%s" % _serialize(node.get("text"), 0))
	if node is CanvasItem:
		parts.append("visible=%s" % str(node.visible))
	return PoolStringArray(parts).join(" ")


func _connection_summary(probe, node) -> String:
	var parts := []
	var signals = node.get_signal_list()
	for sig in signals:
		var signal_name := str(sig.get("name", ""))
		var connections = node.get_signal_connection_list(signal_name)
		for conn in connections:
			var target = conn.get("target")
			var target_path := "?"
			if target != null and target is Node and is_instance_valid(target):
				target_path = str(probe._phase_root.get_path_to(target))
			parts.append("%s->%s.%s" % [signal_name, target_path, str(conn.get("method"))])
			if parts.size() >= 24:
				return PoolStringArray(parts).join("|")
	return PoolStringArray(parts).join("|")


func _script_vars(node) -> String:
	var parts := []
	for prop_name in _script_var_names(node):
		var value = node.get(prop_name)
		if typeof(value) == TYPE_NIL:
			continue
		parts.append("%s=%s" % [prop_name, _serialize(value, 0)])
		if parts.size() >= 28:
			parts.append("...")
			break
	return PoolStringArray(parts).join(" ")


func _script_var_names(obj) -> Array:
	var names := []
	if obj == null or not is_instance_valid(obj) or obj.get_script() == null:
		return names
	for prop in obj.get_property_list():
		var usage = int(prop.get("usage", 0))
		if (usage & USAGE_SCRIPT_VARIABLE) != 0 or (usage & USAGE_SCRIPT_VARIABLE_ENGINE) != 0:
			names.append(str(prop.get("name", "")))
	return names


func _interesting_hits(node) -> String:
	var parts := []
	for prop_name in INTERESTING:
		var value = node.get(prop_name)
		if value == null or typeof(value) == TYPE_NIL:
			continue
		parts.append("%s=%s" % [prop_name, _serialize(value, 0)])
		if parts.size() >= 12:
			break
	return PoolStringArray(parts).join(" ")


func _serialize(value, depth: int) -> String:
	var value_type := typeof(value)
	var result := ""
	if value_type == TYPE_NIL:
		result = "null"
	elif value_type == TYPE_BOOL:
		result = "true" if value else "false"
	elif value_type == TYPE_INT or value_type == TYPE_REAL:
		result = str(value)
	elif value_type == TYPE_STRING:
		var text := str(value)
		if text.length() > 120:
			text = text.substr(0, 120) + "…"
		result = "\"" + text + "\""
	elif (
		value_type == TYPE_VECTOR2
		or value_type == TYPE_VECTOR3
		or value_type == TYPE_RECT2
		or value_type == TYPE_COLOR
	):
		result = str(value)
	elif value_type == TYPE_ARRAY:
		if depth >= 2:
			result = "[array:%d]" % value.size()
		else:
			var items := []
			var count := 0
			while count < value.size() and count < 16:
				items.append(_serialize(value[count], depth + 1))
				count += 1
			if value.size() > 16:
				items.append("...")
			result = "[" + PoolStringArray(items).join(", ") + "]"
	elif value_type == TYPE_DICTIONARY:
		if depth >= 2:
			result = "{dict:%d}" % value.size()
		else:
			var pairs := []
			var keys = value.keys()
			var key_index := 0
			while key_index < keys.size() and key_index < 20:
				var key = keys[key_index]
				pairs.append("%s:%s" % [
					_serialize(key, depth + 1), _serialize(value[key], depth + 1)
				])
				key_index += 1
			if keys.size() > 20:
				pairs.append("...")
			result = "{" + PoolStringArray(pairs).join(", ") + "}"
	elif value_type == TYPE_OBJECT:
		result = _object_summary(value, depth)
	else:
		result = "type_%d" % value_type
	return result


func _object_summary(obj, depth: int) -> String:
	if obj == null or not is_instance_valid(obj):
		return "<freed>"
	if obj is Node:
		if obj.is_inside_tree():
			return "Node<%s>(%s)" % [str(obj.get_class()), str(obj.get_path())]
		return "Node<%s>(<detached>)" % str(obj.get_class())
	var script_path := "-"
	var script = obj.get_script()
	if script != null:
		script_path = str(script.resource_path)
	if depth >= 1:
		return "<%s script=%s>" % [str(obj.get_class()), script_path]
	var parts := []
	for prop_name in _script_var_names(obj):
		var value = obj.get(prop_name)
		if typeof(value) == TYPE_NIL:
			continue
		parts.append("%s=%s" % [prop_name, _serialize(value, depth + 1)])
		if parts.size() >= 20:
			parts.append("...")
			break
	return "<%s script=%s {%s}>" % [
		str(obj.get_class()), script_path, PoolStringArray(parts).join(" ")
	]
