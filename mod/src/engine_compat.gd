extends Reference

# 引擎兼容小工具（ADR-0006）。
#
# JSON 解析把数字统一为 float，协议中声明的整数字段（slot/index/tier/price）需要
# 收敛校验；独立模块避免 shop_observation/shop_actions 重复实现。

static func int_arg(value) -> int:
	var value_type := typeof(value)
	if value_type == TYPE_INT:
		return value
	if value_type == TYPE_REAL:
		var number := float(value)
		if number == floor(number):
			return int(number)
	return -1
