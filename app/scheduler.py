"""定时任务调度器：到点了就在后台自动发起一轮对话。

设计取舍：
- 触发靠一个 10 秒的 ticker（本地单机应用，够用且简单），任务状态全部落在 SQLite，
  重启后 `next_run_at` 还在，不会丢；
- 触发时如果目标会话正在生成，就顺延 60 秒重试，不打断用户手上的对话；
- 任务本身复用 `runs.manager`，所以定时任务和我们手动发消息走完全相同的链路
  （工具、技能、记忆、上下文压缩都一样），输出直接留在对应会话里，想看就看。
"""

from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any

from pathlib import Path

from . import schedule_parse, spaces
from .runs import Run, auto_title, manager as runs_manager
from .settings import load_config
from .store import store


class Scheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.tick_seconds = 10

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            self._task = asyncio.create_task(self._loop())
        except RuntimeError:
            self._task = None

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(3)  # 等模型后端与 MCP 起来
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            await asyncio.sleep(self.tick_seconds)

    # ------------------------------------------------------------ 触发

    async def tick(self) -> list[str]:
        now = time.time()
        fired: list[str] = []
        for schedule in store.list_schedules(enabled_only=True):
            next_at = schedule.get("next_run_at")
            if not next_at:
                store.update_schedule(
                    schedule["id"], next_run_at=schedule_parse.next_run(schedule, now)
                )
                continue
            if float(next_at) > now:
                continue
            result = await self.fire(schedule)
            if result.get("ok"):
                fired.append(schedule["id"])
        return fired

    async def _new_run_conversation(self, schedule: dict[str, Any]) -> str:
        """每次执行都新建一个对话，挂在任务自己的目录下（产物按时间戳分开）。"""
        cfg = load_config()
        space = spaces.ensure_schedule_space(schedule)
        root = Path(space["path"])
        root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        title = schedule.get("title") or auto_title(schedule.get("prompt", ""), 18)
        conv = store.create_conversation(
            title=f"{title[:24]} · {time.strftime('%m-%d %H:%M', time.localtime(now))}",
            model_id=schedule.get("model_id") or cfg["active_model_id"],
            system_prompt=cfg["defaults"]["system_prompt"],
            temperature=cfg["defaults"]["temperature"],
            top_p=cfg["defaults"]["top_p"],
            max_tokens=cfg["defaults"]["max_tokens"],
            tools_enabled=cfg["tools"]["enabled"],
            thinking=bool(cfg["defaults"].get("thinking", False)),
            thinking_level=cfg["defaults"].get("thinking_level", "medium"),
            space_id=space["id"],
            output_dir=spaces.run_output_dir(root, now),
        )
        store.update_schedule(
            schedule["id"], conversation_id=conv["id"], space_id=space["id"]
        )
        schedule["conversation_id"] = conv["id"]
        schedule["space_id"] = space["id"]
        return conv["id"]

    async def fire(self, schedule: dict[str, Any], *, manual: bool = False) -> dict[str, Any]:
        schedule_id = schedule["id"]
        prompt = (schedule.get("prompt") or "").strip()
        if not prompt:
            store.update_schedule(schedule_id, last_status="empty_prompt", enabled=False)
            return {"ok": False, "detail": "任务内容为空，已停用"}

        conv_id = await self._new_run_conversation(schedule)
        running = runs_manager.active_run(conv_id)
        if running:
            store.update_schedule(
                schedule_id, next_run_at=time.time() + 60, last_status="busy_retry"
            )
            return {"ok": False, "detail": "目标会话正在生成，60 秒后重试"}

        try:
            run = await runs_manager.start(conv_id, content=prompt, source="schedule")
        except Exception as exc:  # noqa: BLE001
            store.update_schedule(schedule_id, last_status=f"error:{exc}")
            return {"ok": False, "detail": str(exc)}

        upcoming = schedule_parse.next_run(schedule)
        store.update_schedule(
            schedule_id,
            last_run_at=time.time(),
            last_run_id=run.id,
            last_status="running",
            run_count=int(schedule.get("run_count") or 0) + 1,
            next_run_at=upcoming,
            # 一次性任务跑完就没有下一次了，自动停用
            enabled=schedule.get("enabled", True) if upcoming else False,
        )

        def _finished(finished: Run) -> None:
            store.update_schedule(schedule_id, last_status=finished.status)

        run.on_finish.append(_finished)
        return {
            "ok": True,
            "run_id": run.id,
            "conversation_id": conv_id,
            "manual": manual,
            "next_run_at": upcoming,
        }

    async def run_now(self, schedule_id: str) -> dict[str, Any]:
        schedule = store.get_schedule(schedule_id)
        if schedule is None:
            return {"ok": False, "detail": "任务不存在"}
        result = await self.fire(schedule, manual=True)
        # 手动跑一次不影响原定计划：把 next_run_at 还原回去
        if result.get("ok") and schedule.get("enabled"):
            store.update_schedule(
                schedule_id, next_run_at=schedule_parse.next_run(schedule), enabled=True
            )
        return result


scheduler = Scheduler()
