"""会话状态机：难度交互 → 自动开局 → 对局（战术层走位 + 固定商店/升级动作）→ 终局战报。

状态（架构 §4）：``IDLE → MENU_READY → AWAIT_INPUT → STARTING → RUNNING → ENDED → IDLE``。
其中 ``MENU_READY`` 为 OBSERVE_ONLY 下已看到难度页、等待 ``resume`` 接管的等待位；
``ENDED`` 为打印战报的当轮，下一 tick 回到 ``IDLE``。

职责：
- 读 ``AgentState`` 观测，识别难度页/终局页；
- 开局序列：``menu_set_difficulty`` → 读回选中值 → ``menu_start_run`` → 等第 1 波；
- 对局内动作（move / shop_leave / menu_pick_upgrade）委托 ``RunController``（走位为
  战术层+反射层，商店/升级仍为固定动作，正式策略见票据 11–12），终局战报由控制器记账汇总；
- 终端输入由 CLI 转交 ``handle_input``（难度 D0–Dn、模式确认 y/n、q 取消）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from enum import Enum
from typing import Callable, Optional

from .menu_view import DifficultyMenu, RunEndMenu, difficulty_my_id
from .protocol import ack_error, ack_ok
from .run_control import RunController
from .state import AgentState

#: 动作回执与读回/开局等待（协议 §5.2：mod 侧各动作最长 1.5s）
ACK_TIMEOUT_S = 2.0
READBACK_TIMEOUT_S = 1.5
START_TIMEOUT_S = 10.0

#: 死亡兜底：玩家 alive=false 持续该秒数仍未收到 run_end 则按战败收尾
DEATH_FALLBACK_S = 3.0

_DIFFICULTY_RE = re.compile(r"[dD](\d+)")


class Phase(str, Enum):
    IDLE = "IDLE"
    MENU_READY = "MENU_READY"
    AWAIT_INPUT = "AWAIT_INPUT"
    CONFIRM = "CONFIRM"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    ENDED = "ENDED"


class RunSession:
    """单局流程状态机（单线程 asyncio 内使用）。"""

    def __init__(
        self,
        state: AgentState,
        server,
        *,
        output: Callable[[str], None] = print,
        clock: Callable[[], float] = time.monotonic,
        replay_path: Optional[Callable[[], Optional[str]]] = None,
        autopilot=None,
        ack_timeout_s: float = ACK_TIMEOUT_S,
        readback_timeout_s: float = READBACK_TIMEOUT_S,
        start_timeout_s: float = START_TIMEOUT_S,
        death_fallback_s: float = DEATH_FALLBACK_S,
    ) -> None:
        self.state = state
        self.server = server
        self.phase = Phase.IDLE
        self.difficulty: Optional[DifficultyMenu] = None
        self._out = output
        self._clock = clock
        self._ack_timeout_s = ack_timeout_s
        self._readback_timeout_s = readback_timeout_s
        self._start_timeout_s = start_timeout_s
        self._death_fallback_s = death_fallback_s
        self._controller = RunController(
            server,
            output=output,
            clock=clock,
            autopilot=autopilot,
            replay_path=replay_path,
            ack_timeout_s=ack_timeout_s,
        )

        self._connected_seen = False
        self._prompted_signature: Optional[str] = None
        self._suppressed_signature: Optional[str] = None
        self._observe_hint_signature: Optional[str] = None
        self._confirm_value: Optional[int] = None
        self._start_task: Optional[asyncio.Task] = None
        self._run_started_at: Optional[float] = None
        self._run_finished = False
        self._death_since: Optional[float] = None

    # ---- 终端输入 ----

    def handle_input(self, text: str) -> bool:
        """处理一条终端输入；返回 False 表示不属于本状态机（交给 CLI 命令）。"""
        text = text.strip()
        if self.phase is Phase.AWAIT_INPUT:
            self._handle_difficulty_input(text)
            return True
        if self.phase is Phase.CONFIRM:
            self._handle_confirm_input(text)
            return True
        return False

    def _handle_difficulty_input(self, text: str) -> None:
        if not text:
            self._reprint_prompt()
            return
        if text.lower() == "q":
            self._cancel_to_idle()
            return
        match = _DIFFICULTY_RE.fullmatch(text)
        if match is None:
            self._print("无法识别的输入：%r。请输入 D0–Dn 形式（如 D0）。" % text)
            self._reprint_prompt()
            return
        value = int(match.group(1))
        if self.difficulty is None or not self.difficulty.is_unlocked(value):
            unlocked = self.difficulty.unlocked_range_text() if self.difficulty else "未知"
            self._print("D%d 超出当前已解锁范围（%s）。" % (value, unlocked))
            self._reprint_prompt()
            return
        if self.difficulty.modes_on():
            self._confirm_value = value
            self.phase = Phase.CONFIRM
            self._print(self.difficulty.format_confirm_prompt(value))
            return
        self._begin_start(value)

    def _handle_confirm_input(self, text: str) -> None:
        lowered = text.lower()
        if lowered in ("y", "yes"):
            value = self._confirm_value
            if value is None:
                self.phase = Phase.AWAIT_INPUT
                self._reprint_prompt()
                return
            self._begin_start(value)
            return
        if lowered in ("n", "no"):
            self._confirm_value = None
            self.phase = Phase.AWAIT_INPUT
            self._print("已返回难度选择。")
            self._reprint_prompt()
            return
        if lowered == "q":
            self._cancel_to_idle()
            return
        self._print("请输入 y（开始）/ n（返回）/ q（取消）。")

    # ---- 连接/接管 ----

    def on_stop(self) -> None:
        """用户 stop：暂停本局的自动动作（连接由 CLI 断开）。"""
        self._cancel_start_task()
        if self.phase is Phase.STARTING:
            self.phase = Phase.IDLE
        self._controller.reset()

    def on_resume(self) -> None:
        """用户 resume：恢复接管；停留在难度页则重新提示选难度。"""
        now = self._clock()
        menu = self.state.fresh_menu(now=now)
        if menu is not None and menu.get("phase") == "difficulty_select":
            self._suppressed_signature = None
            self._prompted_signature = None
            self._on_difficulty_menu(menu, now)
            if self.phase is Phase.AWAIT_INPUT:
                return
        if self.phase is Phase.RUNNING:
            self._print("已恢复接管（%s）" % self._controller.describe())
            return
        self._print("已恢复接管")

    # ---- 周期驱动 ----

    async def tick(self) -> None:
        """每 50ms 调用一次（CLI 循环）；单次异常会由调用方记录，不致命。"""
        now = self._clock()
        if self.phase is Phase.ENDED:
            self.phase = Phase.IDLE
            self._print("已回到待命（IDLE）；重新停在难度页可开始下一局")
        self._sync_connection()
        self._track_snapshot(now)
        menu = self.state.fresh_menu(now=now)
        if menu is not None:
            phase = menu.get("phase")
            if phase == "difficulty_select":
                self._on_difficulty_menu(menu, now)
            elif phase == "run_end":
                self._on_run_end(menu)
        else:
            self._on_difficulty_left()
        if self.phase is Phase.RUNNING and self.state.connected and not self.state.observe_only:
            await self._controller.tick(self.state, menu, now)

    def _sync_connection(self) -> None:
        connected = self.state.connected
        if connected == self._connected_seen:
            return
        self._connected_seen = connected
        if connected:
            if self.state.observe_only:
                self._print(
                    "已连接（session=%s）：重连默认 OBSERVE_ONLY，输入 resume 恢复接管"
                    % (self.state.session_id or "?")
                )
            else:
                self._print(
                    "已连接（session=%s）：已接管（在难度页输入 D0–Dn 开始）"
                    % (self.state.session_id or "?")
                )
            return
        self._print("连接断开：控制已暂停（mod 将自动重连；重连后需 resume）")
        self._cancel_start_task()
        if self.phase is Phase.STARTING:
            self.phase = Phase.IDLE
        self._controller.reset()

    def _track_snapshot(self, now: float) -> None:
        snapshot = self.state.fresh_snapshot(now=now)
        if snapshot is None:
            return
        if self._run_started_at is None or self._run_finished:
            return
        self._controller.observe(snapshot)
        self._check_death(snapshot, now)

    def _check_death(self, snapshot: dict, now: float) -> None:
        player = snapshot.get("player")
        if not isinstance(player, dict):
            return
        if player.get("alive") is False:
            if self._death_since is None:
                self._death_since = now
            elif now - self._death_since >= self._death_fallback_s:
                self._finish_run(None, note="未收到 run_end，按死亡兜底")
        elif player.get("alive") is True:
            self._death_since = None

    # ---- 难度页 ----

    def _on_difficulty_menu(self, payload: dict, now: float) -> None:
        if self.phase in (Phase.STARTING, Phase.RUNNING, Phase.ENDED):
            return
        signature = _signature(payload)
        menu = DifficultyMenu.from_payload(payload)
        if self.state.observe_only:
            self.phase = Phase.MENU_READY
            if signature != self._observe_hint_signature:
                self._observe_hint_signature = signature
                self._print(
                    "检测到难度选择页；当前为 OBSERVE_ONLY，输入 resume 接管后可选择难度"
                )
            return
        if signature == self._suppressed_signature:
            return
        if self.phase is Phase.CONFIRM and signature == self._prompted_signature:
            return
        self.difficulty = menu
        if signature != self._prompted_signature:
            self._prompted_signature = signature
            self._confirm_value = None
            self.phase = Phase.AWAIT_INPUT
            self._print(menu.format_prompt())
        elif self.phase in (Phase.IDLE, Phase.MENU_READY):
            self.phase = Phase.AWAIT_INPUT
            self._print(menu.format_prompt())

    def _on_difficulty_left(self) -> None:
        if self.phase in (Phase.STARTING, Phase.RUNNING, Phase.ENDED):
            return
        if self.phase is not Phase.IDLE:
            self._print("难度页已离开，回到待命")
            self._confirm_value = None
            self.phase = Phase.IDLE
        self.difficulty = None
        self._prompted_signature = None
        self._suppressed_signature = None
        self._observe_hint_signature = None

    # ---- 开局序列 ----

    def _begin_start(self, value: int) -> None:
        self._confirm_value = None
        self.phase = Phase.STARTING
        self._print("[开始] 设置 D%d → 读回校验 → 开始对局（走位控制器接管）…" % value)
        self._cancel_start_task()
        self._start_task = asyncio.create_task(self._start_sequence(value))

    async def _start_sequence(self, value: int) -> None:
        try:
            ref = await self.server.send_action(
                "menu_set_difficulty", {"value": value}, track_ack=True
            )
            if ref is None:
                self._print("[开始] 未连接或未接管，已取消")
                self._back_to_input_or_idle()
                return
            ack = await self.server.wait_ack(ref, timeout=self._ack_timeout_s)
            if not ack_ok(ack):
                self._print("[开始] 设置难度 D%d 失败：%s" % (value, ack_error(ack)))
                self._back_to_input_or_idle()
                return
            if not await self._wait_selected(value):
                self._print("[开始] 读回校验失败：未观测到选中 D%d" % value)
                self._back_to_input_or_idle()
                return
            self._print("[开始] 难度读回校验通过（D%d），发送开始对局" % value)
            ref = await self.server.send_action("menu_start_run", {}, track_ack=True)
            if ref is None:
                self._print("[开始] 未连接或未接管，已取消")
                self._back_to_input_or_idle()
                return
            ack = await self.server.wait_ack(ref, timeout=self._ack_timeout_s)
            if not ack_ok(ack):
                self._print("[开始] 开始对局失败：%s" % ack_error(ack))
                self._back_to_input_or_idle()
                return
            if await self._wait_run_start():
                self._enter_run(value)
            else:
                self._print(
                    "[开始] 已发送开始，但未在 %.0fs 内观测到第 1 波战斗"
                    % self._start_timeout_s
                )
                self._back_to_input_or_idle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 状态机任务不允许静默死亡
            self._print("[开始] 内部异常：%s" % exc)
            self._back_to_input_or_idle()

    async def _wait_selected(self, value: int) -> bool:
        target = difficulty_my_id(value)
        deadline = self._clock() + self._readback_timeout_s
        while True:
            now = self._clock()
            menu = self.state.fresh_menu(now=now)
            if menu is not None and menu.get("phase") == "difficulty_select":
                difficulty = menu.get("difficulty")
                selected = difficulty.get("selected") if isinstance(difficulty, dict) else None
                if selected == target:
                    return True
            if not self.state.connected or now >= deadline:
                return False
            await asyncio.sleep(0.05)

    async def _wait_run_start(self) -> bool:
        deadline = self._clock() + self._start_timeout_s
        while True:
            now = self._clock()
            snapshot = self.state.fresh_snapshot(now=now)
            if snapshot is not None:
                wave = snapshot.get("wave")
                if (
                    isinstance(wave, dict)
                    and wave.get("index") == 1
                    and wave.get("phase") == "combat"
                    and snapshot.get("player")
                ):
                    return True
            if not self.state.connected or now >= deadline:
                return False
            await asyncio.sleep(0.05)

    def _enter_run(self, value: int) -> None:
        self.phase = Phase.RUNNING
        self._run_started_at = self._clock()
        self._run_finished = False
        self._death_since = None
        self._controller.begin()
        self._print(
            "[对局] 已进入第 1 波（D%d）；走位控制器 %s 接管"
            % (value, self._controller.autopilot_name)
        )

    def _back_to_input_or_idle(self) -> None:
        now = self._clock()
        menu = self.state.fresh_menu(now=now)
        if (
            menu is not None
            and menu.get("phase") == "difficulty_select"
            and self.state.connected
            and not self.state.observe_only
        ):
            self.difficulty = DifficultyMenu.from_payload(menu)
            self._prompted_signature = _signature(menu)
            self.phase = Phase.AWAIT_INPUT
            self._print(self.difficulty.format_prompt())
        else:
            self.phase = Phase.IDLE

    def _cancel_to_idle(self) -> None:
        self._suppressed_signature = self._prompted_signature
        self._confirm_value = None
        self.phase = Phase.IDLE
        self._print("已取消难度选择，保持待命（输入 resume 或重新进入难度页可再次提示）")

    # ---- 终局 ----

    def _on_run_end(self, payload: dict) -> None:
        if self._run_finished:
            return
        self._finish_run(RunEndMenu.from_payload(payload))

    def _finish_run(self, run_end: Optional[RunEndMenu], *, note: Optional[str] = None) -> None:
        if self._run_finished:
            return
        self._run_finished = True
        self.phase = Phase.ENDED
        now = self._clock()
        duration = now - self._run_started_at if self._run_started_at is not None else None
        report = self._controller.build_report(run_end, duration_s=duration, note=note)
        self._print(report.format())
        self._controller.reset()
        self._run_started_at = None
        self._death_since = None

    # ---- 视图 ----

    def describe(self) -> str:
        text = "阶段=%s" % self.phase.value
        if self.phase is Phase.RUNNING:
            text += "（%s）" % self._controller.describe()
        elif self.phase is Phase.AWAIT_INPUT and self.difficulty is not None:
            text += "（可选 %s）" % self.difficulty.unlocked_range_text()
        elif self.phase is Phase.CONFIRM:
            text += "（等待模式确认）"
        return text

    # ---- 工具 ----

    def _print(self, text: str) -> None:
        self._out(text)

    def _reprint_prompt(self) -> None:
        if self.difficulty is not None:
            self._print(self.difficulty.format_prompt())

    def _cancel_start_task(self) -> None:
        task = self._start_task
        self._start_task = None
        if task is not None and not task.done():
            task.cancel()


def _signature(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)
