extends Node2D

# 调试叠加层（票据 03，docs/architecture.md §8.2/§9.3）。
#
# 在游戏画面上绘制：实体（玩家/敌人/弹幕/掉落/危险物）、威胁向量（沿速度的
# 短线段）、选中方向（Python move 向量）、预测碰撞点（弹幕与玩家的最近接近点）。
# 默认关闭，随 welcome.config.debug_overlay 或 debug_overlay 动作开关。
#
# 自挂载到 /root/Main（世界坐标系，随相机变换），z_index 保证绘制在实体之上。

const LOG_NAME := "BrotatoPlayer-AutoBrotato:Overlay"
const Z_INDEX := 2048

const PLAYER_COLOR := Color(0.2, 1.0, 1.0, 0.9)
const DIRECTION_COLOR := Color(0.2, 1.0, 0.3, 0.9)
const ENEMY_COLOR := Color(1.0, 0.25, 0.25, 0.75)
const ELITE_COLOR := Color(1.0, 0.6, 0.1, 0.9)
const BOSS_COLOR := Color(1.0, 0.2, 1.0, 0.95)
const ENEMY_PROJECTILE_COLOR := Color(1.0, 0.5, 0.2, 0.8)
const FRIENDLY_PROJECTILE_COLOR := Color(1.0, 1.0, 0.3, 0.6)
const THREAT_COLOR := Color(1.0, 0.3, 0.3, 0.45)
const PREDICTION_COLOR := Color(1.0, 1.0, 1.0, 0.9)
const MATERIAL_COLOR := Color(0.3, 1.0, 0.5, 0.7)
const CONSUMABLE_COLOR := Color(0.4, 0.7, 1.0, 0.8)
const HAZARD_COLOR := Color(1.0, 0.55, 0.1, 0.85)

const PLAYER_RADIUS := 13.0
const DIRECTION_LENGTH := 120.0
const THREAT_HORIZON_S := 0.4
const PREDICTION_HORIZON_S := 1.5
const PREDICTION_MARGIN := 12.0

var enabled := false
var observation = null
var movement = null

var _main = null
var _attach_pending := false


func _process(_delta: float) -> void:
	if enabled:
		update()


func _notification(what: int) -> void:
	if what == NOTIFICATION_ENTER_TREE or what == NOTIFICATION_EXIT_TREE:
		_attach_pending = false


# 由 mod 主循环每帧调用：找到 /root/Main 后挂载自身（世界坐标系，随相机变换）。
# 若同一 Main 上挂载请求尚未生效，不重复排队；Main 被替换（新一局）时会自动重挂。
func try_attach() -> void:
	if is_inside_tree():
		return
	var tree := Engine.get_main_loop() as SceneTree
	if tree == null:
		return
	var main = tree.root.get_node_or_null("Main")
	if main == null:
		return
	if _attach_pending and main == _main:
		return
	_main = main
	_attach_pending = true
	z_index = Z_INDEX
	visible = enabled
	main.call_deferred("add_child", self)


func set_enabled(value: bool) -> void:
	enabled = value
	if is_inside_tree():
		visible = value
		update()


func is_enabled() -> bool:
	return enabled


func _draw() -> void:
	if not enabled or observation == null:
		return
	var snapshot = observation.latest()
	if typeof(snapshot) != TYPE_DICTIONARY:
		return
	var player = snapshot.get("player")
	var player_pos := Vector2.ZERO
	var player_vel := Vector2.ZERO
	var has_player := false
	if typeof(player) == TYPE_DICTIONARY and typeof(player.get("pos")) == TYPE_ARRAY:
		player_pos = _to_vector2(player.get("pos"))
		player_vel = _to_vector2(player.get("vel"))
		has_player = true
		draw_circle(player_pos, PLAYER_RADIUS, PLAYER_COLOR)
		var direction := Vector2.ZERO
		if movement != null:
			direction = movement.active_vector(OS.get_ticks_msec())
		if direction.length() > 0.01:
			draw_line(player_pos, player_pos + direction * DIRECTION_LENGTH, DIRECTION_COLOR, 3.0)
	for enemy in snapshot.get("enemies", []):
		var pos := _to_vector2(enemy.get("pos"))
		var vel := _to_vector2(enemy.get("vel"))
		var radius = enemy.get("radius", 8.0)
		var color := ENEMY_COLOR
		if enemy.get("boss", false):
			color = BOSS_COLOR
		elif enemy.get("elite", false):
			color = ELITE_COLOR
		draw_circle(pos, float(radius), Color(color.r, color.g, color.b, 0.25))
		draw_line(pos, pos + vel * THREAT_HORIZON_S, THREAT_COLOR, 1.0)
	for projectile in snapshot.get("projectiles", []):
		var pos := _to_vector2(projectile.get("pos"))
		var vel := _to_vector2(projectile.get("vel"))
		var radius = float(projectile.get("radius", 4.0))
		if projectile.get("friendly", false):
			draw_circle(pos, radius, FRIENDLY_PROJECTILE_COLOR)
			continue
		draw_circle(pos, radius, ENEMY_PROJECTILE_COLOR)
		draw_line(pos, pos + vel * THREAT_HORIZON_S, THREAT_COLOR, 1.0)
		if has_player:
			_draw_predicted_collision(pos, vel, player_pos, player_vel, radius)
	for pickup in snapshot.get("pickups", []):
		var pos := _to_vector2(pickup.get("pos"))
		var color := MATERIAL_COLOR
		if pickup.get("kind", "") == "consumable":
			color = CONSUMABLE_COLOR
		draw_rect(Rect2(pos - Vector2(3, 3), Vector2(6, 6)), color, true)
	for hazard in snapshot.get("hazards", []):
		var pos := _to_vector2(hazard.get("pos"))
		draw_circle(pos, float(hazard.get("radius", 16.0)), HAZARD_COLOR)


# 弹幕与玩家的最近接近点（相对运动线性预测；接近距离进入接触范围才绘制）。
func _draw_predicted_collision(
	pos: Vector2, vel: Vector2, player_pos: Vector2, player_vel: Vector2, radius: float
) -> void:
	var rel_pos := pos - player_pos
	var rel_vel := vel - player_vel
	var denom := rel_vel.length_squared()
	if denom < 0.0001:
		return
	var t := clamp(-rel_pos.dot(rel_vel) / denom, 0.0, PREDICTION_HORIZON_S)
	var point := pos + vel * t
	var approach := point.distance_to(player_pos + player_vel * t)
	if approach <= PLAYER_RADIUS + radius + PREDICTION_MARGIN:
		draw_circle(point, 5.0, PREDICTION_COLOR)


func _to_vector2(value) -> Vector2:
	if typeof(value) != TYPE_ARRAY or value.size() != 2:
		return Vector2.ZERO
	return Vector2(float(value[0]), float(value[1]))
