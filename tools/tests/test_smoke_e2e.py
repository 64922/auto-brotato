"""tools/smoke_e2e.py 测试：回放分析（纯文件）与冒烟编排（FakeAgent）。

用带 ref 的合成录制覆盖验收证据链；编排测试用脚本化 FakeAgent，
不依赖游戏与真实进程。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.replay.tests.support import in_line, out_line, snapshot, write_recording
from tools.smoke_agent import EOF
from tools.smoke_analysis import FAIL, PASS, SKIP, analyze_recording
from tools.smoke_e2e import format_result, run_smoke

HEADER_V2 = {
    "kind": "header",
    "ts": 99.0,
    "agent_version": "0.1.0",
    "mod_version": "0.2.0",
    "protocol_version": 2,
    "game_version": "1.1.15.4",
    "session_id": "smoke-test",
    "hero_id": None,
    "weapons": None,
    "difficulty": None,
    "window": None,
}


def hello(ts=100.0):
    return in_line(
        ts,
        "hello",
        {
            "protocol_version": 2,
            "mod_version": "0.2.0",
            "game_version": "1.1.15.4",
            "session_id": "smoke-test",
        },
    )


def menu_difficulty(ts=100.5):
    return in_line(
        ts,
        "menu",
        {
            "phase": "difficulty_select",
            "character": {"id": "character_ranger"},
            "difficulty": {"selected": "difficulty_0", "options": []},
            "modes": {"endless": False, "ban": False, "coop": False},
            "can_start": True,
        },
    )


def shop(ts, wave_next):
    return in_line(
        ts,
        "shop",
        {"wave_next": wave_next, "gold": 10, "slots": [], "can_leave": True},
    )


def snapshots(ts0, count, wave, step=1 / 60):
    return [
        snapshot(ts0 + index * step, {"index": wave, "phase": "combat", "time_left": 20.0})
        for index in range(count)
    ]


def move_actions(ts0, count, ref_base=100):
    records = []
    for index in range(count):
        ref = ref_base + index
        records.append(out_line(ts0 + index * 0.12, "move", ref=ref, vector=[1, 0]))
        records.append(in_line(ts0 + 0.05 + index * 0.12, "ack", {"ok": True}, ref=ref))
    return records


def happy_records(include_level_up=True):
    records = [
        hello(),
        menu_difficulty(),
        out_line(101.0, "menu_set_difficulty", ref=1, value=0),
        in_line(101.2, "ack", {"ok": True}, ref=1),
        out_line(101.5, "menu_start_run", ref=2),
        in_line(102.0, "ack", {"ok": True}, ref=2),
        *snapshots(102.5, 120, wave=1),
        shop(105.0, 2),
        out_line(105.2, "shop_leave", ref=3),
        in_line(105.4, "ack", {"ok": True}, ref=3),
        *snapshots(106.0, 120, wave=2),
        shop(109.0, 3),
    ]
    if include_level_up:
        records.insert(
            11,
            in_line(
                108.5,
                "menu",
                {
                    "phase": "level_up",
                    "wave": 1,
                    "options": [
                        {
                            "slot": 1,
                            "kind": "upgrade",
                            "id": "upgrade_percent_damage_1",
                            "tier": 1,
                            "can_pick": True,
                        }
                    ],
                },
            ),
        )
        records.insert(12, out_line(108.6, "menu_pick_upgrade", ref=4, index=1))
        records.insert(13, in_line(108.8, "ack", {"ok": True}, ref=4))
    records.append(out_line(109.2, "shop_leave", ref=5))
    records.append(in_line(109.4, "ack", {"ok": True}, ref=5))
    records.append(
        snapshot(
            109.5,
            {"index": 2, "phase": "combat", "time_left": 5.0},
            economy={"gold": 42, "materials_this_wave": 3},
            inventory={
                "weapons": [{"slot": 1, "id": "weapon_pistol", "tier": 1}],
                "items": [{"id": "item_helmet", "count": 2}],
            },
        )
    )
    records.extend(move_actions(110.0, 10))
    return records


class AnalyzeRecordingTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ab-smoke-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def write(self, records, name="smoke.ndjson", header=None):
        return write_recording(
            self.directory / name, records, header=header or HEADER_V2
        )

    def status(self, analysis, name):
        for check in analysis.checks:
            if check.name == name:
                return check.status
        self.fail("缺少检查项：%s" % name)

    def test_happy_path_all_checks_pass(self):
        path = self.write(happy_records())
        analysis = analyze_recording(path, difficulty=0)
        self.assertTrue(analysis.checks, "应产出检查项")
        self.assertNotIn(FAIL, [check.status for check in analysis.checks], analysis.checks)
        self.assertEqual(self.status(analysis, "升级选卡（出现时）"), PASS)
        self.assertEqual(self.status(analysis, "第 2 波结束"), PASS)
        self.assertEqual(self.status(analysis, "快照链路（≥45Hz）"), PASS)
        self.assertIn("快照频率", analysis.summary_text)
        self.assertTrue(analysis.timeline)

    def test_battle_summary_from_last_snapshot(self):
        analysis = analyze_recording(self.write(happy_records()), difficulty=0)
        battle = analysis.battle
        self.assertIsNotNone(battle)
        self.assertEqual(battle.result, "stopped")
        self.assertEqual(battle.max_wave, 2)
        self.assertEqual(battle.gold, 42)
        self.assertEqual([weapon["id"] for weapon in battle.weapons], ["weapon_pistol"])
        self.assertEqual([item["id"] for item in battle.items], ["item_helmet"])

    def test_missing_shop_leave_fails(self):
        records = [
            record
            for record in happy_records()
            if not (
                (record["kind"] == "out" and record["envelope"]["payload"]["kind"] == "shop_leave")
                or (
                    record["kind"] == "in"
                    and record["envelope"]["type"] == "ack"
                    and record["envelope"]["ref"] in (3, 5)
                )
            )
        ]
        analysis = analyze_recording(self.write(records), difficulty=0)
        self.assertEqual(self.status(analysis, "第 1 波结束 → 商店自动离开"), FAIL)

    def test_level_up_absent_is_skip(self):
        analysis = analyze_recording(self.write(happy_records(include_level_up=False)), difficulty=0)
        self.assertEqual(self.status(analysis, "升级选卡（出现时）"), SKIP)

    def test_death_before_wave_two_passes(self):
        records = [
            hello(),
            menu_difficulty(),
            out_line(101.0, "menu_set_difficulty", ref=1, value=0),
            in_line(101.2, "ack", {"ok": True}, ref=1),
            out_line(101.5, "menu_start_run", ref=2),
            in_line(102.0, "ack", {"ok": True}, ref=2),
            *snapshots(102.5, 120, wave=1),
            *move_actions(104.5, 10),
            in_line(
                105.0,
                "menu",
                {"phase": "run_end", "result": "defeat", "wave": 1, "stats": {}},
            ),
        ]
        analysis = analyze_recording(self.write(records), difficulty=0)
        self.assertEqual(self.status(analysis, "第 2 波结束"), SKIP)
        self.assertNotIn(FAIL, [check.status for check in analysis.checks], analysis.checks)
        self.assertIsNotNone(analysis.battle)
        self.assertEqual(analysis.battle.result, "defeat")

    def test_version_mismatch_fails(self):
        header = dict(HEADER_V2, protocol_version=1)
        analysis = analyze_recording(self.write(happy_records(), header=header), difficulty=0)
        self.assertEqual(self.status(analysis, "握手与版本（协议 v2 / 游戏 1.1.15.4）"), FAIL)

    def test_corrupt_recording_fails_cleanly(self):
        path = self.directory / "broken.ndjson"
        path.write_text("{not-json}\n", encoding="utf-8")
        analysis = analyze_recording(path, difficulty=0)
        self.assertEqual(self.status(analysis, "回放文件可解析"), FAIL)

    def test_wrong_difficulty_value_fails(self):
        analysis = analyze_recording(self.write(happy_records()), difficulty=1)
        self.assertEqual(self.status(analysis, "难度设置（menu_set_difficulty）"), FAIL)


class FakeAgent:
    """按 SubprocessAgent 形状实现的脚本化 agent（测试专用）。"""

    def __init__(self, lines):
        self._lines = list(lines)
        self.sent = []
        self.closed = False
        self.exit_code = 0

    def read_line(self, timeout):
        if self._lines:
            return self._lines.pop(0)
        return EOF

    def send_line(self, text):
        self.sent.append(text)

    def close(self, timeout=20.0):
        self.send_line("quit")
        self.closed = True
        return self.exit_code


PROMPT_LINES = [
    "agent> 检测到难度选择页：",
    "  英雄：character_ranger（游侠）",
    "  初始武器：weapon_pistol_1（weapon_pistol·T1）",
    "  可选难度：D0–D1",
    "  模式开关：无尽=关 · 禁用=关 · 合作=关",
    "  当前选择：D0",
    "请输入难度（如 D0，回车重打印、q 取消）：",
]


class RunSmokeTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ab-smoke-run-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def make_recording(self, records=None, name="smoke.ndjson"):
        path = write_recording(
            self.directory / name, records or happy_records(), header=HEADER_V2
        )
        return path

    def run_with(self, lines):
        path = self.make_recording()
        agent = FakeAgent(["12:00:00 INFO    [ab.recorder] 开始录制：%s" % path] + lines)
        output = []
        result = run_smoke(
            agent,
            difficulty=0,
            record_root=self.directory,
            menu_timeout=1.0,
            start_timeout=1.0,
            run_timeout=1.0,
            output=output.append,
        )
        return agent, result, output

    def test_happy_flow_passes_and_auto_inputs_difficulty(self):
        agent, result, _output = self.run_with(
            [
                "19:00:00 INFO    [ab.cli] AutoBrotato agent v0.1.0 已启动，监听 127.0.0.1:37650",
                "19:00:00 INFO    [ab.ipc] 首条快照：未在对局（wave=null）",  # 不得被误判为开局失败
            ]
            + PROMPT_LINES
            + [
                "[开始] 难度读回校验通过（D0），发送开始对局",
                "[对局] 已进入第 1 波（D0）；占位走位接管（正式策略见票据 09/10）",
                "[商店] 下一波=2 · 金币=10 · 商品 4 件 → 固定动作：离开",
                "[升级] 第 1 波 · 4 张卡：1=upgrade_percent_damage_1",
                "[升级] 已选择第 1 项",
                "[商店] 下一波=3 · 金币=20 · 商品 4 件 → 固定动作：离开",
            ]
        )
        self.assertTrue(result.passed, [check for check in result.checks if check.failed])
        self.assertEqual(agent.sent, ["D0", "quit"])
        self.assertTrue(
            all(check.status == PASS for check in result.flow_checks), result.flow_checks
        )
        report = format_result(result)
        self.assertIn("[冒烟战报]", report)
        self.assertIn("主动停止（完成第 2 波）", report)
        self.assertIn("金币：42", report)

    def test_start_timeout_is_detected(self):
        agent, result, _output = self.run_with(
            PROMPT_LINES + ["[开始] 已发送开始，但未在 10s 内观测到第 1 波战斗"]
        )
        self.assertFalse(result.passed)
        start_check = next(
            check for check in result.flow_checks if check.name == "开局序列（读回校验 → 进入对局）"
        )
        self.assertEqual(start_check.status, FAIL)
        self.assertIn("观测到第 1 波战斗", start_check.detail)

    def test_modes_on_is_rejected(self):
        lines = list(PROMPT_LINES)
        lines[4] = "  模式开关：无尽=开 · 禁用=开 · 合作=关"
        agent, result, _output = self.run_with(lines)
        self.assertFalse(result.passed)
        self.assertNotIn("D0", agent.sent)
        self.assertIn("模式开关检查", [check.name for check in result.flow_checks])
        mode_check = next(c for c in result.flow_checks if c.name == "模式开关检查")
        self.assertEqual(mode_check.status, FAIL)

    def test_death_report_is_recognized(self):
        path = self.make_recording(
            [
                hello(),
                menu_difficulty(),
                out_line(101.0, "menu_set_difficulty", ref=1, value=0),
                in_line(101.2, "ack", {"ok": True}, ref=1),
                out_line(101.5, "menu_start_run", ref=2),
                in_line(102.0, "ack", {"ok": True}, ref=2),
                *snapshots(102.5, 120, wave=1),
                *move_actions(104.5, 10),
                in_line(105.0, "menu", {"phase": "run_end", "result": "defeat", "wave": 1, "stats": {}}),
            ]
        )
        agent = FakeAgent(
            ["开始录制：%s" % path]
            + PROMPT_LINES
            + [
                "[开始] 难度读回校验通过（D0），发送开始对局",
                "[对局] 已进入第 1 波（D0）；占位走位接管",
                "========== 对局战报 ==========",
                "备注：未收到 run_end，按死亡兜底",
            ]
        )
        result = run_smoke(
            agent,
            difficulty=0,
            record_root=self.directory,
            menu_timeout=1.0,
            start_timeout=1.0,
            run_timeout=1.0,
            output=lambda _line: None,
        )
        death_check = next(c for c in result.flow_checks if c.name == "终局/死亡检测")
        self.assertEqual(death_check.status, PASS)
        self.assertIn("死亡兜底", death_check.detail)
        self.assertTrue(result.passed, [check for check in result.checks if check.failed])

    def test_agent_eof_before_menu_fails(self):
        path = self.make_recording()
        agent = FakeAgent(["开始录制：%s" % path])
        result = run_smoke(
            agent,
            difficulty=0,
            record_root=self.directory,
            menu_timeout=0.1,
            start_timeout=0.1,
            run_timeout=0.1,
            output=lambda _line: None,
        )
        self.assertFalse(result.passed)
        menu_check = next(c for c in result.flow_checks if c.name == "难度页检测与提示")
        self.assertEqual(menu_check.status, FAIL)


if __name__ == "__main__":
    unittest.main()
