"""工作区：模型和用户共享的文件沙箱。

所有文件读写都被限制在工作区目录内，越界直接抛 PermissionError，
这样即使模型被提示词诱导也改不到项目源码或系统文件。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import spaces
from .settings import BASE_DIR, load_config

SKIP_DIRS = {
    ".git",
    ".workbuddy",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
}

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".html", ".htm", ".css", ".scss", ".xml", ".csv", ".tsv",
    ".log", ".sh", ".bat", ".ps1", ".sql", ".java", ".c", ".h", ".cpp", ".go", ".rs", ".rb",
    ".php", ".env", ".gitignore", ".dockerfile", ".vue", ".svelte",
}


def root() -> Path:
    """当前工作目录：优先用本轮上下文（任务 / 空间）指定的目录。"""
    from . import runtime

    override = runtime.current().workspace_root
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return spaces.task_root()


def resolve(raw: str | None, *, must_exist: bool = False) -> Path:
    """把相对路径解析为工作区内的绝对路径，越界即拒绝。"""
    base = root().resolve()
    cleaned = str(raw or ".").strip().strip('"').strip("'")
    if cleaned in ("", "~"):
        cleaned = "."
    candidate = Path(cleaned)
    target = candidate if candidate.is_absolute() else (base / candidate)
    try:
        target = target.resolve()
    except OSError as exc:  # 路径非法（如 Windows 保留字符）
        raise ValueError(f"路径非法：{raw}（{exc}）") from exc
    if target != base and base not in target.parents:
        raise PermissionError(f"只能访问工作区内的路径：{base}")
    _guard_sensitive(base, target)
    if must_exist and not target.exists():
        raise FileNotFoundError(f"文件或目录不存在：{cleaned}")
    return target


# 「任务」的工作目录是整个项目，模型不该顺手翻配置里的密钥
SENSITIVE_RELATIVE = {
    "data/config.json",
    "data/mcp.json",
    "data/chat.db",
    "data/chat.db-wal",
    "data/chat.db-shm",
}
SENSITIVE_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}


def _guard_sensitive(base: Path, target: Path) -> None:
    if base != BASE_DIR.resolve():
        return
    try:
        parts = target.relative_to(base).parts
    except ValueError:
        return
    rel = "/".join(parts)
    if (
        rel in SENSITIVE_RELATIVE
        or (parts and parts[0] == ".git")
        or target.name.lower() in SENSITIVE_NAMES
        or target.suffix.lower() == ".pem"
    ):
        raise PermissionError(
            "这是 HelloAgent 自己的配置或密钥文件，任务模式下不开放读写；"
            "要处理这类文件，请把所在目录作为「空间」关联进来"
        )


def rel(path: Path) -> str:
    try:
        return path.relative_to(root().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _is_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES or not path.suffix:
        try:
            with path.open("rb") as fh:
                head = fh.read(2048)
            return b"\x00" not in head
        except OSError:
            return False
    return False


def tree(max_entries: int | None = None, sub: str | None = None) -> dict[str, Any]:
    """返回扁平化的文件列表，附带大小与修改时间。"""
    cfg = load_config().get("workspace", {})
    limit = int(max_entries or cfg.get("max_tree_entries", 400))
    base = resolve(sub)
    entries: list[dict[str, Any]] = []
    truncated = False

    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        current_path = Path(current)
        depth = len(current_path.relative_to(base).parts) if current_path != base else 0
        if depth >= 6:
            dirs[:] = []
        for name in sorted(files):
            if name in (".DS_Store", "Thumbs.db"):
                continue
            full = current_path / name
            try:
                stat = full.stat()
            except OSError:
                continue
            entries.append(
                {
                    "path": rel(full),
                    "name": name,
                    "is_dir": False,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "depth": depth,
                }
            )
            if len(entries) >= limit:
                truncated = True
                break
        if truncated:
            break

    return {
        "root": str(root()),
        "entries": entries,
        "truncated": truncated,
        "limit": limit,
    }


def read_text(path: str, max_bytes: int | None = None) -> dict[str, Any]:
    cfg = load_config().get("workspace", {})
    limit = int(max_bytes or cfg.get("max_read_bytes", 200000))
    target = resolve(path, must_exist=True)
    if target.is_dir():
        listing = "\n".join(sorted(p.name for p in target.iterdir()))[:4000]
        return {"path": rel(target), "is_dir": True, "content": listing, "truncated": False}
    size = target.stat().st_size
    if not _is_text(target):
        return {
            "path": rel(target),
            "is_dir": False,
            "binary": True,
            "size": size,
            "content": f"（二进制文件，{size} 字节，无法以文本方式预览）",
            "truncated": False,
        }
    raw = target.read_bytes()[: limit + 1]
    text = raw.decode("utf-8", errors="replace")
    truncated = len(raw) > limit
    if truncated:
        text = text[:limit]
    return {
        "path": rel(target),
        "is_dir": False,
        "size": size,
        "content": text,
        "truncated": truncated,
        "lines": text.count("\n") + 1 if text else 0,
    }


def write_text(path: str, content: str, *, append: bool = False) -> dict[str, Any]:
    target = resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with target.open(mode, encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return {"path": rel(target), "bytes": len(content.encode("utf-8")), "append": append}


def _edit_miss_hint(text: str, old: str) -> str:
    """old 没匹配上时，给一个能直接行动的提示，而不是只说「找不到」。"""
    base = "未找到要替换的原文。"
    if re.match(r"^\s*\d+\s{2}", old or ""):
        return base + "你贴的内容带着 read_file 输出的行号，请去掉行号只保留原始文本。"

    needle = ""
    for line in (old or "").splitlines():
        if line.strip():
            needle = line.strip()
            break
    if needle:
        for idx, line in enumerate(text.splitlines(), 1):
            if needle in line or line.strip() == needle:
                return (
                    f"{base}第 {idx} 行有相近内容，但缩进或空白不一致。"
                    "请把 read_file 里的原文连着上下文一起复制，不要手改空格。"
                )
        # 空白无关的近似匹配
        squeezed = re.sub(r"\s+", "", old or "")[:60]
        flat = re.sub(r"\s+", "", text)
        if squeezed and squeezed in flat:
            return f"{base}内容存在但空白/换行不一致（例如缩进用了 tab 而不是空格）。请重新复制原文。"
    return base + "先用 read_file 确认原文（注意文件可能已被改动），再原样复制要替换的片段。"


def edit_text(path: str, old: str, new: str, *, replace_all: bool = False) -> dict[str, Any]:
    target = resolve(path, must_exist=True)
    text = target.read_text(encoding="utf-8", errors="replace")
    if not old:
        raise ValueError("old_text 不能为空")
    if old not in text:
        raise ValueError(_edit_miss_hint(text, old))
    count = text.count(old)
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    target.write_text(updated, encoding="utf-8", newline="\n")
    result: dict[str, Any] = {"path": rel(target), "replaced": count if replace_all else 1}
    if count > 1 and not replace_all:
        result["warning"] = (
            f"这段文本在文件里出现了 {count} 次，只替换了第一处；"
            "如果本意是全改，下次传 replace_all=true，并确认第一处就是目标位置。"
        )
    return result


def mkdir(path: str) -> dict[str, Any]:
    target = resolve(path)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": rel(target), "is_dir": True}


# 任务的工作目录就是项目根目录：这些是产品自己的代码与数据，删了就跑不起来了
PROTECTED_TOP_LEVEL = {
    "app",
    "web",
    "data",
    "models",
    "skills",
    ".git",
    ".workbuddy",
    "run.py",
    "requirements.txt",
    "README.md",
}


def delete(path: str) -> dict[str, Any]:
    target = resolve(path, must_exist=True)
    base = root().resolve()
    if target == base:
        raise PermissionError("不能删除工作目录本身")
    # 只有「任务」（根目录=项目目录）才需要额外保护，空间是用户自己的目录，自己负责
    if base == BASE_DIR.resolve():
        try:
            rel_parts = target.relative_to(base).parts
        except ValueError:
            rel_parts = ()
        if rel_parts and rel_parts[0] in PROTECTED_TOP_LEVEL:
            raise PermissionError(
                f"「{rel_parts[0]}」是 HelloAgent 自己的代码或数据，不能从任务里删除；"
                "确实要清理请直接操作文件系统"
            )
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"path": rel(target), "deleted": True}


def search(pattern: str, sub: str | None = None, *, max_results: int = 60) -> dict[str, Any]:
    """在文本文件里做正则搜索，返回 文件:行号:内容。"""
    base = resolve(sub)
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"正则表达式非法：{exc}") from exc
    hits: list[dict[str, Any]] = []
    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            full = Path(current) / name
            if not _is_text(full) or full.stat().st_size > 1_500_000:
                continue
            try:
                lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for idx, line in enumerate(lines, 1):
                if regex.search(line):
                    hits.append(
                        {
                            "path": rel(full),
                            "line": idx,
                            "text": line.strip()[:240],
                        }
                    )
                    if len(hits) >= max_results:
                        return {"hits": hits, "truncated": True, "count": len(hits)}
    return {"hits": hits, "truncated": False, "count": len(hits)}


def stats() -> dict[str, Any]:
    base = root()
    files = 0
    total = 0
    for current, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            try:
                total += (Path(current) / name).stat().st_size
                files += 1
            except OSError:
                continue
    return {"root": str(base), "files": files, "bytes": total}


def open_in_explorer() -> dict[str, Any]:
    """在系统文件管理器里打开工作区（仅本机有效）。"""
    base = str(root())
    try:
        if os.name == "nt":
            os.startfile(base)  # type: ignore[attr-defined]
        elif sys_platform() == "darwin":
            os.system(f'open "{base}"')
        else:
            os.system(f'xdg-open "{base}"')
        return {"ok": True, "path": base}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": base, "detail": str(exc)}


def _launch(target: Path) -> dict[str, Any]:
    """交给系统默认程序打开。"""
    try:
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys_platform() == "darwin":
            os.system(f'open "{target}"')
        else:
            os.system(f'xdg-open "{target}"')
        return {"ok": True, "path": str(target)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": str(target), "detail": str(exc)}


def open_abs(target: Path) -> dict[str, Any]:
    """打开工作目录之外的已知文件（技能库产物）。调用方自己负责守卫。"""
    if not target.is_file():
        return {"ok": False, "path": str(target), "detail": "不是文件"}
    return _launch(target)


def open_file(raw: str | None) -> dict[str, Any]:
    """用系统默认程序打开工作区里的某个文件。

    这就是「双击打开」——docx 交给本机 Word/WPS、pdf 交给默认阅读器，
    后端不解析、不转换，格式和版式完全是应用自己渲染的样子。
    只对已经过 workspace.resolve 守卫的文件生效，不会越出工作目录。
    """
    try:
        target = resolve(raw, must_exist=True)
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        return {"ok": False, "path": str(raw or ""), "detail": str(exc)}
    if not target.is_file():
        return {"ok": False, "path": str(target), "detail": "不是文件"}
    return _launch(target)


def reveal_file(raw: str | None) -> dict[str, Any]:
    """在文件管理器里选中某个文件（资源管理器 /select）。"""
    try:
        target = resolve(raw, must_exist=True)
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        return {"ok": False, "path": str(raw or ""), "detail": str(exc)}
    try:
        if os.name == "nt":
            import subprocess

            # explorer /select 定位到具体文件；返回码非 0 是常态，不当作失败
            subprocess.run(["explorer", "/select,", str(target)], check=False)
        elif sys_platform() == "darwin":
            os.system(f'open -R "{target}"')
        else:
            os.system(f'xdg-open "{target.parent}"')
        return {"ok": True, "path": str(target)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": str(target), "detail": str(exc)}


def sys_platform() -> str:
    import sys

    return sys.platform
