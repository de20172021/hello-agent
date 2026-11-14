"""工作区文件变更监听：轮询快照 + diff，把模型改动过的文件实时暴露出来。

为什么不用 watchdog：
- 依赖越少越好，而且 diff 的口径（相对路径、跳过哪些目录）要和文件面板一致；
- 快照只存 `相对路径 → mtime`，几千个文件也就毫秒级，1.2s 轮询一次完全无感。

一个 Watcher 可以同时盯多个根目录（当前工作目录 + 技能库），
技能库里的 SKILL.md 也会以 `skills/<slug>/SKILL.md` 的形式出现在变更清单里，
和产物卡片的路径口径完全一致。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

# 不参与变更检测的目录（与 workspace.SKIP_DIRS 口径一致，再补几个常见的重目录）
SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    ".venv",
    "venv",
    ".sandbox",
    ".ipynb_checkpoints",
    ".idea",
    ".vscode",
    ".workbuddy",
}

# 快照文件数上限：超过说明这个工作目录太大，diff 会失真，超出部分不跟踪
MAX_FILES = 8000
# 单次 poll 最多返回的变更条数（防抖：模型批量删库时事件不至于刷屏）
MAX_CHANGES = 200

POLL_INTERVAL = 1.2


class _Overflow(Exception):
    """快照文件数达到上限时用来快速退出递归。"""


def snapshot(root: Path, prefix: str = "") -> dict[str, float]:
    """把一个目录快照成 `{相对路径: mtime}`。prefix 用来拼多根目录时的路径前缀。"""
    out: dict[str, float] = {}

    def walk(base: Path, pre: str) -> None:
        try:
            entries = list(base.iterdir())
        except OSError:
            return
        for entry in entries:
            if len(out) >= MAX_FILES:
                raise _Overflow
            name = entry.name
            if entry.is_dir():
                if name in SKIP_DIRS or name.startswith("."):
                    continue
                walk(entry, f"{pre}{name}/")
            elif entry.is_file():
                try:
                    out[f"{pre}{name}"] = entry.stat().st_mtime
                except OSError:
                    pass

    try:
        walk(root, prefix)
    except _Overflow:
        pass
    except RecursionError:
        pass
    return out


class ChangeWatcher:
    """监听若干个根目录的文件变更。

    roots: `{路径前缀: 绝对目录}`，例如 `{"": workdir, "skills/": skills_dir}`。
    """

    def __init__(self, roots: dict[str, Path]) -> None:
        self.roots = {prefix: Path(path) for prefix, path in roots.items() if path}
        self._before: dict[str, float] | None = None

    def _snap(self) -> dict[str, float]:
        merged: dict[str, float] = {}
        for prefix, root in self.roots.items():
            merged.update(snapshot(root, prefix))
        return merged

    def start(self) -> None:
        """记下起始快照，之后的 diff 都相对这个时刻。"""
        self._before = self._snap()

    def poll(self) -> list[dict[str, Any]]:
        """返回自上次 poll 以来的变更（created / modified / deleted），并滚动快照。"""
        if self._before is None:
            # 没调过 start()：先拍基线，本次不报变更
            self.start()
            return []
        after = self._snap()
        changes: list[dict[str, Any]] = []
        for path, mtime in after.items():
            old = self._before.get(path)
            if old is None:
                changes.append({"action": "created", "path": path, "mtime": mtime})
            elif old != mtime:
                changes.append({"action": "modified", "path": path, "mtime": mtime})
        for path in self._before:
            if path not in after:
                changes.append({"action": "deleted", "path": path})
        self._before = after
        changes.sort(key=lambda c: (c["action"] != "created", c["path"]))
        return changes[:MAX_CHANGES]

    def stop(self) -> list[dict[str, Any]]:
        """收尾：最后一次 diff（这轮结束后再也不会轮询了）。"""
        return self.poll()


def describe(changes: list[dict[str, Any]]) -> str:
    """给人看的一行摘要，调试日志用。"""
    if not changes:
        return "（无变更）"
    parts = []
    for action, label in (("created", "新增"), ("modified", "修改"), ("deleted", "删除")):
        n = sum(1 for c in changes if c["action"] == action)
        if n:
            parts.append(f"{label} {n}")
    return "、".join(parts) + f"（共 {len(changes)} 项，{time.strftime('%H:%M:%S')}）"
