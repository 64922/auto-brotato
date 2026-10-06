"""movement.py 测试：指标口径、收集与报告可复现性。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.replay import movement
from tools.replay.tests import support


ARENA = {"min": [0.0, 0.0], "max": [2048.0, 1536.0]}


def make_step(ts, *, pos=(1024.0, 768.0), vel=(0.0, 0.0), vector=None, hp=100.0, wave=1, enemies=()):
    snapshot = {
        "wave": {"index": wave, "phase": "combat"},
        "player": {"pos": list(pos), "vel": list(vel), "hp": hp, "max_hp": 100.0, "alive": True},
        "enemies": list(enemies),
        "projectiles": [],
        "hazards": [],
        "arena": ARENA,
    }
    parsed = None
    if vector is not None:
        parsed = (float(vector[0]), float(vector[1]))
    return movement.Step(ts=ts, snapshot=snapshot, vector=parsed)


def enemy(x, y, *, radius=20.0, vel=(0.0, 0.0)):
    return {"pos": [x, y], "vel": list(vel), "radius": radius}


class FakeEngine:
    def __init__(self, vectors):
        self._vectors = list(vectors)
        self.resets = 0

    def reset(self):
        self.resets += 1

    def next_move(self, snapshot, now):
        if not self._vectors:
            return None
        return self._vectors.pop(0)


class CollectTest(unittest.TestCase):
    def test_collect_feeds_snapshots_and_skips_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rec.ndjson"
            support.write_recording(
                path,
                [
                    support.in_line(1000.0, "hello", {"protocol_version": 2}),
                    support.snapshot(1001.0, wave={"index": 1}, player={"pos": [1, 2], "alive": True}),
                    support.snapshot(1002.0, wave={"index": 1}, player={"pos": [3, 4], "alive": True}),
                ],
            )
            recording = movement.open_recording(path)
            engine = FakeEngine([[1.0, 0.0], None])
            steps = movement.collect(recording, engine)
            self.assertEqual(engine.resets, 1)
            self.assertEqual([step.vector for step in steps], [(1.0, 0.0), None])

    def test_collect_honors_max_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rec.ndjson"
            supports = [
                support.snapshot(1000.0 + index, wave={"index": 1}, player={"pos": [1, 2], "alive": True})
                for index in range(5)
            ]
            support.write_recording(path, supports)
            recording = movement.open_recording(path)
            steps = movement.collect(recording, FakeEngine([None] * 5), max_seconds=2.0)
            self.assertEqual(len(steps), 3)


class EvaluateTest(unittest.TestCase):
    def test_exposure_edge_and_sectors(self):
        steps = [
            make_step(0.0, pos=(10.0, 10.0), vector=(1.0, 0.0), enemies=[enemy(500.0, 10.0)]),
            make_step(0.1, pos=(10.0, 10.0), vector=(1.0, 0.0), enemies=[enemy(300.0, 10.0)]),
        ]
        metrics = movement.evaluate(steps)
        self.assertEqual(metrics.steps, 2)
        self.assertEqual(metrics.decisions, 2)
        self.assertEqual(metrics.sector_counts[0], 2)
        self.assertEqual(metrics.flips_per_s, 0.0)
        self.assertEqual(metrics.mean_turn_deg, 0.0)
        self.assertEqual(metrics.edge_fraction, 1.0)
        self.assertAlmostEqual(metrics.mean_magnitude, 1.0)
        self.assertGreater(metrics.exposure_mean, 0.0)
        self.assertEqual(metrics.exposed_fraction, 0.0)

    def test_exposed_when_prediction_inside_threat(self):
        steps = [
            make_step(0.0, pos=(10.0, 10.0), vector=(1.0, 0.0), enemies=[enemy(145.0, 10.0, radius=40.0)]),
        ]
        metrics = movement.evaluate(steps)
        self.assertEqual(metrics.exposed_fraction, 1.0)
        self.assertLess(metrics.clearance_min, 0.0)
        self.assertAlmostEqual(metrics.exposure_mean, 1.0)

    def test_flip_and_turn_counts(self):
        steps = [
            make_step(0.0, vector=(1.0, 0.0)),
            make_step(0.1, vector=(0.0, 1.0)),
            make_step(0.2, vector=(0.0, 1.0)),
        ]
        metrics = movement.evaluate(steps)
        self.assertEqual(metrics.decisions, 3)
        self.assertEqual(metrics.flips_per_s, 5.0)
        self.assertAlmostEqual(metrics.mean_turn_deg, 45.0)

    def test_holds_reuse_last_vector(self):
        steps = [
            make_step(0.0, pos=(10.0, 10.0), vector=(1.0, 0.0), enemies=[enemy(500.0, 10.0)]),
            make_step(0.1, pos=(10.0, 10.0), vector=None, enemies=[enemy(500.0, 10.0)]),
        ]
        metrics = movement.evaluate(steps)
        self.assertEqual(metrics.decisions, 1)
        self.assertEqual(metrics.steps, 2)


class ClearanceTest(unittest.TestCase):
    def test_projectile_horizon_clamped_by_ttl(self):
        snapshot = {
            "player": {"pos": [0.0, 0.0]},
            "enemies": [],
            "hazards": [],
            "projectiles": [
                {"pos": [300.0, 0.0], "vel": [-600.0, 0.0], "radius": 16.0, "ttl": 0.1, "friendly": False}
            ],
        }
        # ttl=0.1 时弹幕在 x=240；对预测点 (45,0) 的 clearance = 195-16-10
        self.assertAlmostEqual(movement._clearance(snapshot, (45.0, 0.0)), 169.0)


class DamageTest(unittest.TestCase):
    def test_hp_drops_counted_within_wave(self):
        steps = [
            make_step(0.0, hp=100.0),
            make_step(0.1, hp=90.0),
            make_step(0.2, hp=90.0),
            make_step(0.3, hp=85.0),
            make_step(0.4, hp=10.0, wave=2),
        ]
        events, total = movement.recording_damage(steps)
        self.assertEqual(events, 2)
        self.assertAlmostEqual(total, 15.0)


class EngineAndReportTest(unittest.TestCase):
    def _recording(self, tmp):
        path = Path(tmp) / "rec.ndjson"
        steps = []
        for index in range(20):
            steps.append(
                support.snapshot(
                    1000.0 + index * 0.05,
                    wave={"index": 1},
                    player={"pos": [1024.0, 768.0], "vel": [0.0, 0.0], "hp": 100.0, "max_hp": 100.0, "alive": True},
                    enemies=[enemy(1324.0, 768.0)],
                    projectiles=[],
                    hazards=[],
                    pickups=[],
                    arena=ARENA,
                    inventory={"weapons": [], "items": []},
                    economy={"gold": 0, "materials_this_wave": 0},
                )
            )
        support.write_recording(path, steps)
        return path

    def test_report_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._recording(tmp)
            recording = movement.open_recording(path)
            first_runs = [
                (name, movement.collect(recording, engine))
                for name, engine in movement.build_engines(None, 0, "placeholder")
            ]
            second_runs = [
                (name, movement.collect(recording, engine))
                for name, engine in movement.build_engines(None, 0, "placeholder")
            ]
            first = movement.build_report(path, first_runs, note="x")
            second = movement.build_report(path, second_runs, note="x")
            self.assertEqual(first, second)
            self.assertIn("回放走位指标报告", first)
            self.assertIn("占位基线", first)

    def test_main_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._recording(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = movement.main([str(path), "--json", "--max-seconds", "0.5"])
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["baseline"], "placeholder")
            self.assertEqual(len(payload["runs"]), 2)
            self.assertEqual(len(payload["runs"][0]["metrics"]["sector_counts"]), 8)

    def test_main_missing_file(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = movement.main(["no-such-file.ndjson"])
        self.assertEqual(code, 1)
        self.assertIn("错误", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
