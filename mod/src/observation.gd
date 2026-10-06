extends Reference

# 完整战斗观测采集（票据 03，docs/architecture.md §4.4）。
#
# - 全字段：玩家/武器/敌人/弹幕/掉落/危险物/场地/背包/属性/经济。
# - 视野裁剪：以玩家为中心 cull_radius=900px；最近优先封顶（敌人 300、弹幕 400），
#   超限置 truncated=true。
# - 游戏内不存在或场景未就绪的类别给 null/空数组，字段不缺。
# - 引擎兼容：本引擎（Brotato 1.1.15.4）中 is_finite() 恒 false；内建类型判定用
#   typeof()；对象私有属性经 get() 读取，null 表示不存在。

const LOG_NAME := "BrotatoPlayer-AutoBrotato:Observation"
const RESCAN_INTERVAL_MS := 500
const INVENTORY_CACHE_MS := 500
const CULL_RADIUS := 900.0
const ENEMY_CAP := 300
const PROJECTILE_CAP := 400

# 协议 stats 字段 → 游戏内 Keys.generate_hash 使用的键
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

var _main = null
var _spawner = null
var _player = null
var _wave_timer: Timer = null
var _next_scan_ms := 0
var _last_positions := {}
var _inventory_cache := {"weapons": [], "items": []}
var _inventory_next_ms := 0
var _gold_wave_index := -999
var _gold_wave_start := -1.0
var _truncated_enemies := false
var _truncated_projectiles := false
var _latest = _empty_snapshot()


func _empty_snapshot() -> Dictionary:
	return {
		"t": 0.0,
		"wave": null,
		"player": null,
		"weapons": [],
		"enemies": [],
		"projectiles": [],
		"pickups": [],
		"hazards": [],
		"arena": null,
		"inventory": {"weapons": [], "items": []},
		"stats": null,
		"economy": null,
		"truncated": false,
	}


# 每个物理帧调用一次（delta 为物理帧秒数），缓存最新观测。
func sample(delta: float) -> void:
	var now := OS.get_ticks_msec()
	_refresh(now)
	var player = _find_player()
	var player_data = null
	if player != null:
		player_data = _sample_player(player, delta)
	_truncated_enemies = false
	_truncated_projectiles = false
	var enemies := _sample_enemies(player)
	var projectiles := _sample_projectiles(player)
	_latest = {
		"t": float(now) / 1000.0,
		"wave": _sample_wave(),
		"player": player_data,
		"weapons": _sample_live_weapons(player),
		"enemies": enemies,
		"projectiles": projectiles,
		"pickups": _sample_pickups(),
		"hazards": _sample_hazards(),
		"arena": _sample_arena(),
		"inventory": _sample_inventory(now),
		"stats": _sample_stats(),
		"economy": _sample_economy(),
		"fps": Engine.get_frames_per_second(),
		"truncated": _truncated_enemies or _truncated_projectiles,
	}


func latest() -> Dictionary:
	return _latest


# ---------------------------------------------------------------- 场景定位

func _refresh(now: int) -> void:
	if _main != null and is_instance_valid(_main) and _main.is_inside_tree():
		if _wave_timer == null or not is_instance_valid(_wave_timer):
			_wave_timer = _main.get_node_or_null("WaveTimer") as Timer
		return
	_main = null
	_spawner = null
	_wave_timer = null
	if now < _next_scan_ms:
		return
	_next_scan_ms = now + RESCAN_INTERVAL_MS
	var tree := Engine.get_main_loop() as SceneTree
	if tree == null:
		return
	var main = tree.root.get_node_or_null("Main")
	if main != null:
		_main = main
		_spawner = main.get("_entity_spawner")
		_wave_timer = main.get_node_or_null("WaveTimer") as Timer


func _find_player() -> Node2D:
	if _player != null and is_instance_valid(_player) and _player.is_inside_tree():
		return _player
	_player = null
	if _spawner != null and is_instance_valid(_spawner):
		for key in ["players", "_players"]:
			var players = _spawner.get(key)
			if typeof(players) == TYPE_ARRAY and players.size() > 0:
				var candidate = players[0]
				if candidate != null and is_instance_valid(candidate) and candidate.is_inside_tree():
					_player = candidate
					return _player
	# 退路：遍历场景树找 Player（菜单/商店期可能没有）
	var tree := Engine.get_main_loop() as SceneTree
	if tree == null:
		return null
	var stack := [tree.root]
	while not stack.empty():
		var node = stack.pop_back()
		if node is Player:
			_player = node
			return _player
		if node is Node:
			for child in node.get_children():
				stack.push_back(child)
	return null


# ---------------------------------------------------------------- 各字段采集

func _sample_player(player: Node2D, delta: float) -> Dictionary:
	var pos: Vector2 = player.global_position
	var vel := _observed_velocity(player, pos, delta)
	var current_stats = player.get("current_stats")
	var max_stats = player.get("max_stats")
	var hp := _stat_value(current_stats, "health", 0.0)
	var max_hp := _stat_value(max_stats, "health", hp)
	if max_hp <= 0.0:
		max_hp = max(hp, 1.0)
	var dead = player.get("dead")
	var invuln_timer = player.get("_invincibility_timer")
	var invuln := false
	if invuln_timer != null and is_instance_valid(invuln_timer) and invuln_timer is Timer:
		invuln = not invuln_timer.is_stopped()
	return {
		"pos": [pos.x, pos.y],
		"vel": [vel.x, vel.y],
		"hp": hp,
		"max_hp": max_hp,
		"alive": hp > 0.0 and dead != true,
		"invuln": invuln,
		"speed": _stat("speed"),
		"armor": _stat("armor"),
		"dodge": _stat("dodge"),
		"lifesteal": _stat("lifesteal"),
		"regen": _stat("hp_regeneration"),
	}


func _sample_live_weapons(player) -> Array:
	var out := []
	if player == null:
		return out
	var weapons = player.get("current_weapons")
	if typeof(weapons) != TYPE_ARRAY:
		return out
	for weapon in weapons:
		if weapon == null or not is_instance_valid(weapon):
			continue
		var cooldown := 0.0
		var current_stats = weapon.get("current_stats")
		if current_stats != null:
			cooldown = _stat_value(current_stats, "cooldown", 0.0)
		var current := 0.0
		var raw_current = weapon.get("_current_cooldown")
		if typeof(raw_current) == TYPE_REAL or typeof(raw_current) == TYPE_INT:
			current = float(raw_current)
		var pct := 0.0
		if cooldown > 0.0:
			pct = clamp(current / cooldown, 0.0, 1.0)
		out.append({
			"id": str(weapon.get("weapon_id")),
			"tier": _int_or(weapon.get("tier"), 0),
			"cooldown_pct": pct,
		})
	return out


func _sample_enemies(player) -> Array:
	var out := []
	var spawner = _spawner
	if spawner == null or not is_instance_valid(spawner):
		return out
	var enemies = spawner.get("enemies")
	if typeof(enemies) != TYPE_ARRAY:
		return out
	var center = null
	if player != null:
		center = player.global_position
	var delta := _physics_delta()
	for enemy in enemies:
		if enemy == null or not is_instance_valid(enemy):
			continue
		if enemy.get("dead") == true or enemy.visible == false:
			continue
		var pos: Vector2 = enemy.global_position
		var dist := -1.0
		if center != null:
			dist = (pos - center).length()
			if dist > CULL_RADIUS:
				continue
		var vel := _observed_velocity(enemy, pos, delta)
		var hp := 0.0
		var max_hp := 1.0
		var current_stats = enemy.get("current_stats")
		var max_stats = enemy.get("max_stats")
		hp = _stat_value(current_stats, "health", 0.0)
		max_hp = _stat_value(max_stats, "health", hp)
		if max_hp <= 0.0:
			max_hp = max(hp, 1.0)
		out.append({
			"id": enemy.get_instance_id(),
			"type": str(enemy.get("enemy_id")),
			"pos": [pos.x, pos.y],
			"vel": [vel.x, vel.y],
			"hp_pct": clamp(hp / max_hp, 0.0, 1.0),
			"radius": _entity_radius(enemy, 8.0),
			"elite": enemy.get("is_elite") == true,
			"boss": enemy is Boss and enemy.get("is_elite") != true,
			"_dist": dist,
		})
	if out.size() > ENEMY_CAP:
		out.sort_custom(_Sorter.new(), "closest_first")
		out.resize(ENEMY_CAP)
		_truncated_enemies = true
	for entry in out:
		entry.erase("_dist")
	return out


func _sample_projectiles(player) -> Array:
	var out := []
	var main = _main
	if main == null or not is_instance_valid(main):
		return out
	var center = null
	if player != null:
		center = player.global_position
	for friendly in [false, true]:
		var key = "_player_projectiles" if friendly else "_enemy_projectiles"
		var container = main.get(key)
		var projectiles: Array = []
		if typeof(container) == TYPE_ARRAY:
			projectiles = container
		elif container is Node:
			projectiles = container.get_children()
		else:
			continue
		for projectile in projectiles:
			if projectile == null or not is_instance_valid(projectile):
				continue
			if projectile.visible == false:
				continue
			var pos: Vector2 = projectile.global_position
			var dist := -1.0
			if center != null:
				dist = (pos - center).length()
				if dist > CULL_RADIUS:
					continue
			var vel := Vector2.ZERO
			var raw_velocity = projectile.get("velocity")
			if typeof(raw_velocity) == TYPE_VECTOR2:
				vel = raw_velocity
			var ttl := 0.0
			var raw_ttl = projectile.get("_time_until_max_range")
			if typeof(raw_ttl) == TYPE_REAL or typeof(raw_ttl) == TYPE_INT:
				ttl = max(float(raw_ttl), 0.0)
			out.append({
				"id": projectile.get_instance_id(),
				"pos": [pos.x, pos.y],
				"vel": [vel.x, vel.y],
				"radius": _projectile_radius(projectile, 4.0),
				"friendly": friendly,
				"ttl": ttl,
				"_dist": dist,
			})
	if out.size() > PROJECTILE_CAP:
		out.sort_custom(_Sorter.new(), "closest_first")
		out.resize(PROJECTILE_CAP)
		_truncated_projectiles = true
	for entry in out:
		entry.erase("_dist")
	return out


func _sample_pickups() -> Array:
	var out := []
	var main = _main
	if main == null or not is_instance_valid(main):
		return out
	for kind in ["material", "consumable"]:
		var key = "_active_golds" if kind == "material" else "_consumables"
		var items = main.get(key)
		if typeof(items) != TYPE_ARRAY:
			continue
		for item in items:
			if item == null or not is_instance_valid(item):
				continue
			if item.visible == false or item.get("already_picked_up") == true:
				continue
			var pos: Vector2 = item.global_position
			out.append({"kind": kind, "pos": [pos.x, pos.y]})
	return out


func _sample_hazards() -> Array:
	var out := []
	var spawner = _spawner
	if spawner == null or not is_instance_valid(spawner):
		return out
	var structures = spawner.get("structures")
	if typeof(structures) != TYPE_ARRAY:
		return out
	for structure in structures:
		if structure == null or not is_instance_valid(structure):
			continue
		if not (structure is Landmine):
			continue
		if structure.get("dead") == true or structure.visible == false:
			continue
		var pos: Vector2 = structure.global_position
		out.append({
			"kind": "landmine",
			"pos": [pos.x, pos.y],
			"radius": _entity_radius(structure, 16.0),
		})
	return out


func _sample_arena():
	var rect = ZoneService.current_zone_rect
	if typeof(rect) != TYPE_RECT2 or rect.size.x <= 0.0 or rect.size.y <= 0.0:
		return null
	return {
		"min": [rect.position.x, rect.position.y],
		"max": [rect.end.x, rect.end.y],
	}


func _sample_wave():
	var timer = _wave_timer
	if timer == null or not is_instance_valid(timer):
		return null
	var index := -1
	if RunData != null:
		index = _int_or(RunData.current_wave, -1)
	return {
		"index": index,
		"phase": "combat" if not timer.is_stopped() else "ended",
		"time_left": float(timer.time_left),
		"time_total": float(timer.wait_time),
	}


func _sample_inventory(now: int) -> Dictionary:
	if now < _inventory_next_ms:
		return _inventory_cache
	_inventory_next_ms = now + INVENTORY_CACHE_MS
	var weapons := []
	var items := []
	if RunData != null and RunData.get_player_count() > 0:
		var raw_weapons = RunData.get_player_weapons_ref(0)
		if typeof(raw_weapons) == TYPE_ARRAY:
			for index in range(raw_weapons.size()):
				var weapon = raw_weapons[index]
				if weapon == null:
					continue
				var weapon_id = weapon.get("weapon_id")
				if typeof(weapon_id) != TYPE_STRING or weapon_id == "":
					weapon_id = weapon.get("my_id")
				weapons.append({
					"slot": int(index),
					"id": str(weapon_id),
					"tier": _int_or(weapon.get("tier"), 0),
				})
		var counts := {}
		var order := []
		var raw_items = RunData.get_player_items(0)
		if typeof(raw_items) == TYPE_ARRAY:
			for item in raw_items:
				if item == null:
					continue
				var item_id = item.get("my_id")
				if typeof(item_id) != TYPE_STRING or item_id == "":
					continue
				if not counts.has(item_id):
					counts[item_id] = 0
					order.append(item_id)
				counts[item_id] += 1
		order.sort()
		for item_id in order:
			items.append({"id": item_id, "count": counts[item_id]})
	_inventory_cache = {"weapons": weapons, "items": items}
	return _inventory_cache


func _sample_stats():
	if RunData == null or RunData.get_player_count() <= 0:
		return null
	var stats := {}
	for field_name in STAT_KEYS:
		stats[field_name] = _stat(STAT_KEYS[field_name])
	return stats


func _sample_economy():
	if RunData == null or RunData.get_player_count() <= 0:
		return null
	var gold := _number_or(RunData.get_player_gold(0), 0.0)
	var wave_index := -1
	if RunData != null:
		wave_index = _int_or(RunData.current_wave, -1)
	if wave_index != _gold_wave_index:
		_gold_wave_index = wave_index
		_gold_wave_start = gold
	if _gold_wave_start < 0.0:
		_gold_wave_start = gold
	return {
		"gold": gold,
		"materials_this_wave": max(gold - _gold_wave_start, 0.0),
	}


# ---------------------------------------------------------------- 工具

func _physics_delta() -> float:
	var fps := Engine.iterations_per_second
	if fps <= 0:
		return 1.0 / 60.0
	return 1.0 / float(fps)


# 速度由物理帧位置差分计算（与票据 02 相同路径；传送/复活跳变按 0 处理）。
func _observed_velocity(entity, pos: Vector2, delta: float) -> Vector2:
	var key: int = entity.get_instance_id()
	var previous = _last_positions.get(key)
	_last_positions[key] = pos
	if previous == null or typeof(previous) != TYPE_VECTOR2 or delta <= 0.0:
		return Vector2.ZERO
	var movement: Vector2 = pos - previous
	if movement.length() > 3000.0:
		return Vector2.ZERO
	return movement / delta


func _stat(name: String) -> float:
	var value = Utils.get_stat(Keys.generate_hash(name), 0)
	if typeof(value) == TYPE_REAL or typeof(value) == TYPE_INT:
		return float(value)
	return 0.0


func _stat_value(stats, name: String, fallback: float) -> float:
	if stats == null:
		return fallback
	var value = stats.get(name)
	if typeof(value) == TYPE_REAL or typeof(value) == TYPE_INT:
		return float(value)
	return fallback


func _entity_radius(entity, fallback: float) -> float:
	var collision = entity.get("_collision")
	if collision == null:
		collision = entity.get_node_or_null("Collision")
	if collision == null:
		return fallback
	var shape = collision.get("shape")
	if shape == null:
		return fallback
	var radius = shape.get("radius")
	if typeof(radius) == TYPE_REAL or typeof(radius) == TYPE_INT:
		return max(float(radius), 0.0)
	return fallback


func _projectile_radius(projectile, fallback: float) -> float:
	var hitbox = projectile.get("_hitbox")
	if hitbox == null:
		return fallback
	var collision = hitbox.get_node_or_null("Collision")
	if collision == null:
		return fallback
	var shape = collision.get("shape")
	if shape == null:
		return fallback
	var radius = shape.get("radius")
	if typeof(radius) == TYPE_REAL or typeof(radius) == TYPE_INT:
		return max(float(radius), 0.0)
	return fallback


func _int_or(value, fallback: int) -> int:
	if typeof(value) == TYPE_INT:
		return value
	if typeof(value) == TYPE_REAL:
		return int(value)
	return fallback


func _number_or(value, fallback: float) -> float:
	if typeof(value) == TYPE_REAL or typeof(value) == TYPE_INT:
		return float(value)
	return fallback


# 按 "最近优先" 排序用的比较器（实体上限裁剪时使用）。
class _Sorter:
	extends Reference

	func closest_first(a: Dictionary, b: Dictionary) -> bool:
		var da = a.get("_dist", -1.0)
		var db = b.get("_dist", -1.0)
		if da < 0.0:
			return false
		if db < 0.0:
			return true
		return da < db
