"""测试用录制文件构造辅助。"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

HEADER = {
    "kind": "header",
    "ts": 999.0,
    "agent_version": "0.1.0",
    "mod_version": "0.2.0",
    "protocol_version": 1,
    "game_version": "1.1.15.4",
    "session_id": "test-session",
    "hero_id": None,
    "weapons": None,
    "difficulty": None,
    "window": None,
}


def envelope(msg_type, payload=None, ref=None):
    return {
        "v": 1,
        "seq": 0,
        "ts": 1000.0,
        "type": msg_type,
        "ref": ref,
        "payload": {} if payload is None else payload,
    }


def in_line(ts, msg_type, payload=None, ref=None):
    return {"kind": "in", "ts": ts, "envelope": envelope(msg_type, payload, ref)}


def out_line(ts, kind, ref=None, **params):
    payload = {"kind": kind}
    payload.update(params)
    return {"kind": "out", "ts": ts, "envelope": envelope("action", payload, ref)}


def decision_line(ts, layer, action, reason):
    return {"kind": "decision", "ts": ts, "layer": layer, "action": action, "reason": reason}


def snapshot(ts, wave=None, **extra):
    payload = {"t": ts - 1000.0, "wave": wave, "player": {"alive": True}}
    payload.update(extra)
    return in_line(ts, "snapshot", payload)


def write_recording(path, records, header=None, *, compress=False):
    lines = [header if header is not None else HEADER] + list(records)
    text = "\n".join(json.dumps(line, ensure_ascii=False, separators=(",", ":")) for line in lines) + "\n"
    path = Path(path)
    if compress or path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path
