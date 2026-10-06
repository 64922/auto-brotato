"""protocol.py 单元测试：NDJSON 信封编解码与握手版本校验。"""
import json
import unittest

from ab_agent import protocol
from ab_agent.protocol import Envelope, ProtocolError


class ParseLineTest(unittest.TestCase):
    def test_parse_valid_envelope(self):
        line = json.dumps(
            {"v": 1, "seq": 7, "ts": 123.5, "type": "snapshot", "ref": None, "payload": {"wave": {"index": 1}}}
        )
        envelope = protocol.parse_line(line)
        self.assertIsInstance(envelope, Envelope)
        self.assertEqual(envelope.v, 1)
        self.assertEqual(envelope.seq, 7)
        self.assertEqual(envelope.ts, 123.5)
        self.assertEqual(envelope.type, "snapshot")
        self.assertIsNone(envelope.ref)
        self.assertEqual(envelope.payload, {"wave": {"index": 1}})

    def test_parse_keeps_ref_and_int_ts(self):
        envelope = protocol.parse_line(
            json.dumps({"v": 1, "seq": 0, "ts": 42, "type": "ack", "ref": 17, "payload": {"ok": True}})
        )
        self.assertEqual(envelope.ref, 17)
        self.assertEqual(envelope.ts, 42.0)

    def test_parse_string_ref(self):
        envelope = protocol.parse_line(
            json.dumps({"v": 1, "seq": 0, "ts": 0, "type": "action", "ref": "a-1", "payload": {}})
        )
        self.assertEqual(envelope.ref, "a-1")

    def test_tolerates_missing_payload_as_empty(self):
        envelope = protocol.parse_line(
            json.dumps({"v": 1, "seq": 0, "ts": 0, "type": "pong", "ref": None})
        )
        self.assertEqual(envelope.payload, {})

    def test_as_dict_roundtrips_and_keeps_payload_reference(self):
        envelope = protocol.parse_line(
            json.dumps({"v": 1, "seq": 3, "ts": 9.5, "type": "snapshot", "ref": None, "payload": {"a": 1}})
        )
        data = envelope.as_dict()
        self.assertEqual(
            data,
            {"v": 1, "seq": 3, "ts": 9.5, "type": "snapshot", "ref": None, "payload": {"a": 1}},
        )
        self.assertIs(data["payload"], envelope.payload)

    def test_rejects_blank_line(self):
        with self.assertRaises(ProtocolError):
            protocol.parse_line("   ")

    def test_rejects_invalid_json(self):
        with self.assertRaises(ProtocolError):
            protocol.parse_line("{not json")

    def test_rejects_non_object(self):
        with self.assertRaises(ProtocolError):
            protocol.parse_line("[1, 2, 3]")

    def test_rejects_missing_type(self):
        with self.assertRaises(ProtocolError):
            protocol.parse_line(json.dumps({"v": 1, "seq": 0, "ts": 0, "payload": {}}))

    def test_rejects_non_object_payload(self):
        with self.assertRaises(ProtocolError):
            protocol.parse_line(json.dumps({"v": 1, "seq": 0, "ts": 0, "type": "x", "payload": 3}))

    def test_rejects_missing_seq(self):
        with self.assertRaises(ProtocolError):
            protocol.parse_line(json.dumps({"v": 1, "ts": 0, "type": "x", "payload": {}}))

    def test_rejects_bool_seq(self):
        with self.assertRaises(ProtocolError):
            protocol.parse_line(json.dumps({"v": 1, "seq": True, "ts": 0, "type": "x", "payload": {}}))


class EncodeTest(unittest.TestCase):
    def test_encode_roundtrip(self):
        data = protocol.encode("welcome", {"session_id": "s1"}, seq=0, ref=None, ts=1.5)
        self.assertTrue(data.endswith(b"\n"))
        envelope = protocol.parse_line(data.decode("utf-8"))
        self.assertEqual(envelope.type, "welcome")
        self.assertEqual(envelope.seq, 0)
        self.assertEqual(envelope.ts, 1.5)
        self.assertEqual(envelope.payload, {"session_id": "s1"})
        self.assertEqual(envelope.v, protocol.PROTOCOL_VERSION)

    def test_encode_default_payload_is_object(self):
        envelope = protocol.parse_line(protocol.encode("ping", seq=3).decode("utf-8"))
        self.assertEqual(envelope.payload, {})
        self.assertEqual(envelope.seq, 3)

    def test_encode_keeps_ref(self):
        envelope = protocol.parse_line(protocol.encode("action", {"kind": "move"}, seq=1, ref=9).decode("utf-8"))
        self.assertEqual(envelope.ref, 9)


class HelloCheckTest(unittest.TestCase):
    HELLO = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "mod_version": "0.2.0",
        "game_version": protocol.GAME_VERSION,
        "session_id": "4042ab31d9aaf1e4",
        "capabilities": {"snapshot_hz": 60, "move_analog": True},
    }

    def test_accepts_matching_versions(self):
        self.assertIsNone(protocol.check_hello(dict(self.HELLO)))

    def test_rejects_protocol_mismatch_with_clear_message(self):
        hello = dict(self.HELLO, protocol_version=2)
        error = protocol.check_hello(hello)
        self.assertIsNotNone(error)
        self.assertIn("protocol_version", error)
        self.assertIn("2", error)

    def test_rejects_game_mismatch_with_clear_message(self):
        hello = dict(self.HELLO, game_version="1.1.15.5")
        error = protocol.check_hello(hello)
        self.assertIsNotNone(error)
        self.assertIn("game_version", error)
        self.assertIn("1.1.15.4", error)

    def test_rejects_missing_versions(self):
        error = protocol.check_hello({})
        self.assertIsNotNone(error)
        self.assertIn("protocol_version", error)
        self.assertIn("game_version", error)


class PayloadBuildersTest(unittest.TestCase):
    def test_welcome_payload_defaults(self):
        payload = protocol.welcome_payload("s1")
        self.assertEqual(payload["protocol_version"], protocol.PROTOCOL_VERSION)
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(
            payload["config"],
            {"snapshot_hz": 60, "debug_overlay": False, "action_ttl_ms": 250},
        )

    def test_welcome_payload_does_not_alias_default_config(self):
        payload = protocol.welcome_payload("s1")
        payload["config"]["snapshot_hz"] = 30
        self.assertEqual(protocol.DEFAULT_WELCOME_CONFIG["snapshot_hz"], 60)

    def test_error_payload(self):
        payload = protocol.error_payload("version_mismatch", "版本不匹配")
        self.assertEqual(payload["code"], "version_mismatch")
        self.assertIn("版本不匹配", payload["message"])


if __name__ == "__main__":
    unittest.main()
