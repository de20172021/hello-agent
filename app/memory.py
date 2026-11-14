"""产品自记忆：跨会话的长期记忆文件 + 按段落的增删改。

记忆保存在 data/memory/MEMORY.md，属于「固定位置」，会注入系统提示词，
模型也可以通过 memory_save / memory_forget 工具自己维护。
"""

from __future__ import annotations

import datetime as _dt
import re
import threading
from pathlib import Path
from typing import Any

from .settings import MEMORY_PATH, ensure_dirs, load_config

_LOCK = threading.RLock()
DEFAULT_SECTION = "事实与决定"
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _path() -> Path:
    ensure_dirs()
    return MEMORY_PATH


def read_text() -> str:
    with _LOCK:
        try:
            return _path().read_text(encoding="utf-8")
        except OSError:
            return ""


def write_text(text: str) -> None:
    with _LOCK:
        normalized = (text or "").rstrip() + "\n"
        _path().write_text(normalized, encoding="utf-8")


def _today() -> str:
    return _dt.date.today().isoformat()


def sections() -> list[dict[str, Any]]:
    text = read_text()
    matches = list(SECTION_RE.finditer(text))
    out: list[dict[str, Any]] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        bullets = [line for line in body.splitlines() if line.strip().startswith(("-", "*"))]
        out.append({"name": match.group(1).strip(), "items": len(bullets), "body": body})
    return out


def _insert_into_section(text: str, section: str, line: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(section)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        block = f"\n## {section}\n\n{line}\n"
        return text.rstrip() + "\n" + block
    start = match.end()
    next_match = SECTION_RE.search(text, start)
    end = next_match.start() if next_match else len(text)
    body = text[start:end]
    trimmed = body.rstrip()
    return text[:start] + trimmed + "\n" + line + "\n" + (text[end:] if next_match else "\n")


def save(content: str, section: str = DEFAULT_SECTION, *, source: str = "agent") -> dict[str, Any]:
    item = " ".join((content or "").split())
    if not item:
        raise ValueError("记忆内容为空")
    if len(item) > 800:
        item = item[:800] + "…"
    section = (section or DEFAULT_SECTION).strip()[:40] or DEFAULT_SECTION

    with _LOCK:
        text = read_text()
        if item in text:
            return {"ok": True, "duplicate": True, "section": section}
        line = f"- [{_today()}] {item}"
        if source:
            line += f"  <!-- {source} -->"
        updated = _insert_into_section(text, section, line)
        write_text(updated)
    return {"ok": True, "section": section, "chars": len(updated)}


def forget(query: str, section: str | None = None) -> dict[str, Any]:
    needle = (query or "").strip().lower()
    if not needle:
        raise ValueError("请提供要忘记的关键词")
    text = read_text()
    removed: list[str] = []
    if section:
        pattern = re.compile(rf"^##\s+{re.escape(section)}\s*$", re.MULTILINE)
        match = pattern.search(text)
        if not match:
            return {"ok": True, "removed": 0}
        start = match.end()
        next_match = SECTION_RE.search(text, start)
        end = next_match.start() if next_match else len(text)
        body = text[start:end]
        kept_lines = []
        for line in body.splitlines():
            if line.strip().startswith(("-", "*")) and needle in line.lower():
                removed.append(line.strip())
            else:
                kept_lines.append(line)
        text = text[:start] + "\n".join(kept_lines) + text[end:]
    else:
        kept_lines = []
        for line in text.splitlines():
            if line.strip().startswith(("-", "*")) and needle in line.lower():
                removed.append(line.strip())
            else:
                kept_lines.append(line)
        text = "\n".join(kept_lines)
    if removed:
        write_text(text)
    return {"ok": True, "removed": len(removed), "items": removed[:10]}


def clear(section: str | None = None) -> None:
    if not section:
        write_text("# HelloAgent 长期记忆\n\n## 用户偏好\n\n## 项目约定\n\n## 事实与决定\n")
        return
    text = read_text()
    pattern = re.compile(rf"^##\s+{re.escape(section)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return
    start = match.end()
    next_match = SECTION_RE.search(text, start)
    end = next_match.start() if next_match else len(text)
    write_text(text[:start] + "\n" + text[end:])


def prompt_block() -> str:
    cfg = load_config().get("memory", {})
    if not cfg.get("enabled", True):
        return ""
    text = read_text().strip()
    if not text:
        return ""
    limit = int(cfg.get("max_chars", 6000))
    body = text if len(text) <= limit else text[:limit] + "\n…（记忆过长已截断）"
    return (
        "## 长期记忆（跨会话，来自 data/memory/MEMORY.md）\n"
        "以下是你在以往对话中主动记下的信息，回答时应当遵守；"
        "如果用户明确纠正了其中某条，请用 memory_forget 删掉旧条目再用 memory_save 记新的。\n\n"
        f"{body}"
    )


def overview() -> dict[str, Any]:
    return {
        "path": str(MEMORY_PATH),
        "enabled": bool(load_config().get("memory", {}).get("enabled", True)),
        "sections": sections(),
        "text": read_text(),
    }
