extends Reference

# auto_brotato TCP/NDJSON 客户端（协议 v2，docs/protocol.md §7；docs/architecture.md §4）。
#
# 职责：连接/指数退避重连、hello/welcome 握手、逐行解析 NDJSON、心跳应答 pong、
# 记录最近一次 Python 消息时间（供断线兜底判定）。不含策略逻辑。

const LOG_NAME := "BrotatoPlayer-AutoBrotato:IPC"
const PROTOCOL_VERSION := 2
const RETRY_INITIAL_MS := 2000
const RETRY_MAX_MS := 10000
const WELCOME_TIMEOUT_MS := 10000
const FALLBACK_TIMEOUT_MS := 2000

var host := "127.0.0.1"
var port := 37650
var mod_version := "0.1.0"
var game_version := "1.1.15.4"
var session_id := ""

var _sock: StreamPeerTCP = null
var _status := "disconnected"  # disconnected / connecting / handshaking / ready / rejected
var _buf := PoolByteArray()
var _messages := []
var _seq := 0
var _retry_ms := RETRY_INITIAL_MS
var _next_attempt_ms := 0
var _handshake_started_ms := 0
var _last_msg_ms := 0


func _init(mod_version_value: String, game_version_value: String) -> void:
	mod_version = mod_version_value
	game_version = game_version_value
	host = _option_string("ipc/host", "127.0.0.1")
	port = _option_int("ipc/port", 37650)
	session_id = "%08x%08x" % [randi(), randi()]


func _option_string(name: String, fallback: String) -> String:
	if ProjectSettings.has_setting(name):
		var value = ProjectSettings.get_setting(name)
		if typeof(value) == TYPE_STRING and value != "":
			return value
	return fallback


func _option_int(name: String, fallback: int) -> int:
	if ProjectSettings.has_setting(name):
		var value = ProjectSettings.get_setting(name)
		var value_type := typeof(value)
		if value_type == TYPE_REAL or value_type == TYPE_INT:
			return int(value)
	return fallback


# 每帧调用：推进连接状态机并读取 socket。
func poll() -> void:
	if _status == "rejected":
		return
	var now := OS.get_ticks_msec()
	if _sock == null:
		if now < _next_attempt_ms:
			return
		_next_attempt_ms = now + _retry_ms
		_sock = StreamPeerTCP.new()
		var err := _sock.connect_to_host(host, port)
		if err != OK:
			_sock = null
			_schedule_retry(now)
			return
		_status = "connecting"
		return

	_sock.poll()
	match _sock.get_status():
		StreamPeerTCP.STATUS_CONNECTING:
			pass
		StreamPeerTCP.STATUS_CONNECTED:
			if _status == "connecting":
				_status = "handshaking"
				_handshake_started_ms = now
				_last_msg_ms = now
				_send_hello()
			_read_available()
			if _status == "handshaking" and now - _handshake_started_ms > WELCOME_TIMEOUT_MS:
				_disconnect("welcome_timeout")
		_:
			_disconnect("connection_lost")


func is_ready() -> bool:
	return _status == "ready"


# 会话建立后超过 FALLBACK_TIMEOUT_MS 未收到任何 Python 消息 → 兜底（安全停住）。
func is_fallback() -> bool:
	if _status != "ready":
		return true
	return OS.get_ticks_msec() - _last_msg_ms > FALLBACK_TIMEOUT_MS


func take_messages() -> Array:
	var out := _messages
	_messages = []
	return out


func send(msg_type: String, payload: Dictionary, ref = null) -> bool:
	if _sock == null:
		return false
	if _status != "ready" and msg_type != "hello":
		return false
	var envelope := {
		"v": PROTOCOL_VERSION,
		"seq": _seq,
		"ts": _unix_time(),
		"type": msg_type,
		"ref": ref,
		"payload": payload,
	}
	_seq += 1
	var data := (JSON.print(envelope) + "\n").to_utf8()
	var err := _sock.put_data(data)
	if err != OK:
		# 连接仍存活时 put_data 失败多为发送缓冲暂满（ERR_BUSY）：丢弃该条消息，
		# 不因瞬态错误断开连接；连接已失效才进入重连。
		if _sock.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			_disconnect("write_error")
		return false
	return true


func stop() -> void:
	_disconnect("stopped")
	_status = "rejected"


func _send_hello() -> void:
	var payload := {
		"protocol_version": PROTOCOL_VERSION,
		"mod_version": mod_version,
		"game_version": game_version,
		"session_id": session_id,
		"capabilities": {"snapshot_hz": 60, "move_analog": true},
	}
	if not send("hello", payload):
		_disconnect("hello_failed")


func _read_available() -> void:
	var available := _sock.get_available_bytes()
	if available <= 0:
		return
	var result = _sock.get_data(available)
	if result[0] != OK:
		_disconnect("read_error")
		return
	_buf.append_array(result[1])
	var start := 0
	while true:
		var newline := -1
		for i in range(start, _buf.size()):
			if _buf[i] == 10:
				newline = i
				break
		if newline < 0:
			break
		if newline > start:
			_handle_line(_buf.subarray(start, newline - 1).get_string_from_utf8())
		start = newline + 1
	if start > 0:
		# 注意：start == _buf.size() 时 subarray 会越界（本引擎下直接 FATAL 崩溃），
		# 必须整体清空缓冲区而不是 subarray。
		if start < _buf.size():
			_buf = _buf.subarray(start)
		else:
			_buf = PoolByteArray()


func _handle_line(line: String) -> void:
	var parsed := JSON.parse(line)
	if parsed.error != OK or typeof(parsed.result) != TYPE_DICTIONARY:
		ModLoaderLog.warning("丢弃无法解析的消息：%s" % line.substr(0, 200), LOG_NAME)
		return
	var envelope: Dictionary = parsed.result
	_last_msg_ms = OS.get_ticks_msec()
	_messages.append(envelope)
	match envelope.get("type", ""):
		"welcome":
			_status = "ready"
			_retry_ms = RETRY_INITIAL_MS
			ModLoaderLog.success(
				"hello/welcome 握手完成：session_id=%s config=%s"
				% [session_id, JSON.print(envelope.get("payload", {}))],
				LOG_NAME
			)
		"ping":
			send("pong", {})
		"error":
			_status = "rejected"
			ModLoaderLog.error(
				"服务端拒绝会话：%s" % JSON.print(envelope.get("payload", {})), LOG_NAME
			)
			_disconnect("rejected")


func _disconnect(reason: String) -> void:
	if _sock != null:
		_sock.disconnect_from_host()
		_sock = null
	if _status != "rejected":
		_status = "disconnected"
	_schedule_retry(OS.get_ticks_msec())
	ModLoaderLog.info("连接断开（%s），%dms 后重试" % [reason, _retry_ms], LOG_NAME)


func _schedule_retry(now: int) -> void:
	_next_attempt_ms = now + _retry_ms
	_retry_ms = min(_retry_ms * 2, RETRY_MAX_MS)


func _unix_time() -> float:
	return float(OS.get_unix_time()) + float(OS.get_ticks_msec() % 1000) / 1000.0
