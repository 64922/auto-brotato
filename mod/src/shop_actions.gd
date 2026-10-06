extends Reference

# 商店动作执行（票据 06）：shop_buy / shop_sell / shop_reroll / shop_lock / shop_leave。
#
# - 经游戏正常 UI 流程执行（ADR-0007）：发射与 UI 按钮等价的信号
#   （BuyButton.pressed / ItemPopup.item_discard_button_pressed / RerollButton.pressed /
#   LockButton.toggled / GoButton.pressed）触发游戏自身逻辑，不直接改金币/背包/数值
#   （架构不变量 1）。
# - 商业动作顺序执行、不并发（architecture §4.7）：已有动作在途/排队时，新动作入队，
#   前一个结算后再执行；队列上限 QUEUE_LIMIT，超出拒绝 busy。
# - 执行前在 mod 侧校验约束；无效动作 ack {ok:false, error} 且无副作用。
# - 幂等：已完成动作按 ref 缓存最近 LEDGER_LIMIT 条 ack，重复 ref 只重发 ack
#   不重执行；在途/排队中的同 ref 忽略重复触发；连接断开时在途动作按 link_lost
#   记账，重连后旧 ref 不会再次执行。
# - 购买等待游戏信号（shop_item_bought / shop_item_insufficient_currency）后回执，
#   并在成功时向上层给出 purchase_done 事件；卖出与刷新按商店状态差分验证；
#   超时记失败（Python 侧 2s 未回执也另计失败，architecture §4.7）。

const LOG_NAME := "BrotatoPlayer-AutoBrotato:ShopActions"
const BUY_TIMEOUT_MS := 1000
const SELL_TIMEOUT_MS := 1500
const REROLL_TIMEOUT_MS := 700
const LEDGER_LIMIT := 256
const QUEUE_LIMIT := 16

const Compat := preload("res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/engine_compat.gd")

const KINDS := ["shop_buy", "shop_sell", "shop_reroll", "shop_lock", "shop_leave"]

var _observer = null
var _ledger := {}
var _ledger_order := []
var _pending := []
var _queue := []
var _bound_container = null


func _init(observer) -> void:
	_observer = observer


func handles(kind: String) -> bool:
	return kind in KINDS


# 接收动作。返回 {"state": "acked"|"queued"|"duplicate", "ack": {...}} 或
# {"state": "pending", "ack": {}}（排队/在途结果经 poll() 取出）。
func execute(kind: String, payload: Dictionary, ref, now_ms: int) -> Dictionary:
	if ref != null and _ledger.has(ref):
		return {"state": "duplicate", "ack": _ledger[ref]}
	if _has_request(ref):
		return {"state": "pending", "ack": {}}
	_observer.sample()
	if not _observer.is_open():
		return _acked(ref, {"ok": false, "error": "not_in_shop"})
	_bind()
	if _busy():
		if _queue.size() >= QUEUE_LIMIT:
			return _acked(ref, {"ok": false, "error": "busy"})
		_queue.append({"kind": kind, "payload": payload, "ref": ref})
		return {"state": "queued", "ack": {}}
	return _run(kind, payload, ref, now_ms)


# 每帧调用：结算在途动作并串行推进队列，返回 [{ref, ack, event?}]。
func poll(now_ms: int) -> Array:
	var done := []
	var index := 0
	while index < _pending.size():
		var op = _pending[index]
		if op.result == null:
			if op.kind == "shop_sell":
				_check_sell(op)
			elif op.kind == "shop_reroll":
				_check_reroll(op)
		if op.result == null and now_ms >= op.deadline:
			op.result = {"ok": false, "error": _timeout_error(op.kind)}
		if op.result != null:
			done.append(_completion(op))
			_pending.remove(index)
		else:
			index += 1
	while _pending.empty() and not _queue.empty():
		var item = _queue.pop_front()
		var outcome = _run(item.kind, item.payload, item.ref, now_ms)
		if outcome["state"] == "acked":
			done.append({"ref": item.ref, "ack": outcome["ack"]})
	return done


# 连接断开时调用：在途动作不再执行并记账为 link_lost，排队动作丢弃（未执行、无副作用），
# 节点绑定失效；账本保留以吸收重连后的旧 ref 重发。
func reset() -> void:
	for op in _pending:
		if op.ref != null and not _ledger.has(op.ref):
			_store_ledger(op.ref, {"ok": false, "error": "link_lost"})
	_pending = []
	_queue = []
	_bound_container = null


func _run(kind: String, payload: Dictionary, ref, now_ms: int) -> Dictionary:
	match kind:
		"shop_buy":
			return _execute_buy(payload, ref, now_ms)
		"shop_sell":
			return _execute_sell(payload, ref, now_ms)
		"shop_reroll":
			return _execute_reroll(ref, now_ms)
		"shop_lock":
			return _execute_lock(payload, ref)
		"shop_leave":
			return _execute_leave(ref)
	return _acked(ref, {"ok": false, "error": "unsupported_kind:%s" % kind})


func _execute_buy(payload: Dictionary, ref, now_ms: int) -> Dictionary:
	var slot := Compat.int_arg(payload.get("slot"))
	var nodes = _observer.slot_nodes()
	if slot < 0 or slot >= nodes.size():
		return _acked(ref, {"ok": false, "error": "bad_slot"})
	var node = nodes[slot]
	var item = node.get("item_data")
	if item == null or node.get("active") == false:
		return _acked(ref, {"ok": false, "error": "slot_sold"})
	var price := Compat.int_arg(node.get("value"))
	var gold: int = _observer.gold()
	if price < 0:
		return _acked(ref, {"ok": false, "error": "bad_price"})
	if gold < price:
		return _acked(ref, {"ok": false, "error": "insufficient_gold"})
	var button = node.get_node_or_null("%BuyButton")
	if button == null:
		button = node.get_node_or_null("PanelContainer/MarginContainer/VBoxContainer/BuyButton")
	if button == null:
		return _acked(ref, {"ok": false, "error": "buy_button_missing"})
	_pending.append(
		{
			"ref": ref,
			"kind": "shop_buy",
			"deadline": now_ms + BUY_TIMEOUT_MS,
			"result": null,
			"node": node,
			"offer": item,
			"slot": slot,
			"item_id": str(item.get("my_id")),
			"price": price,
		}
	)
	button.emit_signal("pressed")
	return {"state": "pending", "ack": {}}


# 卖出=商店内回收装备。游戏规则（实机反编译 BaseShop/ItemPopup 确认）：商店仅允许回收
# 武器（ItemPopup.should_show_buttons 要求 WeaponData），道具固定返回 not_discardable。
# 等价路径：ItemPopup.item_discard_button_pressed(weapon_data)——BaseShop._ready 已把该
# 信号绑定到 _on_item_discard_button_pressed(player_index=0)，由游戏自身完成
# 移除武器、加金币、刷新商店与 UI 的全部流程。
func _execute_sell(payload: Dictionary, ref, now_ms: int) -> Dictionary:
	var inv_kind := str(payload.get("inv_kind", ""))
	if inv_kind != "item" and inv_kind != "weapon":
		return _acked(ref, {"ok": false, "error": "bad_inv_kind"})
	if inv_kind != "weapon":
		return _acked(ref, {"ok": false, "error": "not_discardable"})
	var index := Compat.int_arg(payload.get("index"))
	var elements = _observer.inventory_elements("weapon")
	if index < 0 or index >= elements.size():
		return _acked(ref, {"ok": false, "error": "bad_index"})
	var weapon = elements[index].get("item")
	if weapon == null:
		return _acked(ref, {"ok": false, "error": "bad_index"})
	var popup = _observer.item_popup()
	if popup == null or not is_instance_valid(popup):
		return _acked(ref, {"ok": false, "error": "popup_missing"})
	_pending.append(
		{
			"ref": ref,
			"kind": "shop_sell",
			"deadline": now_ms + SELL_TIMEOUT_MS,
			"result": null,
			"gold_before": _observer.gold(),
			"inventory_before": JSON.print(_observer.inventory()),
		}
	)
	popup.emit_signal("item_discard_button_pressed", weapon)
	return {"state": "pending", "ack": {}}


func _execute_reroll(ref, now_ms: int) -> Dictionary:
	var cost = _observer.reroll_cost()
	var gold: int = _observer.gold()
	if _observer.free_rerolls() <= 0 and (cost < 0 or gold < cost):
		return _acked(ref, {"ok": false, "error": "insufficient_gold"})
	var button = _observer.reroll_button()
	if button == null:
		return _acked(ref, {"ok": false, "error": "reroll_button_missing"})
	var op := {
		"ref": ref,
		"kind": "shop_reroll",
		"deadline": now_ms + REROLL_TIMEOUT_MS,
		"result": null,
		"gold_before": gold,
		"signature_before": _observer.offer_signature(),
		"count_before": _observer.reroll_count(),
	}
	_pending.append(op)
	button.emit_signal("pressed")
	_check_reroll(op)
	if op.result != null:
		_pending.erase(op)
		return _acked(ref, op.result)
	return {"state": "pending", "ack": {}}


func _execute_lock(payload: Dictionary, ref) -> Dictionary:
	var slot := Compat.int_arg(payload.get("slot"))
	var locked = payload.get("locked")
	if typeof(locked) != TYPE_BOOL:
		return _acked(ref, {"ok": false, "error": "bad_locked"})
	var nodes = _observer.slot_nodes()
	if slot < 0 or slot >= nodes.size():
		return _acked(ref, {"ok": false, "error": "bad_slot"})
	var node = nodes[slot]
	var item = node.get("item_data")
	if item == null or node.get("active") == false:
		return _acked(ref, {"ok": false, "error": "slot_sold"})
	if item.get("is_lockable") == false:
		return _acked(ref, {"ok": false, "error": "not_lockable"})
	if _lock_state(node, item) == locked:
		return _acked(ref, {"ok": true})
	var button = node.get_node_or_null("%LockButton")
	if button == null:
		return _acked(ref, {"ok": false, "error": "lock_button_missing"})
	button.emit_signal("toggled", locked)
	if _lock_state(node, item) != locked:
		return _acked(ref, {"ok": false, "error": "lock_failed"})
	return _acked(ref, {"ok": true})


func _execute_leave(ref) -> Dictionary:
	var button = _observer.go_button()
	if button == null or not button.visible or button.get("disabled") == true:
		return _acked(ref, {"ok": false, "error": "cannot_leave"})
	button.emit_signal("pressed")
	return _acked(ref, {"ok": true})


func _lock_state(node, item) -> bool:
	# ShopItem.locked 与 ItemParentData.is_locked 由游戏同步；二者取或兼容版本差异。
	if node.get("locked") == true or item.get("is_locked") == true:
		return true
	return false


func _check_sell(op) -> void:
	var inventory_now := JSON.print(_observer.inventory())
	if _observer.gold() > op.gold_before or inventory_now != op.inventory_before:
		op.result = {"ok": true}


func _check_reroll(op) -> void:
	if (
		_observer.offer_signature() != op.signature_before
		or _observer.gold() != op.gold_before
		or _observer.reroll_count() != op.count_before
	):
		op.result = {"ok": true}


func _bind() -> void:
	var container = _observer.container()
	if container == null or container == _bound_container:
		return
	if _bound_container != null and is_instance_valid(_bound_container):
		if _bound_container.is_connected("shop_item_bought", self, "_on_shop_item_bought"):
			_bound_container.disconnect("shop_item_bought", self, "_on_shop_item_bought")
		if _bound_container.is_connected(
			"shop_item_insufficient_currency", self, "_on_shop_item_insufficient_currency"
		):
			_bound_container.disconnect(
				"shop_item_insufficient_currency", self, "_on_shop_item_insufficient_currency"
			)
	_bound_container = container
	if not container.is_connected("shop_item_bought", self, "_on_shop_item_bought"):
		container.connect("shop_item_bought", self, "_on_shop_item_bought")
	if not container.is_connected(
		"shop_item_insufficient_currency", self, "_on_shop_item_insufficient_currency"
	):
		container.connect(
			"shop_item_insufficient_currency", self, "_on_shop_item_insufficient_currency"
		)


func _on_shop_item_bought(shop_item) -> void:
	_complete_by_offer(shop_item, {"ok": true})


func _on_shop_item_insufficient_currency(shop_item) -> void:
	_complete_by_offer(shop_item, {"ok": false, "error": "insufficient_gold"})


func _complete_by_offer(shop_item, result: Dictionary) -> void:
	for op in _pending:
		if op.kind != "shop_buy" or op.result != null:
			continue
		if not is_instance_valid(op.node):
			op.result = {"ok": false, "error": "shop_closed"}
			continue
		if op.node == shop_item or op.offer == shop_item or op.node.get("item_data") == shop_item:
			op.result = result


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


func _completion(op) -> Dictionary:
	var entry := {"ref": op.ref, "ack": _finish(op.ref, op.result)}
	if op.kind == "shop_buy" and op.result.get("ok") == true:
		entry["event"] = {
			"name": "purchase_done",
			"data": {"slot": op.slot, "id": op.item_id, "price": op.price},
		}
	return entry


func _timeout_error(kind: String) -> String:
	match kind:
		"shop_buy":
			return "buy_timeout"
		"shop_sell":
			return "sell_failed"
		_:
			return "reroll_failed"


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
