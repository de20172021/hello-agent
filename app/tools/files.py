"""文件与沙箱工具：全部限制在当前工作区内。

描述统一按「做什么 → 什么时候用 → 关键约束」三段写，并给参数补上
enum / default，模型照着填就不容易错。
"""

from __future__ import annotations

import json
from typing import Any

from .. import sandbox, workspace
from .registry import tool

_PATH_HINT = "路径相对工作区根目录，用正斜杠，例如 reports/a.md"


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


@tool(
    name="workspace_info",
    description=(
        "查看工作区根目录、文件数、占用空间，以及绝对路径。"
        "第一次操作文件、或不确定工作区在哪时先调用它。无参数。"
    ),
    parameters={"type": "object", "properties": {}},
    category="工作区",
)
def workspace_info() -> str:
    info = workspace.stats()
    info["path_hint"] = _PATH_HINT
    info["absolute"] = str(workspace.root())
    return _json(info)


@tool(
    name="list_files",
    description=(
        "列出工作区里的文件树（含大小）。"
        "找文件、确认某个文件存不存在时用；只想要文件名不要内容时优先用它，比逐个 read_file 省 token。"
        f"{_PATH_HINT}"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要列的子目录，默认 . 表示工作区根目录",
                "default": ".",
            },
            "max_entries": {
                "type": "integer",
                "description": "最多返回多少条，默认 200，上限 2000",
                "default": 200,
            },
        },
    },
    category="工作区",
)
def list_files(path: str = ".", max_entries: int = 200) -> str:
    result = workspace.tree(max_entries=int(max_entries or 200), sub=path)
    lines = [f"工作区：{result['root']}"]
    for item in result["entries"]:
        indent = "  " * int(item.get("depth", 0))
        size = f"{item['size']}B" if not item["is_dir"] else "dir"
        lines.append(f"{indent}{item['path']}  [{size}]")
    if not result["entries"]:
        lines.append("（空目录）")
    if result["truncated"]:
        lines.append(f"…（超过 {result['limit']} 条已截断，请用 search_files 精确定位）")
    return "\n".join(lines)


@tool(
    name="read_file",
    description=(
        "读取文本文件内容，可指定行范围。改文件前必须先用它看原文。"
        "返回的行号只是为了方便定位，不是文件内容的一部分 —— 传给 edit_file 的原文里不要带行号。"
        f"{_PATH_HINT}"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，例如 app/main.py"},
            "start_line": {
                "type": "integer",
                "description": "起始行号，从 1 开始，默认 1",
                "default": 1,
            },
            "max_lines": {
                "type": "integer",
                "description": "最多读多少行，默认 400，上限 3000",
                "default": 400,
            },
        },
        "required": ["path"],
    },
    category="工作区",
)
def read_file(path: str, start_line: int = 1, max_lines: int = 400) -> str:
    result = workspace.read_text(path)
    if result.get("is_dir"):
        return f"{result['path']} 是目录，内容：\n{result['content']}"
    text = result["content"]
    lines = text.splitlines()
    start = max(1, int(start_line or 1))
    limit = max(1, min(int(max_lines or 400), 3000))
    chunk = lines[start - 1 : start - 1 + limit]
    header = f"{result['path']}（{result['size']} 字节，共 {len(lines)} 行"
    if result.get("truncated"):
        header += "，文件过大仅读取前部"
    header += "）\n"
    numbered = "\n".join(f"{start + i:>5}  {line}" for i, line in enumerate(chunk))
    tail = ""
    if start - 1 + limit < len(lines):
        tail = f"\n…（还有 {len(lines) - (start - 1 + limit)} 行，继续用 start_line={start + limit} 读取）"
    return header + numbered + tail


@tool(
    name="write_file",
    description=(
        "写入或覆盖文件（自动建父目录）。新建文件、整段重写时用它。"
        "内容超过 1500 字就分段写：先 append=false 写第一段，再 append=true 追加后续段落 —— "
        "一次塞太长的内容会被输出上限截断，导致参数不完整而失败。"
        f"{_PATH_HINT} 写完在回复里给出路径，用户可在界面上预览。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，例如 reports/summary.md"},
            "content": {"type": "string", "description": "这一段的完整内容，换行用 \\n 转义"},
            "append": {
                "type": "boolean",
                "description": "true 追加到末尾（分段写第 2 段起用），false 覆盖。默认 false",
                "default": False,
            },
        },
        "required": ["path", "content"],
    },
    category="工作区",
)
def write_file(path: str, content: str, append: bool = False) -> str:
    result = workspace.write_text(path, content or "", append=bool(append))
    try:
        total = workspace.resolve(path).stat().st_size
    except OSError:
        total = result.get("bytes", 0)
    return _json({**result, "ok": True, "file_bytes": total})


@tool(
    name="edit_file",
    description=(
        "把文件里的一段原文替换成新文本，适合小范围修改，比整篇重写省 token。"
        "old_text 必须和文件里的内容逐字符一致（含缩进，不含行号）；匹配不到会报错并给出疑似位置。"
        "多行修改请带上前后各一行上下文，确保唯一。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_text": {
                "type": "string",
                "description": "要被替换的原文，必须完全匹配（含空格缩进，不要带行号）",
            },
            "new_text": {"type": "string", "description": "替换后的新文本，删除内容时传空字符串"},
            "replace_all": {
                "type": "boolean",
                "description": "是否替换全部匹配项，默认只换第一处",
                "default": False,
            },
        },
        "required": ["path", "old_text", "new_text"],
    },
    category="工作区",
)
def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    result = workspace.edit_text(path, old_text, new_text, replace_all=bool(replace_all))
    return _json({**result, "ok": True})


@tool(
    name="search_files",
    description=(
        "按正则搜索文件内容，返回 文件:行号:匹配行。"
        "不知道内容在哪个文件时用它，比遍历 list_files + read_file 快得多。"
        "只搜文本文件；正则用 Python 语法，例如 def\\s+main、TODO|FIXME。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则表达式，例如 TODO|FIXME"},
            "path": {
                "type": "string",
                "description": "要搜的子目录，默认 . 表示整个工作区",
                "default": ".",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回多少条，默认 60",
                "default": 60,
            },
        },
        "required": ["pattern"],
    },
    category="工作区",
)
def search_files(pattern: str, path: str = ".", max_results: int = 60) -> str:
    result = workspace.search(pattern, path, max_results=int(max_results or 60))
    if not result["hits"]:
        return f"没有匹配 {pattern} 的内容"
    lines = [f"{hit['path']}:{hit['line']}: {hit['text']}" for hit in result["hits"]]
    if result["truncated"]:
        lines.append("…（结果过多已截断，请把正则写得更具体）")
    return "\n".join(lines)


@tool(
    name="make_dir",
    description="创建目录（父目录会自动创建，已存在不报错）。write_file 会自动建目录，一般不需要单独调用。",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "目录路径，例如 reports/2026"}},
        "required": ["path"],
    },
    category="工作区",
)
def make_dir(path: str) -> str:
    return _json(workspace.mkdir(path))


@tool(
    name="delete_file",
    description=(
        "删除工作区里的文件或目录，不可恢复。"
        "只在用户明确要求删除时调用；删除前先 list_files 确认路径，不要猜。"
    ),
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "要删除的路径，不能用通配符"}},
        "required": ["path"],
    },
    category="工作区",
    dangerous=True,
)
def delete_file(path: str) -> str:
    return _json(workspace.delete(path))


@tool(
    name="run_command",
    description=(
        "在工作区目录里执行一条系统命令，返回退出码与 stdout/stderr。"
        "适合 git、装依赖、列目录、调用命令行工具。工作目录是工作区，不能 cd 到外面。"
        "Windows 下走 cmd.exe：不要用 bash 专有写法（&& 可用，$VAR 不可用，路径用 \\ 或 / 都行）。"
        "默认超时 30 秒，跑更久的任务请显式传 timeout。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "完整命令行，例如 git status 或 python -m pytest -q",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数，默认 30，最大 300",
                "default": 30,
            },
            "cwd": {
                "type": "string",
                "description": "相对工作区的子目录，默认 .",
                "default": ".",
            },
        },
        "required": ["command"],
    },
    category="沙箱",
    dangerous=True,
)
def run_command(command: str, timeout: int = 30, cwd: str = ".") -> str:
    result = sandbox.run_command(command, timeout=timeout, cwd=cwd)
    return _json(result)


@tool(
    name="run_python",
    description=(
        "用当前 Python 解释器执行一段代码，返回 stdout/stderr，当前目录是工作区。"
        "适合做数据处理、验证逻辑、批量生成文件；能用它一次算完的就别写临时脚本再 run_command。"
        "代码要自包含（没有交互输入），print 出来看结果。默认超时 30 秒。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "完整可执行的 Python 代码，换行用 \\n 转义"},
            "timeout": {
                "type": "integer",
                "description": "超时秒数，默认 30，最大 300",
                "default": 30,
            },
        },
        "required": ["code"],
    },
    category="沙箱",
    dangerous=True,
)
def run_python(code: str, timeout: int = 30) -> str:
    result = sandbox.run_python(code, timeout=timeout)
    return _json(result)
