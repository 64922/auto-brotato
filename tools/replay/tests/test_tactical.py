"""tools/replay/tactical 测试：录制事实、采集/无效移动口径、保守区间与报告可复现。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.replay import tactical, tactical_metrics
from tools.replay.tests import support

ARENA = {"min": [0.0, 0.0], "max": [2048.0, 1536.0]}


def player(x=1024.0, y=768.0, *, hp=100.0, alive=True, vel=(0.0, 0.0)):
    return {
        "pos": [x, y],
        "vel": list(vel),
        "hp": hp,
        "max_hp": 100.0,
        "alive": alive,
    }


def material(x, y):
    return {"kind": "material", "pos": [x, y]}


def enemy(x, y, *, radius=20.0):
    return {"id": 1, "pos": [x, y], "vel": [0.0, 0.0], "radius": radius}


def snapshot_line(ts, *, wave=1, hp=100.0, x=1024.0, y=768.0, pickups=(), enemies=(), materials=0):
    return support.snapshot(
        ts,
        wave={"index": wave, "phase": "combat"},
        player=player(x, y, hp=hp),
        enemies=[dict(item) for item in enemies],
        projectiles=[],
        hazards=[],
        pickups=[dict(item) for item in pickups],
        arena=ARENA,
        inventory={"weapons": [], "items": []},
        economy={"gold": 0, "materials_this_wave": materials},
    )


def make_step(ts, *, vector=None, conservative=False, snapshot=None):
    return tactical_metrics.TacticalStep(
        ts=ts,
        snapshot=snapshot or {"player": {"pos": [1024.0, 768.0], "alive": True}},
        vector=vector,
        conservative=conservative,
    )


class EvaluateTest(unittest.TestCase):
    def test_approach_and_waste_fractions(self):
        steps = [
            tactical_metrics.TacticalStep(
                ts=0.0,
                snapshot={"player": {"pos": [0.0, 0.0], "vel": [0.0, 0.0], "alive": True},
                          "pickups": [material(300.0, 0.0)], "enemies": [], "hazards": [], "projectiles": []},
                vector=(1.0, 0.0),
            ),
            tactical_metrics.TacticalStep(
                ts=0.1,
                snapshot={"player": {"pos": [0.0, 0.0], "vel": [0.0, 0.0], "alive": True},
                          "pickups": [material(0.0, 300.0)], "enemies": [], "hazards": [], "projectiles": []},
                vector=(1.0, 0.0),
            ),
            tactical_metrics.TacticalStep(
                ts=0.2,
                snapshot={"player": {"pos": [0.0, 0.0], "vel": [0.0, 0.0], "alive": True},
                          "pickups": [], "enemies": [enemy(0.0, 100.0)], "hazards": [], "projectiles": []},
                vector=(0.0, -1.0),
            ),
        ]
        metrics = tactical_metrics.evaluate(steps)
        self.assertEqual(metrics.material_steps, 2)
        self.assertAlmostEqual(metrics.approach_fraction, 0.5)
        self.assertAlmostEqual(metrics.waste_fraction, 1.0 / 3.0)

    def test_conservative_intervals(self):
        steps = [
            make_step(0.0, conservative=False),
            make_step(1.0, conservative=True),
            make_step(2.0, conservative=True),
            make_step(3.0, conservative=False),
            make_step(4.0, conservative=True),
        ]
        intervals = tactical_metrics.conservative_intervals(steps)
        self.assertEqual(len(intervals), 2)
        self.assertEqual((intervals[0].start_ts, intervals[0].end_ts), (1.0, 2.0))
        self.assertEqual((intervals[1].start_ts, intervals[1].end_ts), (4.0, 4.0))


class FactsTest(unittest.TestCase):
    def test_materials_curve_takes_per_wave_peak(self):
        steps = [
            make_step(0.0, snapshot={"wave": {"index": 1}, "economy": {"materials_this_wave": 2}}),
            make_step(1.0, snapshot={"wave": {"index": 1}, "economy": {"materials_this_wave": 7}}),
            make_step(2.0, snapshot={"wave": {"index": 2}, "economy": {"materials_this_wave": 5}}),
        ]
        self.assertEqual(tactical_metrics.materials_curve(steps), ((1, 7), (2, 5)))

    def test_damage_attribution_finds_nearby_enemy(self):
        steps = [
            make_step(
                0.0,
                snapshot={
                    "wave": {"index": 3},
                    "player": {"pos": [0.0, 0.0], "hp": 100.0, "alive": True},
                    "enemies": [enemy(100.0, 0.0)],
                    "projectiles": [],
                    "hazards": [],
                },
            ),
            make_step(
                2.5,
                snapshot={
                    "wave": {"index": 3},
                    "player": {"pos": [0.0, 0.0], "hp": 80.0, "alive": True},
                    "enemies": [enemy(140.0, 0.0)],
                    "projectiles": [],
                    "hazards": [],
                },
            ),
        ]
        events = tactical_metrics.damage_attribution(steps)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0].amount, 20.0)
        self.assertEqual(events[0].wave, 3)
        self.assertAlmostEqual(events[0].nearest_enemy_px, 100.0 - 20.0 - 10.0)
        self.assertEqual(events[0].enemies_near, 1)


class OutcomeTest(unittest.TestCase):
    def _recording(self, tmp, *, death=False, materials=(4, 9)):
        path = Path(tmp) / "rec.ndjson"
        lines = [
            snapshot_line(1000.0, wave=1, materials=materials[0]),
            snapshot_line(1001.0, wave=2, materials=materials[1]),
        ]
        if death:
            lines.append(snapshot_line(1002.0, wave=2, hp=0.0))
            lines[-1]["envelope"]["payload"]["player"]["alive"] = False
        support.write_recording(path, lines)
        return path

    def test_outcome_summarizes_waves_materials_and_death(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._recording(tmp, death=True)
            outcome = tactical_metrics.recording_outcome(tactical.open_recording(path))
            self.assertEqual(outcome.max_wave, 2)
            self.assertEqual(outcome.materials_by_wave, ((1, 4), (2, 9)))
            self.assertEqual(outcome.total_materials, 13)
            self.assertAlmostEqual(outcome.death_ts, 1002.0)
            self.assertEqual(outcome.death_wave, 2)

    def test_no_death_when_never_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._recording(tmp)
            outcome = tactical_metrics.recording_outcome(tactical.open_recording(path))
            self.assertIsNone(outcome.death_ts)


class CollectAndReportTest(unittest.TestCase):
    def _recording(self, tmp, *, waves=(1, 2)):
        path = Path(tmp) / "rec.ndjson"
        lines = [
            snapshot_line(
                1000.0 + index * 0.05,
                wave=wave,
                materials=index * 2,
                pickups=[material(1324.0, 768.0)],
                enemies=[enemy(1900.0, 1400.0)],
            )
            for index, wave in enumerate(waves)
        ]
        support.write_recording(path, lines)
        return path

    def test_collect_exposes_tactical_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._recording(tmp)
            recording = tactical.open_recording(path)
            controller = tactical.build_engines(None, None, "none", 0)[0][1]
            steps = tactical_metrics.collect(recording, controller)
            self.assertEqual(len(steps), 2)
            self.assertIsNotNone(steps[0].vector)
            self.assertEqual(steps[0].rhythm, "early")

    def test_build_engines_baselines(self):
        names = [name for name, _ in tactical.build_engines(None, None, "reflex", 0)]
        self.assertEqual(names, ["战术层", "仅反射层"])
        names = [name for name, _ in tactical.build_engines(None, None, "placeholder", 0)]
        self.assertEqual(names, ["战术层", "占位基线"])
        self.assertEqual(len(tactical.build_engines(None, None, "none", 0)), 1)

    def test_main_json_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._recording(tmp)
            outputs = []
            for _ in range(2):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = tactical.main([str(path), "--json"])
                self.assertEqual(code, 0)
                outputs.append(buffer.getvalue())
            self.assertEqual(outputs[0], outputs[1])
            payload = json.loads(outputs[0])
            self.assertEqual(payload["runs"][0]["name"], "战术层")
            self.assertEqual(len(payload["runs"]), 2)
            self.assertEqual(payload["materials_by_wave"], [[1, 0], [2, 2]])
            self.assertEqual(payload["damage_events"], [])

    def test_main_compare_with(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._recording(tmp, waves=(1, 2))
            second = Path(tmp) / "other.ndjson"
            support.write_recording(
                second,
                [
                    snapshot_line(2000.0, wave=1, materials=3),
                    snapshot_line(2001.0, wave=2, materials=6),
                    snapshot_line(2002.0, wave=3, materials=2),
                ],
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = tactical.main([str(first), "--compare-with", str(second), "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["compare"]["base"]["total_materials"], 2)
            self.assertEqual(payload["compare"]["other"]["total_materials"], 11)
            self.assertEqual(payload["compare"]["other"]["max_wave"], 3)

    def test_main_text_report_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._recording(tmp)
            outputs = []
            for _ in range(2):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = tactical.main([str(path)])
                self.assertEqual(code, 0)
                outputs.append(buffer.getvalue())
            self.assertEqual(outputs[0], outputs[1])
            self.assertIn("回放战术层报告", outputs[0])
            self.assertIn("仅反射层", outputs[0])
            self.assertIn("低血保守模式", outputs[0])

    def test_main_missing_file(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = tactical.main(["no-such-file.ndjson"])
        self.assertEqual(code, 1)
        self.assertIn("错误", buffer.getvalue())

    def test_main_bad_compare_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._recording(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                code = tactical.main([str(path), "--compare-with", "no-such-file.ndjson"])
            self.assertEqual(code, 1)
            self.assertIn("对比录制", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
