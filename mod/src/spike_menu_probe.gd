extends Reference

# ⚠ 临时探针（票据 04，一次性 spike）——不属于正式发布代码。
#
# 目的：在难度选择页 / 升级选卡页 / 终局页出现时，把场景根、节点树、脚本成员、
# 按钮信号连接与关键属性 dump 到 user://auto_brotato_spike.log（并少量写 mod 日志），
# 为票据 05/06 的正式 menu_observation/menu_actions 提供实机结论。
#
# 启用方式（默认完全惰性：文件不存在即不扫描、不导航、不写任何文件）：
#   %APPDATA%\Brotato\auto_brotato_spike.json
#   {"enabled": true, "navigate": false, "hero": "ranger", "target_difficulty": 0}
# navigate=true 时用 UI 信号自动走流程（主菜单→选英雄→选武器→难度页→开局→升级页），
# 属调试辅助手段，仅用于本 spike 取证。
#
# 清理时机：票据 05/06 落地正式实现时，删除本文件、spike_menu_probe_dump.gd
# 及 mod_main.gd 中的挂接代码。

const LOG_NAME := "BrotatoPlayer-AutoBrotato:SpikeProbe"
const CONFIG_PATH := "user://auto_brotato_spike.json"
const CMD_PATH := "user://auto_brotato_spike_cmd.json"
const DUMP_PATH := "user://auto_brotato_spike.log"
const DUMP_MODULE_PATH := (
	"res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/spike_menu_probe_dump.gd"
)
const NAV_MODULE_PATH := (
	"res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/spike_menu_probe_nav.gd"
)
const POLL_INTERVAL_MS := 250
const CONTENT_DUMP_INTERVAL_MS := 2000
const MAX_DUMPS_PER_PHASE := 6
const MAX_NODES := 6000

# scene 文件名后缀 → phase 名（按此顺序做优先级匹配）
const PHASE_SCENES := [
	["difficulty_selection.tscn", "difficulty_select"],
	["upgrades_ui.tscn", "level_up"],
	["end_run.tscn", "run_end"],
	["coop_end_run.tscn", "run_end_coop"],
	["character_selection.tscn", "character_select"],
	["weapon_selection.tscn", "weapon_select"],
	["shop.tscn", "shop"],
	["coop_shop.tscn", "shop_coop"],
	["main_menu.tscn", "main_menu"],
]

var enabled := false
var navigate := false
var hero := "ranger"
var target_difficulty := 0

var _config_checked := false
var _next_poll_ms := 0
var _phase := ""
var _phase_root = null
var _last_dump_ms := 0
var _dump_count := 0
var _nav_done := {}
var _nav_next_ms := 0
var _confirm_path := ""
var _last_cmd_check_ms := 0
var _session := ""
var _dumper = null
var _nav = null


func poll() -> void:
	if not _config_checked:
		_config_checked = true
		_load_config()
	if not enabled:
		return
	var now := OS.get_ticks_msec()
	if now < _next_poll_ms:
		return
	_next_poll_ms = now + POLL_INTERVAL_MS
	_scan(now)
	_process_command(now)


func _process_command(now: int) -> void:
	if now - _last_cmd_check_ms < 1000:
		return
	_last_cmd_check_ms = now
	var file := File.new()
	if not file.file_exists(CMD_PATH):
		return
	if file.open(CMD_PATH, File.READ) != OK:
		return
	var text := file.get_as_text()
	file.close()
	var writer := File.new()
	if writer.open(CMD_PATH, File.WRITE) == OK:
		writer.store_string("{}")
		writer.close()
	if text.strip_edges() == "" or text.strip_edges() == "{}":
		return
	var parsed = JSON.parse(text)
	if parsed.error != OK or typeof(parsed.result) != TYPE_DICTIONARY:
		_write("CMD 解析失败: %s" % text)
		return
	var cmd: Dictionary = parsed.result
	_run_command(cmd, now)


func _run_command(cmd: Dictionary, now: int) -> void:
	if cmd.get("dump") == true:
		_write("CMD dump 请求")
		if _phase == "":
			_write("CMD dump 无当前 phase")
		else:
			_dump_now("手动")
	if cmd.get("navigate") == true:
		navigate = true
		_nav_done = {}
		_nav_next_ms = now
		_write("CMD navigate 重新武装并启用（phase=%s）" % _phase)
	var tree := Engine.get_main_loop() as SceneTree
	if tree == null:
		return
	if cmd.has("press"):
		var path := str(cmd.get("press"))
		var node = tree.root.get_node_or_null(path)
		if node == null:
			_write("CMD press 失败（找不到）: %s" % path)
		elif node.has_signal("pressed"):
			node.emit_signal("pressed")
			_write("CMD press 成功: %s" % path)
		else:
			_write("CMD press 失败（无 pressed 信号）: %s" % path)
	if cmd.has("emit"):
		var emit_path := str(cmd.get("emit"))
		var signal_name := str(cmd.get("signal", ""))
		var emit_node = tree.root.get_node_or_null(emit_path)
		if emit_node == null:
			_write("CMD emit 失败（找不到）: %s" % emit_path)
		elif signal_name == "" or not emit_node.has_signal(signal_name):
			_write("CMD emit 失败（无信号 %s）: %s" % [signal_name, emit_path])
		else:
			emit_node.emit_signal(signal_name)
			_write("CMD emit 成功: %s.%s" % [emit_path, signal_name])
	if cmd.has("props"):
		var props_path := str(cmd.get("props"))
		var props_node = tree.root.get_node_or_null(props_path)
		if props_node == null:
			_write("CMD props 失败（找不到）: %s" % props_path)
		else:
			_props_now("CMD props %s" % props_path, props_node)
			var props_item = props_node.get("item")
			if props_item != null and typeof(props_item) == TYPE_OBJECT and is_instance_valid(props_item):
				_props_now("CMD props item of %s" % props_path, props_item)


func _props_now(header: String, obj) -> void:
	var dumper = _get_dumper()
	if dumper != null:
		dumper.dump_props(self, header, obj)


func _load_config() -> void:
	if _session == "":
		_session = "%d" % OS.get_ticks_msec()
	var file := File.new()
	if not file.file_exists(CONFIG_PATH):
		return
	if file.open(CONFIG_PATH, File.READ) != OK:
		return
	var parsed = JSON.parse(file.get_as_text())
	file.close()
	if parsed.error != OK or typeof(parsed.result) != TYPE_DICTIONARY:
		ModLoaderLog.warning("spike 配置无法解析，探针保持关闭", LOG_NAME)
		return
	var config: Dictionary = parsed.result
	enabled = config.get("enabled") == true
	navigate = config.get("navigate") == true
	hero = str(config.get("hero", hero))
	target_difficulty = int(config.get("target_difficulty", target_difficulty))
	if enabled:
		_write("===== SPIKE SESSION %s 启动：navigate=%s hero=%s target_difficulty=%d =====" % [
			_session, str(navigate), hero, target_difficulty
		])
		ModLoaderLog.info(
			"spike 探针启用（session=%s navigate=%s target_difficulty=%d）"
			% [_session, str(navigate), target_difficulty],
			LOG_NAME
		)


func _scan(now: int) -> void:
	var tree := Engine.get_main_loop() as SceneTree
	if tree == null:
		return
	# BFS「最后出现的场景根获胜」：菜单页系统按创建顺序追加兄弟节点，
	# 后创建（更近一次导航进入）的页面优先于滞留树中的旧页面。
	var found_root = null
	var found_phase := ""
	var queue := [tree.root]
	while not queue.empty():
		var node = queue.pop_front()
		if node is Node:
			var filename := str(node.get_filename())
			if filename != "":
				for entry in PHASE_SCENES:
					if filename.ends_with(entry[0]) and _is_on_screen(node):
						found_root = node
						found_phase = entry[1]
			for child in node.get_children():
				queue.push_back(child)
	if found_root != null:
		if found_root != _phase_root or found_phase != _phase:
			if _phase_root != null:
				_end_phase()
			_begin_phase(found_root, found_phase, now)
		elif now - _last_dump_ms >= CONTENT_DUMP_INTERVAL_MS and _dump_count < MAX_DUMPS_PER_PHASE:
			_dump_now("周期")
		if navigate:
			_navigate_now(now)
	elif _phase_root != null and (
		not is_instance_valid(_phase_root) or not _phase_root.is_inside_tree()
	):
		_end_phase()


func _begin_phase(root, phase: String, now: int) -> void:
	_phase = phase
	_phase_root = root
	_dump_count = 0
	_nav_done.erase(phase)
	_confirm_path = ""
	_nav_next_ms = now + 1500
	_write("")
	_write("========== PHASE BEGIN %s t=%d.%03d ==========" % [
		phase, OS.get_unix_time(), OS.get_ticks_msec() % 1000
	])
	ModLoaderLog.info("spike 检测到 phase=%s scene=%s" % [phase, str(root.get_filename())], LOG_NAME)
	_dump_now("出现")


func _end_phase() -> void:
	_write("========== PHASE END %s t=%d.%03d ==========" % [
		_phase, OS.get_unix_time(), OS.get_ticks_msec() % 1000
	])
	ModLoaderLog.info("spike phase 结束=%s" % _phase, LOG_NAME)
	_phase = ""
	_phase_root = null


func _dump_now(reason: String) -> void:
	var dumper = _get_dumper()
	if dumper != null:
		dumper.dump_phase(self, reason)


func _get_dumper():
	if _dumper == null and ResourceLoader.exists(DUMP_MODULE_PATH):
		var dump_script = load(DUMP_MODULE_PATH)
		if dump_script != null:
			_dumper = dump_script.new()
	return _dumper


func _walk(root) -> Array:
	var out := []
	var stack := [root]
	while not stack.empty() and out.size() < MAX_NODES:
		var node = stack.pop_back()
		out.append(node)
		var children = node.get_children()
		var index = children.size() - 1
		while index >= 0:
			stack.append(children[index])
			index -= 1
	return out


func _script_path(node) -> String:
	var script = node.get_script()
	if script == null:
		return "-"
	var path := str(script.resource_path)
	if path == "":
		return "<script>"
	return path


func _navigate_now(now: int) -> void:
	var nav = _get_nav()
	if nav != null:
		nav.navigate(self, now)


func _get_nav():
	if _nav == null and ResourceLoader.exists(NAV_MODULE_PATH):
		var nav_script = load(NAV_MODULE_PATH)
		if nav_script != null:
			_nav = nav_script.new()
	return _nav


# 页面「真正在屏幕上」：可见 + （Control 时）全局矩形与视口相交，
# 排除被菜单系统移出屏幕但仍 is_visible_in_tree()==true 的滞留页面。
func _is_on_screen(node) -> bool:
	if node == null or not is_instance_valid(node) or not node.is_inside_tree():
		return false
	if node is CanvasItem and not node.is_visible_in_tree():
		return false
	if node is Control:
		var viewport = node.get_viewport()
		if viewport != null:
			return node.get_global_rect().intersects(viewport.get_visible_rect())
	return true


func _write(text: String) -> void:
	var file := File.new()
	var err := file.open(DUMP_PATH, File.READ_WRITE)
	if err != OK:
		err = file.open(DUMP_PATH, File.WRITE)
		if err != OK:
			return
	file.seek_end()
	file.store_line(text)
	file.close()
