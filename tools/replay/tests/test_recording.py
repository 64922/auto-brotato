"""recording.py：录制解析、校验与统计测试。"""
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.replay.recording import (
    Recording,
    RecordingError,
    format_summary,
    iter_records,
    open_recording,
    summarize,
)
from tools.replay.replay import main
from tools.replay.tests.support import (
    HEADER,
    decision_line,
    in_line,
    out_line,
    snapshot,
    write_recording,
)


def sample_records():
    wave1 = {"index": 1, "phase": "combat", "time_left": 20.0}
    wave2 = {"index": 2, "phase": "combat", "time_left": 10.0}
    return [
        snapshot(100.0, wave1),
        snapshot(100.016, wave1),
        snapshot(100.032, wave1),
        out_line(100.05, "move", vector=[1, 0]),
        in_line(100.5, "ack", {"ok": True}, ref=1),
        snapshot(101.0, wave2),
        snapshot(101.016, wave2),
        in_line(101.2, "shop", {"wave_next": 3, "gold": 10}),
        in_line(101.3, "pong", {}),
        decision_line(102.0, "reflex", {"kind": "move"}, "远离弹幕"),
    ]


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ab-replay-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def path(self, name="run.ndjson"):
        return self.directory / name

    def test_iter_records_skips_header(self):
        path = write_recording(self.path(), sample_records())
        recording = open_recording(path)
        records = list(iter_records(recording))
        self.assertEqual(len(records), len(sample_records()))
        self.assertEqual(records[0].kind, "in")
        self.assertEqual(records[0].data["envelope"]["type"], "snapshot")

    def test_summary_counts_duration_waves_and_frequency(self):
        path = write_recording(self.path(), sample_records())
        summary = summarize(open_recording(path))
        self.assertEqual(summary.in_count, 8)
        self.assertEqual(summary.out_count, 1)
        self.assertEqual(summary.decision_count, 1)
        self.assertEqual(summary.line_count, 10)
        self.assertEqual(summary.in_by_type["snapshot"], 5)
        self.assertEqual(summary.in_by_type["ack"], 1)
        self.assertEqual(summary.in_by_type["shop"], 1)
        self.assertEqual(summary.in_by_type["pong"], 1)
        self.assertEqual(summary.out_by_kind, {"move": 1})
        self.assertEqual(summary.decision_by_layer, {"reflex": 1})
        self.assertAlmostEqual(summary.start_ts, 100.0)
        self.assertAlmostEqual(summary.end_ts, 102.0)
        self.assertAlmostEqual(summary.duration, 2.0)
        self.assertEqual((summary.wave_min, summary.wave_max), (1, 2))
        stats = summary.snapshot
        self.assertEqual(stats.count, 5)
        self.assertAlmostEqual(stats.hz, 4 / 1.016, places=3)
        self.assertAlmostEqual(stats.interval_ms_min, 16.0, places=3)
        self.assertAlmostEqual(stats.interval_ms_max, 968.0, places=3)
        self.assertAlmostEqual(stats.interval_ms_p50, 16.0, places=3)
        self.assertAlmostEqual(stats.interval_ms_p95, 16.0 + 952.0 * 0.85, places=1)

    def test_gzip_recording_is_readable(self):
        path = write_recording(self.path("run.ndjson.gz"), sample_records(), compress=True)
        summary = summarize(open_recording(path))
        self.assertEqual(summary.in_count, 8)

    def test_format_summary_contains_key_stats(self):
        path = write_recording(self.path(), sample_records())
        text = format_summary(summarize(open_recording(path)))
        self.assertIn("消息计数", text)
        self.assertIn("时长：2.0s", text)
        self.assertIn("波次范围：第 1–2 波", text)
        self.assertIn("快照频率", text)
        self.assertIn("snapshot=5", text)
        self.assertIn("move=1", text)

    def test_summary_cli(self):
        path = write_recording(self.path(), sample_records())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--summary", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("波次范围：第 1–2 波", buffer.getvalue())

    def test_open_empty_file_raises(self):
        path = self.path("empty.ndjson")
        path.write_text("", encoding="utf-8")
        with self.assertRaises(RecordingError):
            open_recording(path)

    def test_open_missing_header_raises(self):
        path = self.path("no-header.ndjson")
        path.write_text(
            json.dumps(in_line(1.0, "snapshot")) + "\n", encoding="utf-8"
        )
        with self.assertRaises(RecordingError) as caught:
            open_recording(path)
        self.assertIn("header", str(caught.exception))

    def test_iter_records_requires_header_first(self):
        path = self.path("no-header.ndjson")
        path.write_text(
            json.dumps(in_line(1.0, "snapshot")) + "\n", encoding="utf-8"
        )
        recording = Recording(path=path, header={})
        with self.assertRaises(RecordingError) as caught:
            list(iter_records(recording))
        self.assertIn("应为 header", str(caught.exception))

    def test_bad_json_line_raises_with_lineno(self):
        path = self.path("bad.ndjson")
        path.write_text(
            json.dumps(HEADER) + "\n" + "{broken\n", encoding="utf-8"
        )
        recording = open_recording(path)
        with self.assertRaises(RecordingError) as caught:
            list(iter_records(recording))
        self.assertIn("第 2 行", str(caught.exception))

    def test_unknown_kind_raises(self):
        path = write_recording(self.path(), [{"kind": "nope", "ts": 1.0}])
        recording = open_recording(path)
        with self.assertRaises(RecordingError) as caught:
            list(iter_records(recording))
        self.assertIn("未知 kind", str(caught.exception))

    def test_missing_envelope_raises(self):
        path = write_recording(self.path(), [{"kind": "in", "ts": 1.0}])
        with self.assertRaises(RecordingError) as caught:
            list(iter_records(open_recording(path)))
        self.assertIn("envelope", str(caught.exception))

    def test_decision_missing_reason_raises(self):
        path = write_recording(
            self.path(), [{"kind": "decision", "ts": 1.0, "layer": "reflex", "action": {}}]
        )
        with self.assertRaises(RecordingError) as caught:
            list(iter_records(open_recording(path)))
        self.assertIn("layer/reason", str(caught.exception))

    def test_header_only_recording_summary(self):
        path = write_recording(self.path(), [])
        summary = summarize(open_recording(path))
        self.assertEqual(summary.line_count, 0)
        self.assertEqual(summary.duration, 0.0)
        self.assertIsNone(summary.wave_min)
        self.assertEqual(summary.snapshot.count, 0)

    def test_cli_missing_file_returns_error(self):
        code = main([str(self.directory / "nope.ndjson"), "--summary"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
