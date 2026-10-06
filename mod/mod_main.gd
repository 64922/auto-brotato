extends Node

# AutoBrotato mod 入口（票据 02：move 注入；票据 03：完整战斗观测 + 调试叠加层；
# 票据 06：商店观测与 shop_* 动作）。
#
# 决策在 Python 侧；本 mod 只做四件事：TCP 协议客户端、战斗/商店观测上送、
# move 与 shop_* 动作经游戏输入系统/UI 流程注入（含 TTL 与幂等防护）、
# 可开关的调试叠加层。

const MOD_VERSION := "0.2.0"
const GAME_VERSION := "1.1.15.4"
const LOG_NAME := "BrotatoPlayer-AutoBrotato:Main"
const SHOP_SAMPLE_INTERVAL := 0.1
const SHOP_RESEND_INTERVAL_MS := 1000

const IpcClient := preload("res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/ipc_client.gd")
const Observation := preload("res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/observation.gd")
const Movement := preload("res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/movement.gd")
const Overlay := preload("res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/overlay.gd")
const ShopObservation := preload(
	"res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/shop_observation.gd"
)
const ShopActions := preload("res://mods-unpacked/BrotatoPlayer-AutoBrotato/src/shop_actions.gd")

var _ipc
var _observation
var _movement
var _overlay
var _shop
var _shop_actions
var _snapshot_interval := 1.0 / 60.0
var _snapshot_accum := 0.0
var _shop_accum := 0.0
var _last_shop_signature := ""
var _last_shop_sent_ms := 0
var _link_was_ready := false


func _init() -> void:
	randomize()
	_ipc = IpcClient.new(MOD_VERSION, GAME_VERSION)
	_observation = Observation.new()
	_movement = Movement.new()
	_overlay = Overlay.new()
	_overlay.observation = _observation
	_overlay.movement = _movement
	_shop = ShopObservation.new()
	_shop_actions = ShopActions.new(_shop)


func _ready() -> void:
	ModLoaderLog.success(
		"AutoBrotato mod 已加载：version=%s game=%s session_id=%s"
		% [MOD_VERSION, GAME_VERSION, _ipc.session_id],
		LOG_NAME
	)


func _process(delta: float) -> void:
	_ipc.poll()
	_drain_messages()
	_check_link()
	_overlay.try_attach()
	if not _ipc.is_ready():
		return
	_snapshot_accum += delta
	if _snapshot_accum >= _snapshot_interval:
		_snapshot_accum = fmod(_snapshot_accum, _snapshot_interval)
		_ipc.send("snapshot", _observation.latest())
	_flush_shop_actions()
	_pump_shop(delta)


# 结算异步商店动作（购买/卖出/刷新）并回执；购买成功附带 purchase_done 事件。
func _flush_shop_actions() -> void:
	for done in _shop_actions.poll(OS.get_ticks_msec()):
		if done.has("event"):
			_ipc.send("event", done["event"])
		_ipc.send("ack", done["ack"], done["ref"])


# 商店观测：打开/内容变化时推送，另有 1s 心跳重发（architecture §4.3）。
func _pump_shop(delta: float) -> void:
	_shop_accum += delta
	if _shop_accum < SHOP_SAMPLE_INTERVAL:
		return
	_shop_accum = fmod(_shop_accum, SHOP_SAMPLE_INTERVAL)
	_shop.sample()
	var payload = _shop.latest()
	if payload == null:
		_last_shop_signature = ""
		return
	var signature := JSON.print(payload)
	var now := OS.get_ticks_msec()
	if signature != _last_shop_signature or now - _last_shop_sent_ms >= SHOP_RESEND_INTERVAL_MS:
		if _ipc.send("shop", payload):
			_last_shop_signature = signature
			_last_shop_sent_ms = now


func _physics_process(delta: float) -> void:
	_observation.sample(delta)
	_movement.apply(OS.get_ticks_msec())


func _check_link() -> void:
	var ready: bool = _ipc.is_ready()
	if ready == _link_was_ready:
		if ready and _ipc.is_fallback():
			_movement.clear()
		return
	_link_was_ready = ready
	if ready:
		ModLoaderLog.info("IPC 会话就绪，开始上送快照（input=%s）" % _movement.mode_in_use(), LOG_NAME)
	else:
		_movement.clear()
		_shop_actions.reset()
		_shop.invalidate()
		_last_shop_signature = ""
		ModLoaderLog.warning("IPC 断开，move 注入已归零（安全停住）", LOG_NAME)


func _drain_messages() -> void:
	for envelope in _ipc.take_messages():
		match envelope.get("type", ""):
			"welcome":
				_apply_welcome(envelope.get("payload", {}))
			"action":
				_handle_action(envelope)
			"ping", "pong":
				pass
			"error":
				ModLoaderLog.error("收到协议错误：%s" % JSON.print(envelope.get("payload", {})), LOG_NAME)
			_:
				pass


func _apply_welcome(payload: Dictionary) -> void:
	var config: Dictionary = payload.get("config", {})
	var hz = config.get("snapshot_hz", 60)
	var hz_type := typeof(hz)
	if hz_type == TYPE_REAL or hz_type == TYPE_INT:
		_snapshot_interval = 1.0 / clamp(float(hz), 1.0, 120.0)
	var ttl = config.get("action_ttl_ms", 250)
	var ttl_type := typeof(ttl)
	if ttl_type == TYPE_REAL or ttl_type == TYPE_INT:
		_movement.ttl_ms = int(clamp(float(ttl), 50.0, 1000.0))
	var overlay = config.get("debug_overlay", false)
	if typeof(overlay) == TYPE_BOOL:
		_overlay.set_enabled(overlay)
	ModLoaderLog.info(
		"welcome 配置：snapshot_hz=%.1f action_ttl_ms=%d debug_overlay=%s"
		% [1.0 / _snapshot_interval, _movement.ttl_ms, str(_overlay.is_enabled())],
		LOG_NAME
	)


func _handle_action(envelope: Dictionary) -> void:
	var ref = envelope.get("ref")
	var payload: Dictionary = envelope.get("payload", {})
	var kind: String = payload.get("kind", "")
	if kind == "move":
		var error: String = _movement.set_move(payload.get("vector"), OS.get_ticks_msec())
		if error == "":
			_ipc.send("ack", {"ok": true}, ref)
		else:
			_ipc.send("ack", {"ok": false, "error": error}, ref)
		return
	if kind == "debug_overlay":
		var enabled = payload.get("enabled")
		if typeof(enabled) != TYPE_BOOL:
			_ipc.send("ack", {"ok": false, "error": "bad_enabled"}, ref)
			return
		_overlay.set_enabled(enabled)
		ModLoaderLog.info("调试叠加层：%s" % ("开" if enabled else "关"), LOG_NAME)
		_ipc.send("ack", {"ok": true, "enabled": enabled}, ref)
		return
	if _shop_actions.handles(kind):
		var outcome = _shop_actions.execute(kind, payload, ref, OS.get_ticks_msec())
		# pending/queued 的结果经 _flush_shop_actions 统一回执（商业动作顺序执行）
		if outcome["state"] == "acked" or outcome["state"] == "duplicate":
			_ipc.send("ack", outcome["ack"], ref)
		return
	# 其余动作（menu_*）属后续票据；拒绝而不部分执行（architecture §4.6）
	_ipc.send("ack", {"ok": false, "error": "unsupported_kind:%s" % kind}, ref)


func _exit_tree() -> void:
	_ipc.stop()
	_movement.clear()
	if is_instance_valid(_overlay):
		_overlay.set_enabled(false)
