extends Reference

# 菜单动作执行（票据 06）：menu_set_difficulty / menu_start_run / menu_pick_upgrade。
#
# - 经游戏正常 UI 流程执行（ADR-0007）：设置难度走游戏同款 grab_focus（触发
#   focus_entered/element_focused 信号链），开局与选卡发射与 UI 按钮等价的 pressed
#   信号触发游戏自身逻辑，不直接改难度/场景/升级数据。
# - 语义与 shop_actions.gd 对齐：幂等账本（最近 256 条 ref 只重发 ack）、串行队列
#   （在途时新动作入队，超出 16 回 busy）、超时回执、断连时在途动作记 link_lost、
#   排队动作丢弃。
# - 完成判定：设置难度 = 移动焦点（grab_focus，触发游戏自身 focus 路径）后按
#   _latest_focused_element 读回校验（目标已选中=空操作）；开始对局 = 按下当前焦点
#   元素（游戏 difficulty_selection.gd `_on_element_pressed` 用被按元素的值立即
#   change_scene 开局），以难度页离场且 main.tscn（/root/Main）就位判成功；
#   选卡按升级页 options 差分（卡片被消耗/页面关闭）。
# - 其余 menu_*（含 menu_pause）不在 KINDS 中 → mod_main 统一 unsupported_kind。

const LOG_NAME := "BrotatoPlayer-AutoBrotato:MenuActions"
const DIFFICULTY_TIMEOUT_MS := 700
const START_TIMEOUT_MS := 1500
const PICK_TIMEOUT_MS := 1500
const LEDGER_LIMIT := 256
const QUEUE_LIMIT := 16

const Compat := preload("res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/engine_compat.gd")

const KINDS := ["menu_set_difficulty", "menu_start_run", "menu_pick_upgrade"]

var _menu = null
var _level_up = null
var _ledger := {}
var _ledger_order := []
var _pending := []
var _queue := []


func _init(menu_observer, level_up_observer) -> void:
	_menu = menu_observer
	_level_up = level_up_observer


func handles(kind: String) -> bool:
	return kind in KINDS


# 接收动作。返回 {"state": "acked"|"queued"|"duplicate", "ack": {...}} 或
# {"state": "pending", "ack": {}}（排队/在途结果经 poll() 取出）。
func execute(kind: String, payload: Dictionary, ref, now_ms: int) -> Dictionary:
	if ref != null and _ledger.has(ref):
		return {"state": "duplicate", "ack": _ledger[ref]}
	if _has_request(ref):
		return {"state": "pending", "ack": {}}
	_menu.sample()
	_level_up.sample()
	if _busy():
		if _queue.size() >= QUEUE_LIMIT:
			return _acked(ref, {"ok": false, "error": "busy"})
		_queue.append({"kind": kind, "payload": payload, "ref": ref})
		return {"state": "queued", "ack": {}}
	return _run(kind, payload, ref, now_ms)


# 每帧调用：校验在途动作并串行推进队列，返回 [{ref, ack}]。
func poll(now_ms: int) -> Array:
	var done := []
	var index := 0
	while index < _pending.size():
		var op = _pending[index]
		if op.result == null:
			_check_progress(op)
		if op.result == null and now_ms >= op.deadline:
			op.result = {"ok": false, "error": _timeout_error(op.kind)}
		if op.result != null:
			done.append({"ref": op.ref, "ack": _finish(op.ref, op.result)})
			_pending.remove(index)
		else:
			index += 1
	while _pending.empty() and not _queue.empty():
		var item = _queue.pop_front()
		var outcome = _run(item.kind, item.payload, item.ref, now_ms)
		if outcome["state"] == "acked":
			done.append({"ref": item.ref, "ack": outcome["ack"]})
	return done


# 连接断开时调用：在途动作记账为 link_lost（不再执行），排队动作丢弃（未执行、无副作用）；
# 账本保留以吸收重连后的旧 ref 重发。
func reset() -> void:
	for op in _pending:
		if op.ref != null and not _ledger.has(op.ref):
			_store_ledger(op.ref, {"ok": false, "error": "link_lost"})
	_pending = []
	_queue = []


func _run(kind: String, payload: Dictionary, ref, now_ms: int) -> Dictionary:
	match kind:
		"menu_set_difficulty":
			return _execute_set_difficulty(payload, ref, now_ms)
		"menu_start_run":
			return _execute_start_run(ref, now_ms)
		"menu_pick_upgrade":
			return _execute_pick_upgrade(payload, ref, now_ms)
	return _acked(ref, {"ok": false, "error": "unsupported_kind:%s" % kind})


func _execute_set_difficulty(payload: Dictionary, ref, now_ms: int) -> Dictionary:
	if not _menu.in_difficulty():
		return _acked(ref, {"ok": false, "error": "not_in_difficulty_select"})
	var value := Compat.int_arg(payload.get("value"))
	var element = _menu.difficulty_element(value)
	if element == null:
		return _acked(ref, {"ok": false, "error": "bad_difficulty"})
	var item = element.get("item")
	if item == null or item.get("is_locked") == true:
		return _acked(ref, {"ok": false, "error": "bad_difficulty"})
	var target_id := "difficulty_%d" % value
	if _menu.selected_difficulty_id() == target_id:
		# 目标已选中：空操作（元件 pressed 会让游戏立即开局，属 menu_start_run）
		return _acked(ref, {"ok": true})
	_pending.append(
		{
			"ref": ref,
			"kind": "menu_set_difficulty",
			"deadline": now_ms + DIFFICULTY_TIMEOUT_MS,
			"result": null,
			"target_id": target_id,
		}
	)
	# 只移动焦点（游戏 focus_element_index 同款 grab_focus），不触发 pressed：
	# difficulty_selection.gd 里任何非 special 元素的 pressed 都会直接 change_scene 开局。
	element.grab_focus()
	return {"state": "pending", "ack": {}}


func _execute_start_run(ref, now_ms: int) -> Dictionary:
	if not _menu.in_difficulty():
		return _acked(ref, {"ok": false, "error": "not_in_difficulty_select"})
	if not _menu.can_start():
		return _acked(ref, {"ok": false, "error": "cannot_start"})
	var element = _menu.focused_element()
	if element == null:
		return _acked(ref, {"ok": false, "error": "cannot_start"})
	_pending.append(
		{
			"ref": ref,
			"kind": "menu_start_run",
			"deadline": now_ms + START_TIMEOUT_MS,
			"result": null,
		}
	)
	element.emit_signal("pressed")
	return {"state": "pending", "ack": {}}


func _execute_pick_upgrade(payload: Dictionary, ref, now_ms: int) -> Dictionary:
	if not _level_up.in_level_up():
		return _acked(ref, {"ok": false, "error": "not_in_level_up"})
	var index := Compat.int_arg(payload.get("index"))
	if index < 1 or not _level_up.pickable(index):
		return _acked(ref, {"ok": false, "error": "bad_index"})
	var button = _level_up.choose_button(index)
	if button == null:
		return _acked(ref, {"ok": false, "error": "bad_index"})
	_pending.append(
		{
			"ref": ref,
			"kind": "menu_pick_upgrade",
			"deadline": now_ms + PICK_TIMEOUT_MS,
			"result": null,
			"index": index,
			"signature_before": _level_up.signature(),
		}
	)
	button.emit_signal("pressed")
	return {"state": "pending", "ack": {}}


func _check_progress(op) -> void:
	match op.kind:
		"menu_set_difficulty":
			if _menu.selected_difficulty_id() == op.target_id:
				op.result = {"ok": true}
		"menu_start_run":
			# 开局 = 难度页离场且对局场景（main.tscn 的 /root/Main）已就位；
			# 经 BackButton 返回时切回的是选武器/选人页，不会出现 Main。
			if not _menu.in_difficulty() and _menu.in_run_scene():
				op.result = {"ok": true}
		"menu_pick_upgrade":
			if (
				not _level_up.in_level_up()
				or _level_up.signature() != op.signature_before
			):
				op.result = {"ok": true}


func _busy() -> bool:
	return not _pending.empty() or not _queue.empty()


func _has_request(ref) -> bool:
	if ref == null:
		return false
	for op in _pending:
		if op.ref == ref:
			return true
	for item in _queue:
		if item.ref == ref:
			return true
	return false


func _timeout_error(kind: String) -> String:
	match kind:
		"menu_set_difficulty":
			return "difficulty_timeout"
		"menu_start_run":
			return "start_timeout"
		_:
			return "pick_timeout"


func _acked(ref, ack: Dictionary) -> Dictionary:
	return {"state": "acked", "ack": _finish(ref, ack)}


func _finish(ref, ack: Dictionary) -> Dictionary:
	if ref != null and not _ledger.has(ref):
		_store_ledger(ref, ack)
	return ack


func _store_ledger(ref, ack: Dictionary) -> void:
	_ledger[ref] = ack
	_ledger_order.append(ref)
	while _ledger_order.size() > LEDGER_LIMIT:
		var oldest = _ledger_order.pop_front()
		_ledger.erase(oldest)
