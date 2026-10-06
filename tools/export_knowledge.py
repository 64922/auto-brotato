#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库导出收集工具（票据 11）。

连接 mod 的 IPC 会话（mod 为客户端、本工具为一次性服务端），下发
``debug_export_knowledge`` 调试动作，mod 从游戏运行时资源导出四类 JSON 到
``user://auto_brotato_knowledge/``；本工具校验 ack 文件哈希并按需复制到
仓库 ``docs/knowledge/``。

用法（conda 环境 brotato；先 tools/deploy_mod.ps1 部署并启动 Brotato）：
  python tools/export_knowledge.py                 # 导出两次校验稳定 + 复制入仓
  python tools/export_knowledge.py --repeat 1      # 只导出一次
  python tools/export_knowledge.py --no-copy       # 只触发与校验，不写仓库

退出码：0 全部通过；1 校验/复制失败；2 连接或握手失败。
"""
import argparse
import hashlib
import json
import select
import shutil
import socket
import sys
import time
from pathlib import Path

PROTOCOL_VERSION = 2
GAME_VERSION = "1.1.15.4"
KNOWLEDGE_FILES = ("items.json", "weapons.json", "upgrades.json", "characters.json")
ACTION_KIND = "debug_export_knowledge"
DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "knowledge"


class Link:
    """与 mod 的最小双向 NDJSON 链路（同 tools/smoke_mod.py）。"""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.seq = 0
        self.pending = []

    def send(self, msg_type, payload, ref=None):
        envelope = {
            "v": PROTOCOL_VERSION,
            "seq": self.seq,
            "ts": time.time(),
            "type": msg_type,
            "ref": ref,
            "payload": payload,
        }
        self.seq += 1
        line = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.sock.sendall(line.encode("utf-8"))

    def pump(self, timeout=0.05):
        readable, _, _ = select.select([self.sock], [], [], timeout)
        if not readable:
            return
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("mod 关闭了连接")
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(message, dict):
                self.pending.append(message)

    def drain(self):
        out, self.pending = self.pending, []
        return out


def wait_for_hello(link, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        link.pump(0.1)
        for message in link.drain():
            if message.get("type") == "hello":
                return message.get("payload", {})
    return None


def wait_for_ack(link, ref, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        link.pump(0.1)
        for message in link.drain():
            if message.get("type") == "ack" and message.get("ref") == ref:
                return message.get("payload", {})
    return None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def trigger_export(link, ref, timeout_seconds):
    """触发一次导出；返回 ack 载荷（已确认 ok）。"""
    link.send("action", {"kind": ACTION_KIND}, ref)
    ack = wait_for_ack(link, ref, timeout_seconds)
    if ack is None:
        raise RuntimeError("等待 %s 回执超时（%.0fs）" % (ACTION_KIND, timeout_seconds))
    if not ack.get("ok"):
        raise RuntimeError("导出失败：%s" % ack.get("error"))
    return ack


def collect_hashes(source_dir, ack):
    """校验 ack 声明的文件哈希；返回 {文件名: sha256}。"""
    summary = ack.get("counts", {})
    print(
        "[导出] 条目数：物品 %s · 武器 %s · 升级 %s · 英雄 %s · 套装 %s"
        % (
            summary.get("items"),
            summary.get("weapons"),
            summary.get("upgrades"),
            summary.get("characters"),
            summary.get("sets"),
        )
    )
    hashes = {}
    for name in KNOWLEDGE_FILES:
        path = source_dir / name
        if not path.is_file():
            raise RuntimeError("导出目录缺少文件：%s" % path)
        digest = sha256_file(path)
        declared = (ack.get("files") or {}).get(name, {}).get("sha256")
        if declared and declared.lower() != digest:
            raise RuntimeError("%s 哈希不一致（ack=%s 实际=%s）" % (name, declared, digest))
        hashes[name] = digest
        print("[校验] %s sha256=%s（%d 字节）" % (name, digest[:16], path.stat().st_size))
    data_versions = ack.get("data_versions") or {}
    for name in KNOWLEDGE_FILES:
        print("[版本] %s data_version=%s" % (name, data_versions.get(name)))
    return hashes


def run(args):
    source_dir = None
    declared_runs = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((args.host, args.port))
        server.listen(1)
        server.settimeout(1.0)
        print(
            "[监听] %s:%d，等待 mod 连接（最长 %.0fs）..."
            % (args.host, args.port, args.wait)
        )
        deadline = time.monotonic() + args.wait
        conn = None
        while time.monotonic() < deadline:
            try:
                conn, addr = server.accept()
                break
            except socket.timeout:
                continue
        if conn is None:
            print("[失败] 等待超时：mod 未连接（游戏是否已启动？）")
            return 2
        conn.settimeout(None)
        print("[连接] 来自 %s:%d" % addr)

        link = Link(conn)
        hello = wait_for_hello(link, 10.0)
        if hello is None:
            print("[失败] 未收到 hello")
            return 2
        print(
            "[握手] protocol=%s mod=%s game=%s"
            % (
                hello.get("protocol_version"),
                hello.get("mod_version"),
                hello.get("game_version"),
            )
        )
        if hello.get("protocol_version") != PROTOCOL_VERSION:
            print("[失败] 协议版本不匹配：%s（需要 %d）" % (hello.get("protocol_version"), PROTOCOL_VERSION))
            return 2
        if hello.get("game_version") != GAME_VERSION:
            print("[失败] 游戏版本不匹配：%s（锁定 %s）" % (hello.get("game_version"), GAME_VERSION))
            return 2
        link.send(
            "welcome",
            {
                "protocol_version": PROTOCOL_VERSION,
                "session_id": hello.get("session_id"),
                "config": {"snapshot_hz": 60, "debug_overlay": False, "action_ttl_ms": 250},
            },
        )

        for run_index in range(1, args.repeat + 1):
            try:
                ack = trigger_export(link, run_index, args.timeout)
                # mod 返回的用户目录绝对路径（同机运行）
                source_dir = Path(ack["dir"])
                print("[目录] user:// 导出目录 = %s" % source_dir)
                hashes = collect_hashes(source_dir, ack)
            except (RuntimeError, OSError, KeyError) as exc:
                print("[失败] 第 %d 次导出失败：%s" % (run_index, exc))
                return 1
            declared_runs.append(hashes)
            print("[重复] 第 %d/%d 次导出完成" % (run_index, args.repeat))

        if len(declared_runs) > 1 and any(run != declared_runs[0] for run in declared_runs[1:]):
            print("[失败] 重复导出结果不一致：%s" % declared_runs)
            return 1
        if len(declared_runs) > 1:
            print("[稳定] %d 次导出逐字节一致" % len(declared_runs))

        if args.no_copy:
            print("[完成] 未复制（--no-copy）；源目录 = %s" % source_dir)
            return 0

        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in KNOWLEDGE_FILES:
            target = out_dir / name
            shutil.copyfile(source_dir / name, target)
            copied = sha256_file(target)
            if copied != declared_runs[0][name]:
                print("[失败] 复制后哈希不一致：%s" % target)
                return 1
            print("[复制] %s -> %s" % (name, target))
        print("[完成] 知识库已写入 %s" % out_dir)
        return 0
    finally:
        server.close()


def main():
    parser = argparse.ArgumentParser(description="AutoBrotato 知识库导出收集（票据 11）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=37650)
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR),
        help="输出目录（默认 <仓库>/docs/knowledge）",
    )
    parser.add_argument("--repeat", type=int, default=2, help="导出次数（>1 校验结果稳定，默认 2）")
    parser.add_argument("--wait", type=float, default=180.0, help="等待 mod 连接的最长秒数")
    parser.add_argument("--timeout", type=float, default=30.0, help="单次导出的回执超时秒数")
    parser.add_argument("--no-copy", action="store_true", help="只触发与校验，不复制到仓库")
    args = parser.parse_args()
    if args.repeat < 1:
        print("[失败] --repeat 至少为 1")
        return 2
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130
    except (ConnectionError, OSError) as exc:
        print("[失败] %s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
