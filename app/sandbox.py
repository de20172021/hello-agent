"""沙箱执行：在工作区内跑命令与 Python 片段。

注意：这是「受限工作目录 + 超时 + 输出截断 + 危险命令拦截」，
不是内核级隔离。真要跑不可信代码，请自行放到容器或虚拟机里。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import workspace
from .settings import load_config

# 明显破坏性的命令直接拒绝
DENY_PATTERNS = [
    r"\brm\s+-rf\s+/(\s|$)",
    r"\bformat\s+[a-z]:",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bmkfs\.",
    r"\bdd\s+if=.*of=/dev/",
    r"\bdel\s+/[sf].*[a-z]:\\\s*$",
    r"\brd\s+/s\s+/q\s+[a-z]:\\\s*$",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\bRemove-Item\b.*-Recurse.*[a-z]:\\\s*$",
]

MAX_OUTPUT = 12000


def _denied(command: str) -> str | None:
    lowered = (command or "").lower()
    for pattern in DENY_PATTERNS:
        if re.search(pattern, lowered):
            return pattern
    return None


def _decode(raw: bytes) -> str:
    """Windows 的 cmd / 子进程常输出 GBK，先按 UTF-8 试，失败再退 GBK。"""
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk", "cp936", "mbcs"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _hint(result: dict[str, Any]) -> dict[str, Any]:
    """给常见失败补一句可执行建议，省掉模型瞎猜一轮。"""
    err = (result.get("stderr") or "").lower()
    code = result.get("exit_code")
    if code in (None, 0):
        return result
    if "is not recognized" in err or "不是内部或外部命令" in (result.get("stderr") or ""):
        result["hint"] = (
            "这个命令在当前 PATH 里找不到。可以改用 run_python，或者先用 where <命令> 确认是否安装。"
        )
    elif "permission denied" in err or "拒绝访问" in (result.get("stderr") or ""):
        result["hint"] = "权限不足：换个不需要写系统目录的做法，或改用 run_python 写工作区内的文件。"
    elif "no such file" in err or "cannot find" in err or "找不到" in (result.get("stderr") or ""):
        result["hint"] = "路径不存在：先用 list_files 确认相对路径，注意工作目录是工作区根目录。"
    return result


def _trim(text: str, limit: int = MAX_OUTPUT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[: int(limit * 0.6)]
    tail = text[-int(limit * 0.3) :]
    return f"{head}\n\n…（已截断 {len(text) - limit} 字符）…\n\n{tail}", True


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["LOCALMIND_WORKSPACE"] = str(workspace.root())
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def run_command(command: str, timeout: int | None = None, cwd: str | None = None) -> dict[str, Any]:
    cfg = load_config().get("tools", {})
    if not cfg.get("allow_shell", True):
        raise PermissionError("设置里已关闭命令执行（tools.allow_shell=false）")

    cleaned = (command or "").strip()
    if not cleaned:
        raise ValueError("命令为空")
    hit = _denied(cleaned)
    if hit:
        raise PermissionError(f"命令命中危险模式，已拒绝执行：{hit}")

    limit = int(timeout or cfg.get("shell_timeout", 30))
    limit = max(3, min(limit, 300))
    workdir = workspace.resolve(cwd)

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cleaned,
            shell=True,
            cwd=str(workdir),
            capture_output=True,
            timeout=limit,
            env=_env(),
        )
        out, out_cut = _trim(_decode(proc.stdout))
        err, err_cut = _trim(_decode(proc.stderr))
        return _hint(
            {
                "command": cleaned,
                "cwd": workspace.rel(workdir),
                "exit_code": proc.returncode,
                "stdout": out,
                "stderr": err,
                "truncated": out_cut or err_cut,
                "duration": round(time.perf_counter() - started, 3),
            }
        )
    except subprocess.TimeoutExpired:
        return {
            "command": cleaned,
            "cwd": workspace.rel(workdir),
            "exit_code": None,
            "stdout": "",
            "stderr": f"命令超时（{limit}s），已终止",
            "timeout": True,
            "duration": round(time.perf_counter() - started, 3),
        }


def run_python(code: str, timeout: int | None = None) -> dict[str, Any]:
    """把代码写到工作区临时文件再执行，便于模型做数据分析/脚本验证。"""
    cfg = load_config().get("tools", {})
    limit = int(timeout or cfg.get("shell_timeout", 30))
    limit = max(3, min(limit, 300))
    snippet = (code or "").strip()
    if not snippet:
        raise ValueError("代码为空")

    sandbox_dir = workspace.root() / ".sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".py", dir=str(sandbox_dir), text=True)
    os.close(fd)
    script = Path(name)
    script.write_text(snippet, encoding="utf-8")

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(workspace.root()),
            capture_output=True,
            timeout=limit,
            env=_env(),
        )
        out, out_cut = _trim(_decode(proc.stdout))
        err, err_cut = _trim(_decode(proc.stderr))
        return _hint(
            {
                "exit_code": proc.returncode,
                "stdout": out,
                "stderr": err,
                "truncated": out_cut or err_cut,
                "duration": round(time.perf_counter() - started, 3),
            }
        )
    except subprocess.TimeoutExpired:
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": f"执行超时（{limit}s）",
            "timeout": True,
            "duration": round(time.perf_counter() - started, 3),
        }
    finally:
        try:
            script.unlink()
        except OSError:
            pass
