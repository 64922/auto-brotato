extends Reference

# 知识库条目构建（票据 11）：把 ItemService/ChallengeService 的运行时资源转换为
# JSON 安全的字典条目。与落盘/版本哈希（knowledge_export.gd）分离，便于单文件
# 规模控制；字段语义与稳定性约定见 knowledge_export.gd 文件头。

#: 武器 stats 资源落盘字段（近战/远程并集；缺失字段省略）
const WEAPON_STATS_KEYS := [
	"cooldown",
	"damage",
	"accuracy",
	"crit_chance",
	"crit_damage",
	"min_range",
	"max_range",
	"knockback",
	"knockback_piercing",
	"can_have_positive_knockback",
	"can_have_negative_knockback",
	"effect_scale",
	"scaling_stats",
	"lifesteal",
	"sound_db_mod",
	"is_healing",
	"recoil",
	"recoil_duration",
	"additional_cooldown_every_x_shots",
	"additional_cooldown_multiplier",
	"speed_percent_modifier",
	"nb_projectiles",
	"projectile_spread",
	"piercing",
	"piercing_dmg_reduction",
	"bounce",
	"bounce_dmg_reduction",
	"can_bounce",
	"projectile_speed",
	"increase_projectile_speed_with_range",
	"attack_type",
	"alternate_attack_type",
]


func build_items(raw) -> Array:
	var out := []
	if raw is Array:
		for resource in raw:
			if resource != null:
				out.append(_item_entry(resource))
	return _finish_entries(out)


func build_weapons(raw) -> Array:
	var out := []
	if raw is Array:
		for resource in raw:
			if resource != null:
				out.append(_weapon_entry(resource))
	return _finish_entries(out)


func build_upgrades(raw) -> Array:
	var out := []
	if raw is Array:
		for resource in raw:
			if resource != null:
				out.append(_upgrade_entry(resource))
	return _finish_entries(out)


func build_characters(raw, challenges_by_name: Dictionary) -> Array:
	var out := []
	if raw is Array:
		for resource in raw:
			if resource != null:
				out.append(_character_entry(resource, challenges_by_name))
	return _finish_entries(out)


func build_sets(raw) -> Array:
	var out := []
	if raw is Array:
		for resource in raw:
			if resource != null:
				out.append(_set_entry(resource))
	return _finish_entries(out)


# ChallengeService.challenges 按 name（本地化键）索引：英雄资源与挑战同名即关联。
func index_challenges(challenge_service) -> Dictionary:
	var out := {}
	if challenge_service == null:
		return out
	var raw = challenge_service.get("challenges")
	if not (raw is Array):
		return out
	for challenge in raw:
		if challenge == null:
			continue
		var name_key := _as_string(challenge.get("name"))
		if name_key == "" or out.has(name_key):
			continue
		out[name_key] = _challenge_entry(challenge)
	return out


func _item_entry(resource) -> Dictionary:
	var entry := _base_entry(resource)
	entry["price"] = _as_int(resource.get("value"))
	entry["max_nb"] = _as_int(resource.get("max_nb"), -1)
	entry["tags"] = _as_strings(resource.get("tags"))
	var replaced_by = resource.get("replaced_by")
	entry["replaced_by"] = _resource_id(replaced_by) if replaced_by != null else null
	return entry


func _upgrade_entry(resource) -> Dictionary:
	var entry := _base_entry(resource)
	entry["upgrade_id"] = _as_string(resource.get("upgrade_id"))
	entry["max_nb"] = _as_int(resource.get("max_nb"), -1)
	return entry


func _weapon_entry(resource) -> Dictionary:
	var entry := _base_entry(resource)
	entry["price"] = _as_int(resource.get("value"))
	entry["weapon_id"] = _as_string(resource.get("weapon_id"))
	entry["type"] = _as_int(resource.get("type"))
	# WeaponData.Type：0=MELEE、1=RANGED（导出为可读 class；枚举无其他取值）
	entry["class"] = "ranged" if _as_int(resource.get("type")) == 1 else "melee"
	entry["stats"] = _weapon_stats(resource.get("stats"))
	entry["sets"] = _resource_ids(resource.get("sets"))
	entry["chain"] = _weapon_chain(resource)
	var upgrades_into = resource.get("upgrades_into")
	entry["upgrades_into"] = _resource_id(upgrades_into) if upgrades_into != null else null
	entry["add_to_chars_as_starting"] = _as_strings(resource.get("add_to_chars_as_starting"))
	return entry


func _character_entry(resource, challenges_by_name: Dictionary) -> Dictionary:
	var entry := _base_entry(resource)
	entry["starting_weapons"] = _resource_ids(resource.get("starting_weapons"))
	entry["starting_items"] = _resource_ids(resource.get("starting_items"))
	entry["tags"] = _as_strings(resource.get("tags"))
	entry["wanted_tags"] = _as_strings(resource.get("wanted_tags"))
	entry["banned_item_groups"] = _as_strings(resource.get("banned_item_groups"))
	entry["banned_items"] = _id_list(resource.get("banned_items"))
	entry["banned_upgrades"] = _id_list(resource.get("banned_upgrades"))
	entry["max_nb"] = _as_int(resource.get("max_nb"), -1)
	var name_key := _as_string(resource.get("name"))
	entry["unlock_challenge"] = challenges_by_name.get(name_key) if challenges_by_name.has(name_key) else null
	return entry


func _set_entry(resource) -> Dictionary:
	var bonuses := []
	var raw_bonuses = resource.get("set_bonuses")
	if raw_bonuses is Array:
		# set_bonuses 按件数（1..N）分层，索引 i 对应集齐 i+1 件的加成
		for tier_effects in raw_bonuses:
			var effects := []
			if tier_effects is Array:
				for effect in tier_effects:
					if effect != null:
						effects.append(_effect_entry(effect))
			bonuses.append(effects)
	return {
		"id": _as_string(resource.get("my_id")),
		"name": _translated_name(resource.get("name")),
		"name_key": _as_string(resource.get("name")),
		"bonuses": bonuses,
	}


func _base_entry(resource) -> Dictionary:
	return {
		"id": _as_string(resource.get("my_id")),
		"name": _translated_name(resource.get("name")),
		"name_key": _as_string(resource.get("name")),
		"tier": _as_int(resource.get("tier")),
		"effects": _effects(resource.get("effects")),
		"stat_deltas": _stat_deltas(resource.get("effects")),
		"can_be_looted": _as_bool(resource.get("can_be_looted")),
		"unlocked_by_default": _as_bool(resource.get("unlocked_by_default")),
		"is_lockable": _as_bool(resource.get("is_lockable")),
		"is_cursed": _as_bool(resource.get("is_cursed")),
		"curse_factor": _as_number(resource.get("curse_factor")),
	}


func _challenge_entry(challenge) -> Dictionary:
	var reward = challenge.get("reward")
	return {
		"id": _as_string(challenge.get("my_id")),
		"description_key": _as_string(challenge.get("description")),
		"reward_type": _as_int(challenge.get("reward_type")),
		"reward_id": _resource_id(reward) if reward != null else null,
		"number": _as_int(challenge.get("number")),
		"stat": _as_string(challenge.get("stat")),
	}


func _weapon_stats(stats) -> Dictionary:
	var out := {}
	if stats == null:
		return out
	for key in WEAPON_STATS_KEYS:
		var value = stats.get(key)
		if value == null:
			continue
		out[key] = _json_value(value)
	return out


func _effects(raw_effects) -> Array:
	var out := []
	if raw_effects is Array:
		for effect in raw_effects:
			if effect != null:
				out.append(_effect_entry(effect))
	return out


func _effect_entry(effect) -> Dictionary:
	return {
		"key": _as_string(effect.get("key")),
		"text_key": _as_string(effect.get("text_key")),
		"custom_key": _as_string(effect.get("custom_key")),
		"value": _as_number(effect.get("value")),
		"storage_method": _as_int(effect.get("storage_method")),
		"effect_sign": _as_int(effect.get("effect_sign")),
		"custom_args": _custom_args(effect.get("custom_args")),
	}


func _custom_args(raw_args) -> Array:
	var out := []
	if raw_args is Array:
		for arg in raw_args:
			if arg == null:
				continue
			out.append({
				"arg_index": _as_int(arg.get("arg_index")),
				"arg_key": _as_string(arg.get("arg_key")),
				"arg_value": _as_int(arg.get("arg_value")),
				"arg_sign": _as_int(arg.get("arg_sign")),
				"arg_format": _as_int(arg.get("arg_format")),
			})
	return out


# 属性增量：与游戏 Effect.apply() 同口径——SUM 存储时直接累加 value
# （effect_sign 仅影响文案显示，负值效果以负 value 表达）。
func _stat_deltas(raw_effects) -> Dictionary:
	var deltas := {}
	if not (raw_effects is Array):
		return deltas
	for effect in raw_effects:
		if effect == null:
			continue
		var key := _as_string(effect.get("key"))
		# Effect.StorageMethod：0=SUM；其余（SET/PERCENT 等）非增量语义，不导出
		if key == "" or _as_int(effect.get("storage_method")) != 0:
			continue
		deltas[key] = float(deltas.get(key, 0.0)) + _as_number(effect.get("value"))
	return deltas


func _weapon_chain(weapon) -> Array:
	var chain := []
	var current = weapon
	# 上限 16 为防御性循环引用保护；实际武器链最长 4 阶（1→4）
	while current != null and chain.size() < 16:
		var current_id := _as_string(current.get("my_id"))
		if current_id == "" or current_id in chain:
			break
		chain.append(current_id)
		current = current.get("upgrades_into")
	return chain


# ---- 取值/规范化辅助（防运行时资源字段类型漂移） ----

func _finish_entries(entries: Array) -> Array:
	var deduped := {}
	for entry in entries:
		var entry_id: String = entry.get("id", "")
		if entry_id == "" or deduped.has(entry_id):
			continue
		deduped[entry_id] = entry
	var out := []
	for entry_id in deduped:
		out.append(deduped[entry_id])
	out.sort_custom(self, "_compare_by_id")
	return out


func _compare_by_id(left, right) -> bool:
	return str(left.get("id", "")) < str(right.get("id", ""))


func _resource_id(resource) -> String:
	if resource == null:
		return ""
	return _as_string(resource.get("my_id"))


func _resource_ids(raw) -> Array:
	var out := []
	if raw is Array:
		for resource in raw:
			if resource != null:
				out.append(_resource_id(resource))
	return out


# banned_items / banned_upgrades：游戏内既有字符串 ID 也有资源引用，两种都接受。
func _id_list(raw) -> Array:
	var out := []
	if raw is Array:
		for item in raw:
			if item == null:
				continue
			if typeof(item) == TYPE_STRING:
				out.append(item)
			else:
				out.append(_resource_id(item))
	return out


func _translated_name(name_key) -> String:
	var key := _as_string(name_key)
	if key == "":
		return ""
	return TranslationServer.translate(key)


func _as_string(value) -> String:
	if typeof(value) == TYPE_STRING:
		return value
	return ""


func _as_int(value, fallback := 0) -> int:
	var value_type := typeof(value)
	if value_type == TYPE_INT or value_type == TYPE_REAL:
		return int(value)
	return fallback


func _as_number(value, fallback := 0.0) -> float:
	var value_type := typeof(value)
	if value_type == TYPE_INT or value_type == TYPE_REAL:
		return float(value)
	return fallback


func _as_bool(value) -> bool:
	if typeof(value) == TYPE_BOOL:
		return value
	return false


func _as_strings(raw) -> Array:
	var out := []
	if raw is Array:
		for item in raw:
			if typeof(item) == TYPE_STRING:
				out.append(item)
	return out


func _json_value(value, depth := 0):
	var value_type := typeof(value)
	if value_type == TYPE_NIL or depth > 4:
		return null
	if value_type == TYPE_BOOL or value_type == TYPE_INT or value_type == TYPE_REAL:
		return value
	if value_type == TYPE_STRING:
		return value
	if value_type == TYPE_ARRAY:
		var array_out := []
		for item in value:
			array_out.append(_json_value(item, depth + 1))
		return array_out
	if value_type == TYPE_DICTIONARY:
		var dict_out := {}
		for key in value:
			dict_out[str(key)] = _json_value(value[key], depth + 1)
		return dict_out
	return null
