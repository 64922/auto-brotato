"""tools/verify_knowledge.py 纯函数解析测试（票据 11）。"""
import unittest

from tools.verify_knowledge import (
    _same_number,
    parse_ext_refs,
    parse_ext_resources,
    parse_scalars,
    parse_service_arrays,
    sample_ids,
)

SERVICE_TSCN = """[gd_scene load_steps=4 format=2]

[ext_resource path="res://weapons/ranged/pistol/1/pistol_data.tres" type="Resource" id=2]
[ext_resource path="res://items/all/mushroom/mushroom_data.tres" type="Resource" id=11]
[ext_resource path="res://items/upgrades/attack_speed/1/attack_speed_data.tres" type="Resource" id=79]
[ext_resource path="res://items/characters/ranger/ranger_data.tres" type="Resource" id=57]

[node name="ItemService" type="Node"]
script = ExtResource( 1 )
items = [ ExtResource( 11 ) ]
weapons = [ ExtResource( 2 ) ]
upgrades = [ ExtResource( 79 ) ]
characters = [ ExtResource( 57 ) ]
"""

TRES = """[gd_resource type="Resource" load_steps=3 format=2]

[ext_resource path="res://weapons/ranged/pistol/1/pistol_stats.tres" type="Resource" id=4]
[ext_resource path="res://weapons/ranged/pistol/2/pistol_2_data.tres" type="Resource" id=5]

[resource]
script = ExtResource( 1 )
my_id = "weapon_pistol_1"
tier = 0
value = 10
stats = ExtResource( 4 )
upgrades_into = ExtResource( 5 )
can_be_looted = true
"""


class ParserTest(unittest.TestCase):
    def test_parse_ext_resources(self):
        mapping = parse_ext_resources(TRES)
        self.assertEqual(mapping[4], "res://weapons/ranged/pistol/1/pistol_stats.tres")
        self.assertEqual(mapping[5], "res://weapons/ranged/pistol/2/pistol_2_data.tres")

    def test_parse_service_arrays(self):
        arrays = parse_service_arrays(SERVICE_TSCN)
        self.assertEqual(arrays["items"], ["res://items/all/mushroom/mushroom_data.tres"])
        self.assertEqual(arrays["weapons"], ["res://weapons/ranged/pistol/1/pistol_data.tres"])
        self.assertEqual(
            arrays["upgrades"],
            ["res://items/upgrades/attack_speed/1/attack_speed_data.tres"],
        )
        self.assertEqual(arrays["characters"], ["res://items/characters/ranger/ranger_data.tres"])

    def test_parse_scalars(self):
        scalars = parse_scalars(TRES)
        self.assertEqual(scalars["my_id"], "weapon_pistol_1")
        self.assertEqual(scalars["tier"], 0)
        self.assertEqual(scalars["value"], 10)
        self.assertIs(scalars["can_be_looted"], True)

    def test_parse_scalars_float(self):
        scalars = parse_scalars('[resource]\naccuracy = 0.9\ncrit_damage = 2.0\n')
        self.assertAlmostEqual(scalars["accuracy"], 0.9)
        self.assertAlmostEqual(scalars["crit_damage"], 2.0)

    def test_parse_ext_refs(self):
        refs = parse_ext_refs(TRES)
        self.assertEqual(refs["stats"], 4)
        self.assertEqual(refs["upgrades_into"], 5)


class SampleTest(unittest.TestCase):
    def test_sample_deterministic(self):
        ids = ["id_%02d" % index for index in range(100)]
        first = sample_ids(ids, 10)
        second = sample_ids(ids, 10)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 10)
        self.assertEqual(sorted(first), first)

    def test_sample_all_when_small(self):
        self.assertEqual(sample_ids(["a", "b"], 5), ["a", "b"])


class NumberTest(unittest.TestCase):
    def test_same_number(self):
        self.assertTrue(_same_number(10, 10.0))
        self.assertTrue(_same_number(0.9, 0.9))
        self.assertFalse(_same_number(10, 11))
        self.assertFalse(_same_number(True, 1))


if __name__ == "__main__":
    unittest.main()
