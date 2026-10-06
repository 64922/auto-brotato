#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoBrotato mod 冒烟客户端（票据 01）。

最简 TCP 工具（仅标准库），替代尚未实现的 agent（票据 02），用于验收：
  1. hello/welcome 握手（协议 v2，game_version 1.1.15.4）；
  2. snapshot 上送速率与字段 sanity；
  3. （可选）注入 move 动作验证实机位移；
  4. 停止注入后 TTL（默认 250ms）内安全停住。

用法（conda 环境 brotato）：
  python tools/smoke_mod.py --seconds 12
  python tools/smoke_mod.py --seconds 20 --move 1,0 --move-seconds 3 --await-combat

退出码：0 全部通过；1 存在失败项；2 连接/握手失败。
"""
import argparse
import json
import select
import socket
import sys
import time

PROTOCOL_VERSION = 2
GAME_VERSION = "1.1.15.4"
MOVE_SEND_INTERVAL = 0.08
RAMP_SECONDS = 0.5
TTL_MARGIN_SECONDS = 0.35


class Link:
    """mod 主动连出后的最小双向 NDJSON 链路。"""

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


def positions_during(samples, start, end):
    return [(t, x, y) for (t, x, y) in samples if start <= t <= end]


def average_speed(samples):
    if len(samples) < 2:
        return 0.0, (0.0, 0.0)
    t0, x0, y0 = samples[0]
    t1, x1, y1 = samples[-1]
    dt = t1 - t0
    if dt <= 0:
        return 0.0, (0.0, 0.0)
    return ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 / dt, ((x1 - x0) / dt, (y1 - y0) / dt)


def max_speed(samples):
    peak = 0.0
    for (t0, x0, y0), (t1, x1, y1) in zip(samples, samples[1:]):
        dt = t1 - t0
        if dt > 0:
            peak = max(peak, ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 / dt)
    return peak


def run_smoke(args):
    checks = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((args.host, args.port))
        server.listen(1)
        server.settimeout(1.0)
        print("[监听] %s:%d，等待 mod 连接（最长 %.0fs）..." % (args.host, args.port, args.wait))
        deadline = time.monotonic() + args.wait
        conn = None
        while time.monotonic() < deadline:
            try:
                conn, addr = server.accept()
                break
            except socket.timeout:
                continue
        if conn is None:
            print("[失败] 等待超时：mod 未连接")
            return 2
        conn.settimeout(None)
        print("[连接] 来自 %s:%d" % addr)

        link = Link(conn)
        hello = wait_for_hello(link, 10.0)
        if hello is None:
            print("[失败] 未收到 hello")
            return 2
        print(
            "[握手] hello: protocol=%s mod=%s game=%s session=%s"
            % (
                hello.get("protocol_version"),
                hello.get("mod_version"),
                hello.get("game_version"),
                hello.get("session_id"),
            )
        )
        handshake_ok = (
            hello.get("protocol_version") == PROTOCOL_VERSION
            and hello.get("game_version") == GAME_VERSION
        )
        checks.append(("hello 版本字段", handshake_ok, "protocol=%s game=%s" % (
            hello.get("protocol_version"), hello.get("game_version"))))
        link.send(
            "welcome",
            {
                "protocol_version": PROTOCOL_VERSION,
                "session_id": hello.get("session_id"),
                "config": {"snapshot_hz": 60, "debug_overlay": False, "action_ttl_ms": 250},
            },
        )
        print("[握手] welcome 已发送（snapshot_hz=60, action_ttl_ms=250）")

        move_vector = None
        if args.move:
            parts = args.move.split(",")
            if len(parts) != 2:
                print("[失败] --move 需要 'dx,dy' 格式")
                return 2
            move_vector = (float(parts[0]), float(parts[1]))

        if move_vector and args.await_combat:
            print("[等待] 等待进入战斗（player 存在且 wave.phase=combat，最长 %.0fs）..." % args.await_timeout)
            deadline = time.monotonic() + args.await_timeout
            combat = False
            while time.monotonic() < deadline:
                link.pump(0.05)
                for message in link.drain():
                    if message.get("type") != "snapshot":
                        continue
                    payload = message.get("payload") or {}
                    wave = payload.get("wave")
                    if payload.get("player") and wave and wave.get("phase") == "combat":
                        combat = True
                if combat:
                    break
            if not combat:
                print("[失败] 等待进入战斗超时（未观测到存活玩家/战斗相位）")
                return 1
            print("[等待] 已进入战斗，开始注入时序")

        t0 = time.monotonic()
        if move_vector:
            baseline_end = t0 + args.baseline
            move_end = baseline_end + args.move_seconds
            ttl_end = move_end + TTL_MARGIN_SECONDS + 1.0
            end = max(t0 + args.seconds, ttl_end + 0.3)
        else:
            baseline_end = move_end = ttl_end = None
            end = t0 + args.seconds

        snapshots = []
        positions = []
        alive_samples = []
        acks = []
        snap_count = 0
        next_move_at = baseline_end if move_vector else None
        ref = 1
        phase = "baseline" if move_vector else "observe"
        wave_seen = None

        print("[观测] 共运行 %.1fs%s" % (
            end - t0, "（基线 %.1fs → 注入 %.1fs → 停住观测 %.1fs）" % (
                args.baseline, args.move_seconds, ttl_end - move_end) if move_vector else ""))

        while time.monotonic() < end:
            now = time.monotonic()
            if move_vector and phase == "baseline" and now >= baseline_end:
                phase = "move"
                print("[移动] 开始注入 move=(%.2f, %.2f)，每 %.0fms 一次" % (
                    move_vector[0], move_vector[1], MOVE_SEND_INTERVAL * 1000))
            if move_vector and phase == "move" and now >= move_end:
                phase = "stop"
                print("[停住] 停止注入，观测 TTL 内是否停住")
            if move_vector and phase == "move" and now >= next_move_at:
                link.send("action", {"kind": "move", "vector": [move_vector[0], move_vector[1]]}, ref)
                ref += 1
                next_move_at = now + MOVE_SEND_INTERVAL

            link.pump(0.02)
            for message in link.drain():
                msg_type = message.get("type")
                payload = message.get("payload") or {}
                if msg_type == "snapshot":
                    snap_count += 1
                    snapshots.append((time.monotonic(), payload))
                    wave = payload.get("wave")
                    if wave:
                        wave_seen = wave
                    player = payload.get("player")
                    if player:
                        pos = player.get("pos")
                        if isinstance(pos, list) and len(pos) == 2:
                            positions.append((time.monotonic(), float(pos[0]), float(pos[1])))
                        alive_samples.append((time.monotonic(), player.get("alive"), player.get("hp")))
                elif msg_type == "ack":
                    acks.append(payload)
                elif msg_type == "error":
                    print("[警告] 收到 error：%s" % payload)

        if len(snapshots) >= 2:
            span = snapshots[-1][0] - snapshots[0][0]
            hz = (len(snapshots) - 1) / span if span > 0 else 0.0
        else:
            hz = 0.0
        checks.append(("snapshot 上送", len(snapshots) >= 10 and hz >= 45.0, "%d 条 / %.1fs = %.1fHz" % (
            len(snapshots), snapshots[-1][0] - snapshots[0][0] if len(snapshots) >= 2 else 0.0, hz)))
        print("[观测] snapshot %d 条，平均 %.1fHz" % (len(snapshots), hz))
        if wave_seen:
            last_alive = alive_samples[-1] if alive_samples else None
            print("[快照] wave=%s phase=%s time_left=%s player=%s" % (
                wave_seen.get("index"), wave_seen.get("phase"), wave_seen.get("time_left"),
                "有(hp=%s, alive=%s)" % (last_alive[2], last_alive[1]) if last_alive else "无"))
        else:
            print("[快照] 未处于对局（wave=None）")
        for check in checks:
            print("[检查] %s: %s（%s）" % (check[0], "PASS" if check[1] else "FAIL", check[2]))

        failed = not all(c[1] for c in checks)

        if move_vector:
            baseline_positions = positions_during(positions, t0, baseline_end)
            ramp_start = baseline_end + RAMP_SECONDS if move_end - baseline_end > RAMP_SECONDS else baseline_end
            move_positions = positions_during(positions, ramp_start, move_end)
            stop_positions = positions_during(positions, move_end + TTL_MARGIN_SECONDS, ttl_end)

            if not baseline_positions or not move_positions:
                print("[移动] FAIL：对局中未观测到玩家（不在战斗阶段？）")
                failed = True
            else:
                speed, direction = average_speed(move_positions)
                norm = (move_vector[0] ** 2 + move_vector[1] ** 2) ** 0.5
                unit = (move_vector[0] / norm, move_vector[1] / norm) if norm > 0 else (0.0, 0.0)
                dot = direction[0] * unit[0] + direction[1] * unit[1]
                cos = dot / speed if speed > 0 else 0.0
                displacement = (
                    (move_positions[-1][1] - move_positions[0][1]) ** 2
                    + (move_positions[-1][2] - move_positions[0][2]) ** 2
                ) ** 0.5
                move_ok = speed > 20.0 and cos > 0.7 and displacement > 10.0
                print("[移动] %s：位移 %.1fpx，平均 %.1fpx/s，方向 cos=%.2f" % (
                    "PASS" if move_ok else "FAIL", displacement, speed, cos))
                failed = failed or not move_ok

            if not stop_positions:
                print("[停住] FAIL：停住窗口内无玩家观测")
                failed = True
            else:
                peak = max_speed(stop_positions)
                stop_alive = [a for (t, a, _hp) in alive_samples
                              if move_end + TTL_MARGIN_SECONDS <= t <= ttl_end]
                alive_ok = bool(stop_alive) and all(a is not False for a in stop_alive)
                stop_ok = peak < 8.0 and alive_ok
                print("[停住] %s：TTL 后最大速度 %.1fpx/s（< 8 视为停住），停住窗口内玩家存活=%s" % (
                    "PASS" if stop_ok else "FAIL", peak,
                    "是" if alive_ok else "否（判定无效）"))
                failed = failed or not stop_ok

            move_acks = [a for a in acks]
            if move_acks:
                ack_ok = all(a.get("ok") for a in move_acks)
                print("[回执] 收到 %d 条 ack，%s" % (len(move_acks), "全部 ok" if ack_ok else "存在失败"))
                failed = failed or not ack_ok

        print("[结论] %s" % ("PASS" if not failed else "FAIL"))
        return 0 if not failed else 1
    finally:
        server.close()


def main():
    parser = argparse.ArgumentParser(description="AutoBrotato mod 冒烟客户端（票据 01）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=37650)
    parser.add_argument("--seconds", type=float, default=10.0, help="观测总时长（秒）")
    parser.add_argument("--wait", type=float, default=180.0, help="等待 mod 连接的最长秒数")
    parser.add_argument("--move", default=None, help="注入的移动向量，如 '1,0'；给出后启用移动/TTL 验证")
    parser.add_argument("--move-seconds", type=float, default=3.0, help="注入持续秒数")
    parser.add_argument("--baseline", type=float, default=2.0, help="注入前基线观测秒数")
    parser.add_argument("--await-combat", action="store_true", help="等待进入战斗后再开始注入时序（配合 --move）")
    parser.add_argument("--await-timeout", type=float, default=180.0, help="等待进入战斗的最长秒数")
    args = parser.parse_args()
    try:
        return run_smoke(args)
    except KeyboardInterrupt:
        return 130
    except (ConnectionError, OSError) as exc:
        print("[失败] %s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
