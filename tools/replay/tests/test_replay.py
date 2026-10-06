"""replay.py：时间轴重放、--speed/--seek 与 CLI 测试。"""
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.replay.recording import iter_records, open_recording
from tools.replay.replay import main, replay
from tools.replay.tests.support import (
    decision_line,
    in_line,
    out_line,
    snapshot,
    write_recording,
)


class CollectingEngine:
    def __init__(self):
        self.messages = []

    def on_message(self, envelope):
        self.messages.append(envelope)


class FakeTime:
    """可注入的单调时钟：sleep 会推进时钟。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def timeline_records():
    wave = {"index": 1, "phase": "combat", "time_left": 20.0}
    return [
        in_line(100.0, "snapshot", {"t": 0.0, "wave": wave}),
        out_line(100.1, "move", vector=[1, 0]),
        in_line(100.5, "snapshot", {"t": 0.5, "wave": wave}),
        decision_line(100.6, "reflex", {"kind": "move"}, "测试"),
        in_line(101.0, "hello", {"protocol_version": 1}),
    ]


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ab-replay-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def make_recording(self, name="run.ndjson"):
        return write_recording(self.directory / name, timeline_records())

    def test_feeds_only_in_messages_in_order(self):
        engine = CollectingEngine()
        replay(open_recording(self.make_recording()), engine, speed=0)
        self.assertEqual(
            [message["type"] for message in engine.messages],
            ["snapshot", "snapshot", "hello"],
        )

    def test_seek_skips_earlier_events(self):
        engine = CollectingEngine()
        fed = replay(open_recording(self.make_recording()), engine, speed=0, seek=0.25)
        self.assertEqual(fed, 2)
        self.assertEqual(
            [message["type"] for message in engine.messages],
            ["snapshot", "hello"],
        )

    def test_seek_beyond_recording_feeds_nothing(self):
        engine = CollectingEngine()
        fed = replay(open_recording(self.make_recording()), engine, speed=0, seek=99.0)
        self.assertEqual(fed, 0)
        self.assertEqual(engine.messages, [])

    def test_speed_scales_inter_message_delays(self):
        fake = FakeTime()
        replay(
            open_recording(self.make_recording()),
            CollectingEngine(),
            speed=2.0,
            sleep=fake.sleep,
            clock=fake.clock,
        )
        self.assertEqual(len(fake.sleeps), 2)
        self.assertAlmostEqual(fake.sleeps[0], 0.25, places=6)
        self.assertAlmostEqual(fake.sleeps[1], 0.25, places=6)

    def test_speed_zero_never_sleeps(self):
        def forbidden(_seconds):
            raise AssertionError("speed=0 不应调用 sleep")

        fed = replay(
            open_recording(self.make_recording()),
            CollectingEngine(),
            speed=0,
            sleep=forbidden,
        )
        self.assertEqual(fed, 3)

    def test_header_only_recording(self):
        path = write_recording(self.directory / "empty.ndjson", [])
        fed = replay(open_recording(path), CollectingEngine(), speed=0)
        self.assertEqual(fed, 0)

    def test_cli_replay_reports_fed_count(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([str(self.make_recording()), "--speed", "0"])
        self.assertEqual(code, 0)
        self.assertIn("回放完成：3 条 in 消息", buffer.getvalue())

    def test_cli_rejects_negative_speed(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([str(self.make_recording()), "--speed", "-1"])
        self.assertEqual(code, 1)

    def test_cli_corrupt_late_line_returns_error(self):
        path = self.directory / "corrupt.ndjson"
        path.write_text(
            '{"kind":"header","ts":0,"agent_version":"0.1.0","mod_version":"0.2.0",'
            '"protocol_version":1,"game_version":"1.1.15.4"}\n'
            '{"kind":"in","ts":1.0}\n',
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            code = main([str(path), "--speed", "0"])
        self.assertEqual(code, 1)

    def test_iter_records_is_independent_of_full_load(self):
        path = self.make_recording()
        recording = open_recording(path)
        first_pass = [record.kind for record in iter_records(recording)]
        second_pass = [record.kind for record in iter_records(recording)]
        self.assertEqual(first_pass, second_pass)


if __name__ == "__main__":
    unittest.main()
