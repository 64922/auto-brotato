"""recorder.py 单元测试 + 与 IpcServer 的端到端录制测试。"""
import gzip
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from ab_agent.ipc_server import IpcServer
from ab_agent.recorder import Recorder, recording_filename
from ab_agent.state import AgentState

from tests.test_ipc_server import QUIET, FakeMod, wait_for

HELLO = {
    "protocol_version": 1,
    "mod_version": "0.2.0",
    "game_version": "1.1.15.4",
    "session_id": "session-rec",
    "capabilities": {"snapshot_hz": 60, "move_analog": True},
}


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ab-recorder-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def make(self, **kwargs):
        return Recorder(self.directory, **kwargs)

    def read_lines(self, path):
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_header_fields_and_default_filename(self):
        recorder = self.make()
        path = recorder.start_session(hello=HELLO, agent_version="0.1.0")
        recorder.end_session()
        self.assertRegex(path.name, r"^\d{8}-\d{6}-unknown-unknown\.ndjson$")
        lines = self.read_lines(path)
        self.assertEqual(len(lines), 1)
        header = lines[0]
        self.assertEqual(header["kind"], "header")
        self.assertEqual(header["agent_version"], "0.1.0")
        self.assertEqual(header["mod_version"], "0.2.0")
        self.assertEqual(header["protocol_version"], 1)
        self.assertEqual(header["game_version"], "1.1.15.4")
        self.assertEqual(header["session_id"], "session-rec")
        self.assertIsNone(header["hero_id"])
        self.assertIsNone(header["weapons"])
        self.assertIsNone(header["difficulty"])
        self.assertIsNone(header["window"])
        self.assertIsInstance(header["ts"], float)

    def test_records_in_out_and_decision_lines(self):
        recorder = self.make()
        path = recorder.start_session(hello=HELLO, agent_version="0.1.0")
        recorder.record_in({"v": 1, "seq": 0, "ts": 1.0, "type": "snapshot", "ref": None, "payload": {"t": 0.5}})
        recorder.record_out({"v": 1, "seq": 0, "ts": 1.1, "type": "action", "ref": 1, "payload": {"kind": "move"}})
        recorder.record_decision("reflex", {"kind": "move"}, "远离弹幕")
        recorder.end_session()
        lines = self.read_lines(path)
        self.assertEqual([line["kind"] for line in lines], ["header", "in", "out", "decision"])
        self.assertEqual(lines[1]["envelope"]["type"], "snapshot")
        self.assertEqual(lines[2]["envelope"]["payload"]["kind"], "move")
        self.assertEqual(lines[3]["layer"], "reflex")
        self.assertEqual(lines[3]["reason"], "远离弹幕")
        for line in lines:
            self.assertIsInstance(line["ts"], float)

    def test_hero_difficulty_weapons_in_header_and_filename(self):
        recorder = self.make()
        path = recorder.start_session(
            hello=HELLO,
            agent_version="0.1.0",
            hero_id="character_ranger",
            weapons=[{"id": "pistol", "tier": 1}],
            difficulty=2,
        )
        recorder.end_session()
        self.assertIn("character_ranger-D2", path.name)
        header = self.read_lines(path)[0]
        self.assertEqual(header["hero_id"], "character_ranger")
        self.assertEqual(header["difficulty"], 2)
        self.assertEqual(header["weapons"], [{"id": "pistol", "tier": 1}])

    def test_compress_writes_gzip(self):
        recorder = self.make(compress=True)
        path = recorder.start_session(hello=HELLO, agent_version="0.1.0")
        recorder.record_in({"type": "snapshot", "payload": {"t": 1.0}})
        recorder.end_session()
        self.assertTrue(path.name.endswith(".ndjson.gz"))
        lines = self.read_lines(path)
        self.assertEqual([line["kind"] for line in lines], ["header", "in"])

    def test_fixed_clock_sessions_do_not_overwrite(self):
        clock = lambda: 1759690000.0
        recorder = self.make(clock=clock)
        first = recorder.start_session(hello=HELLO, agent_version="0.1.0")
        recorder.end_session()
        second = recorder.start_session(hello=HELLO, agent_version="0.1.0")
        recorder.end_session()
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())

    def test_start_failure_degrades_gracefully(self):
        occupied = self.directory / "not-a-dir"
        occupied.write_text("occupied", encoding="utf-8")
        recorder = Recorder(occupied)
        path = recorder.start_session(hello=HELLO, agent_version="0.1.0")
        self.assertIsNone(path)
        recorder.record_in({"type": "snapshot"})
        recorder.end_session()

    def test_records_after_end_are_dropped(self):
        recorder = self.make()
        path = recorder.start_session(hello=HELLO, agent_version="0.1.0")
        recorder.end_session()
        recorder.record_in({"type": "snapshot"})
        recorder.record_decision("reflex", None, "迟到的决策")
        self.assertEqual(len(self.read_lines(path)), 1)

    def test_bulk_records_are_flushed_on_end(self):
        recorder = self.make()
        path = recorder.start_session(hello=HELLO, agent_version="0.1.0")
        for index in range(300):
            recorder.record_in({"type": "snapshot", "payload": {"t": index, "blob": "x" * 512}})
        recorder.end_session()
        lines = self.read_lines(path)
        self.assertEqual(len(lines), 301)
        self.assertEqual(lines[-1]["envelope"]["payload"]["t"], 299)

    def test_queue_overflow_drops_and_counts_instead_of_blocking(self):
        recorder = self.make(queue_max=2)
        gate = threading.Event()
        recorder._drain = lambda: gate.wait(5)
        recorder.start_session(hello=HELLO, agent_version="0.1.0")
        for index in range(10):
            recorder.record_in({"type": "snapshot", "payload": {"t": index}})
        self.assertGreaterEqual(recorder.dropped, 1)
        gate.set()
        recorder.end_session()

    def test_filename_slug_handles_unknown_and_string_difficulty(self):
        self.assertIn("-unknown-unknown.ndjson", recording_filename(0, None, None))
        self.assertIn("-h-D0.ndjson", recording_filename(0, "h", 0))
        self.assertIn("-character_ranger-D3.ndjson.gz", recording_filename(0, "character_ranger", "D3", compress=True))


class RecorderIpcIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_server_records_complete_session(self):
        directory = Path(tempfile.mkdtemp(prefix="ab-recorder-ipc-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        recorder = Recorder(directory)
        state = AgentState()
        server = IpcServer(
            state, host="127.0.0.1", port=0, ping_interval=0, logger=QUIET, recorder=recorder
        )
        await server.start()
        self.addAsyncCleanup(server.shutdown)

        mod = await FakeMod.connect(server)
        welcome = await mod.hello(session_id="rec-session")
        self.assertEqual(welcome["type"], "welcome")
        path = recorder.recording_path
        self.assertIsNotNone(path)

        await mod.send(
            "snapshot",
            {"t": 1.0, "wave": {"index": 2, "phase": "combat"}, "player": {"alive": True, "hp": 10}},
        )
        self.assertTrue(await wait_for(lambda: state.snapshot_count == 1))

        state.observe_only = False
        ref = await server.send_action("move", {"vector": [1, 0]})
        action = await mod.recv()
        self.assertEqual(action["type"], "action")
        self.assertEqual(action["ref"], ref)
        await mod.send("ack", {"ok": True}, ref=ref)
        self.assertTrue(await wait_for(lambda: state.last_ack is not None))

        mod.close()
        self.assertTrue(await wait_for(lambda: recorder.recording_path is None, timeout=2.0))

        with open(path, "rt", encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        kinds = [line["kind"] for line in lines]
        self.assertEqual(kinds[0], "header")
        self.assertEqual(lines[0]["session_id"], "rec-session")
        in_types = [line["envelope"]["type"] for line in lines if line["kind"] == "in"]
        self.assertIn("hello", in_types)
        self.assertIn("snapshot", in_types)
        self.assertIn("ack", in_types)
        out_lines = [line for line in lines if line["kind"] == "out"]
        self.assertEqual(len(out_lines), 1)
        self.assertEqual(out_lines[0]["envelope"]["payload"], {"kind": "move", "vector": [1, 0]})

    async def test_server_without_recorder_still_works(self):
        state = AgentState()
        server = IpcServer(state, host="127.0.0.1", port=0, ping_interval=0, logger=QUIET)
        await server.start()
        self.addAsyncCleanup(server.shutdown)
        mod = await FakeMod.connect(server)
        welcome = await mod.hello()
        self.assertEqual(welcome["type"], "welcome")
        mod.close()


if __name__ == "__main__":
    unittest.main()
