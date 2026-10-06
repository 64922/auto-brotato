extends Reference

# move 动作注入与安全停住（docs/architecture.md §4.6/§4.7；票据 02）。
#
# - 动作 TTL：超过 action_ttl_ms 未收到新 move 即归零（安全停住）。
# - 输入路径：默认 8 向数字 + PWM 占空比还原比例（button_move_*）。
#   实机实测（Brotato 1.1.15.4 / 自研 Godot 3.7）：合成手柄轴事件不可用于比例
#   控制——原始轴状态反映不稳定（偶发读 0），且游戏模拟量有 ~0.875 高阈值
#   （0.85 及以下完全不动，0.9 约 79% 满速），无法连续调速。
#   若确实需要模拟量优先（其他引擎），置 ProjectSettings input/prefer_analog=true，
#   届时按合成轴事件是否被输入状态反映自动回退数字（见 _probe_analog）。
# - 本模块只操作 Godot Input 单例，不直接改玩家坐标/速度（架构不变量 1）。

const LOG_NAME := "BrotatoPlayer-AutoBrotato:Movement"
const DIGITAL_THRESHOLD := 0.4  # tan(22.5°) ≈ 0.414，模拟量判定/8 向扇区参考
const DIGITAL_DEADBAND := 0.05  # 数字方向判定的分量死区
const PWM_PERIOD_MS := 100  # 数字回退的占空比周期

const BUTTON_ACTIONS := [
	"button_move_left",
	"button_move_right",
	"button_move_up",
	"button_move_down",
]

var ttl_ms := 250

var _vector := Vector2.ZERO
var _received_at_ms := -1
var _use_analog := false
var _probe_frames := 0
var _probe_commanded := Vector2.ZERO
var _button_state := {}


func _init() -> void:
	if ProjectSettings.has_setting("input/prefer_analog"):
		_use_analog = bool(ProjectSettings.get_setting("input/prefer_analog"))


# 收到 move 动作：校验向量（数组 [dx,dy] 数字，模长 ≤1，超界归一化）。
# 返回 "" 表示成功，否则为 ack 错误码。
# 注意：本引擎 is_finite() 恒返回 false（实测），NaN 判定改用自比较（NaN != NaN）。
func set_move(vector, now_ms: int) -> String:
	if typeof(vector) != typeof([]) or vector.size() != 2:
		return "bad_vector"
	for value in vector:
		var value_type := typeof(value)
		if value_type != typeof(0.0) and value_type != typeof(0):
			return "bad_vector"
	var v := Vector2(float(vector[0]), float(vector[1]))
	if v.x != v.x or v.y != v.y:
		return "bad_vector"
	if v.length() > 1.0:
		v = v.normalized()
	_vector = v
	_received_at_ms = now_ms
	return ""


# 每个物理帧调用：应用当前有效向量（含 TTL 与模拟/数字回退判定）。
func apply(now_ms: int) -> void:
	var target := Vector2.ZERO
	if _received_at_ms >= 0 and now_ms - _received_at_ms <= ttl_ms:
		target = _vector
	if _use_analog:
		_apply_analog(target)
		_probe_analog(target)
	else:
		_apply_digital(target, now_ms)


# 立即安全停住（断开/兜底时调用）：下一物理帧起注入归零。
func clear() -> void:
	_vector = Vector2.ZERO
	_received_at_ms = -1


func mode_in_use() -> String:
	return "analog" if _use_analog else "digital"


# 当前有效的 move 向量（TTL 内）；无动作或已过期返回零向量（调试叠加层用）。
func active_vector(now_ms: int) -> Vector2:
	if _received_at_ms < 0 or now_ms - _received_at_ms > ttl_ms:
		return Vector2.ZERO
	return _vector


func _apply_analog(v: Vector2) -> void:
	_send_axis(JOY_AXIS_0, v.x)
	_send_axis(JOY_AXIS_1, v.y)


func _send_axis(axis: int, value: float) -> void:
	var event := InputEventJoypadMotion.new()
	event.device = 0
	event.axis = axis
	event.axis_value = value
	Input.parse_input_event(event)


# 每帧连续注入非零轴事件若干帧后读取回输入状态：读不回则判定引擎不识别
# 合成轴事件，切换到 8 向数字输入（回退只发生一次，日志记录）。
func _probe_analog(target: Vector2) -> void:
	if target.length() < DIGITAL_THRESHOLD:
		return
	if target != _probe_commanded:
		_probe_commanded = target
		_probe_frames = 0
		return
	_probe_frames += 1
	if _probe_frames < 6:
		return
	if _analog_reflected(target):
		_probe_frames = 0
		return
	_use_analog = false
	_apply_analog(Vector2.ZERO)
	ModLoaderLog.warning(
		"合成手柄轴事件未被输入状态识别（joy_axis=(%.2f, %.2f)），回退 8 向数字输入"
		% [Input.get_joy_axis(0, JOY_AXIS_0), Input.get_joy_axis(0, JOY_AXIS_1)],
		LOG_NAME
	)


# 轴事件是否已反映到原始 Input 轴状态。只信原始轴（本引擎对合成轴事件不更新
# 原始轴、但会更新 action 强度，单看 action 会误判为已生效）。
func _analog_reflected(target: Vector2) -> bool:
	var axis := Vector2(Input.get_joy_axis(0, JOY_AXIS_0), Input.get_joy_axis(0, JOY_AXIS_1))
	return _reflects_component(target.x, axis.x) and _reflects_component(target.y, axis.y)


func _reflects_component(target: float, axis_value: float) -> bool:
	if abs(target) < 0.3:
		return true
	return sign(axis_value) == sign(target) and abs(axis_value) > 0.01


# 数字回退：方向按分量符号做 8 向量化，比例用 PWM 占空比还原
# （本引擎不支持合成模拟量，按键只有开关两态；周期内按下 duty=向量模长 的时长，
# 平均速度 ≈ 模长 × 满速）。
func _apply_digital(v: Vector2, now_ms: int) -> void:
	var duty := min(v.length(), 1.0)
	var pressed := {}
	if duty > 0.0 and now_ms % PWM_PERIOD_MS < int(round(duty * PWM_PERIOD_MS)):
		if v.x > DIGITAL_DEADBAND:
			pressed["button_move_right"] = true
		elif v.x < -DIGITAL_DEADBAND:
			pressed["button_move_left"] = true
		if v.y > DIGITAL_DEADBAND:
			pressed["button_move_down"] = true
		elif v.y < -DIGITAL_DEADBAND:
			pressed["button_move_up"] = true
	for action in BUTTON_ACTIONS:
		var want: bool = pressed.has(action)
		if _button_state.get(action, false) == want:
			continue
		_button_state[action] = want
		if want:
			Input.action_press(action, 1.0)
		else:
			Input.action_release(action)
