"""多模态输入与产物（artifact）元数据。

两条链路都收在这一个模块里：

1. **输入**：用户粘贴/拖进来的图片、文档先落盘到工作区 `uploads/日期/`，
   消息里只存「相对路径 + 类型 + 大小」，不把 base64 塞进数据库。
   组装提示词时再按模型能力决定：
   - 支持看图（OpenAI 兼容端点）→ 转成 `image_url` 的 data URL；
   - 纯文本模型 → 附件的文字内容直接内联进提示词，图片降级成一句路径说明，
     并提示模型可以用工具继续访问，不会让请求直接失败。
2. **产物**：把工作区里的文件转成前端卡片需要的元数据（类型、大小、预览方式）。

同一张图在一轮里会被反复组装（多轮工具循环），这里用带 mtime 的小缓存避免重复读盘。
"""

from __future__ import annotations

import base64
import mimetypes
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import workspace

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdx"}
HTML_SUFFIXES = {".html", ".htm"}
TABLE_SUFFIXES = {".csv", ".tsv"}
CODE_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".sh", ".bat", ".ps1", ".sql", ".java", ".c",
    ".h", ".cpp", ".hpp", ".go", ".rs", ".rb", ".php", ".vue", ".svelte", ".css",
    ".scss", ".less", ".xml", ".ipynb", ".lua", ".kt", ".swift", ".r", ".m",
}
TEXT_SUFFIXES = {".txt", ".log", ".text"}
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".zip"}

# 单张图片超过这个体积就不塞进提示词了（base64 后还会再涨 1/3）
MAX_IMAGE_BYTES = 6 * 1024 * 1024
# 文本附件内联进提示词的上限（字符），超出部分截断并注明
MAX_INLINE_CHARS = 12000
# 一次提示词里最多带几张图（历史消息里的旧图会被降级成文字说明）
MAX_IMAGES_PER_PROMPT = 6

_CODE_LANG = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".sh": "bash", ".bat": "bat", ".ps1": "powershell", ".sql": "sql",
    ".html": "html", ".htm": "html", ".css": "css", ".xml": "xml", ".go": "go",
    ".rs": "rust", ".java": "java", ".c": "c", ".h": "c", ".cpp": "cpp", ".rb": "ruby",
    ".php": "php", ".md": "markdown", ".csv": "csv", ".tsv": "tsv",
}

_UNSAFE = re.compile(r"[^\w\u4e00-\u9fa5.\-]+")


# ------------------------------------------------------------------ 类型判断


def kind_of(name: str) -> str:
    """把文件名归类成前端卡片/预览用的类型。"""
    suffix = Path(str(name or "")).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in HTML_SUFFIXES:
        return "html"
    if suffix in TABLE_SUFFIXES:
        return "table"
    if suffix in CODE_SUFFIXES:
        return "code"
    if suffix in TEXT_SUFFIXES:
        return "text"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    return "other"


def lang_of(name: str) -> str:
    return _CODE_LANG.get(Path(str(name or "")).suffix.lower(), "")


def _mime(name: str) -> str:
    guessed, _ = mimetypes.guess_type(str(name))
    return guessed or "application/octet-stream"


def safe_name(name: str) -> str:
    raw = Path(str(name or "")).name.strip() or "file"
    cleaned = _UNSAFE.sub("_", raw).strip("._")
    return cleaned[:80] or f"file{Path(raw).suffix}"


# ------------------------------------------------------------------ 落盘


def save_upload(name: str, data: bytes) -> dict[str, Any]:
    """把上传的二进制写进 `uploads/YYYYMMDD/`，返回附件元数据。"""
    folder = time.strftime("uploads/%Y%m%d")
    target_dir = workspace.resolve(folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(name)
    stem, suffix = Path(base).stem, Path(base).suffix
    stamp = time.strftime("%H%M%S")
    candidate = target_dir / f"{stamp}_{base}"
    index = 1
    while candidate.exists():
        candidate = target_dir / f"{stamp}_{stem}({index}){suffix}"
        index += 1
    candidate.write_bytes(data)
    return artifact_meta(candidate, source="upload")


def normalize(items: list[Any] | None) -> list[dict[str, Any]]:
    """把前端传来的附件列表洗成可入库/可进提示词的形状，非法条目直接丢掉。"""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip().replace("\\", "/")
        if not rel:
            continue
        try:
            target = workspace.resolve(rel, must_exist=True)
        except (PermissionError, ValueError, FileNotFoundError, OSError):
            continue
        if not target.is_file():
            continue
        meta = artifact_meta(target, source=str(item.get("source") or "upload"))
        # 前端可能带了原始文件名，保留更友好的那个
        given = str(item.get("name") or "").strip()
        if given:
            meta["name"] = given[:120]
        out.append(meta)
    return out[:12]


# ------------------------------------------------------------------ 产物元数据


def artifact_meta(target: Path, *, source: str = "file") -> dict[str, Any]:
    """工作区文件 → 前端卡片需要的元数据。"""
    from urllib.parse import quote

    rel_path = workspace.rel(target)
    kind = kind_of(target.name)
    size = 0
    mtime = 0.0
    try:
        stat = target.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        pass
    return {
        "path": rel_path,
        "name": target.name,
        "kind": kind,
        "lang": lang_of(target.name),
        "mime": _mime(target.name),
        "size": size,
        "mtime": mtime,
        "source": source,
        "url": f"/api/workspace/raw?path={quote(rel_path)}",
        "preview": f"/ws/{quote(rel_path)}",
        "renderable": kind in ("image", "markdown", "html", "table", "code", "text"),
    }


# ------------------------------------------------------------------ 图片 → data URL

_IMAGE_CACHE: "OrderedDict[str, tuple[tuple[float, int], str]]" = OrderedDict()
_IMAGE_CACHE_MAX = 24


def data_url(rel_path: str) -> str | None:
    """读图并转成 data URL（带 mtime 缓存）。体积过大或读不到时返回 None。"""
    try:
        target = workspace.resolve(rel_path, must_exist=True)
    except (PermissionError, ValueError, FileNotFoundError, OSError):
        return None
    try:
        stat = target.stat()
    except OSError:
        return None
    if stat.st_size > MAX_IMAGE_BYTES:
        return None
    signature = (stat.st_mtime, stat.st_size)
    cached = _IMAGE_CACHE.get(rel_path)
    if cached and cached[0] == signature:
        _IMAGE_CACHE.move_to_end(rel_path)
        return cached[1]
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    mime = _mime(target.name)
    if not mime.startswith("image/"):
        mime = "image/png"
    url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    _IMAGE_CACHE[rel_path] = (signature, url)
    _IMAGE_CACHE.move_to_end(rel_path)
    while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.popitem(last=False)
    return url


# ------------------------------------------------------------------ 组装提示词


def _read_inline(rel_path: str) -> tuple[str | None, bool]:
    """读文本附件内容；读不到或二进制时返回 (None, False)。"""
    try:
        data = workspace.read_text(rel_path, max_bytes=MAX_INLINE_CHARS * 4)
    except (PermissionError, ValueError, FileNotFoundError, OSError):
        return None, False
    if data.get("binary") or data.get("is_dir"):
        return None, False
    text = str(data.get("content") or "")
    truncated = bool(data.get("truncated"))
    if len(text) > MAX_INLINE_CHARS:
        text = text[:MAX_INLINE_CHARS]
        truncated = True
    return text, truncated


def build_user_content(
    text: str,
    attachments: list[dict[str, Any]] | None,
    *,
    vision: bool,
    include_images: bool = True,
) -> Any:
    """把一条用户消息 + 附件组装成模型输入（字符串或 OpenAI 多模态数组）。"""
    if not attachments:
        return text or ""

    blocks: list[str] = []
    if (text or "").strip():
        blocks.append(text.strip())
    images: list[dict[str, Any]] = []

    for att in attachments:
        if not isinstance(att, dict):
            continue
        rel_path = str(att.get("path") or "")
        name = str(att.get("name") or Path(rel_path).name)
        kind = str(att.get("kind") or kind_of(name))

        if kind == "image":
            if vision and include_images:
                url = data_url(rel_path)
                if url:
                    images.append({"type": "image_url", "image_url": {"url": url}})
                else:
                    blocks.append(
                        f"[图片附件] {name}（工作区：{rel_path}）"
                        "——图片太大或读取失败，没有一起发送，需要时请让用户重新上传。"
                    )
            elif vision:
                blocks.append(
                    f"[图片附件] {name}（工作区：{rel_path}）——这是较早的图片，"
                    "为节省上下文没有重复发送；如需细节请让用户再说一次。"
                )
            else:
                blocks.append(
                    f"[图片附件] {name}（工作区：{rel_path}）"
                    "——当前模型不能直接看图，只能依据文件名与你用工具读到的信息作答。"
                )
            continue

        body, truncated = _read_inline(rel_path)
        if body is not None:
            lang = lang_of(name)
            note = "（内容较长，以下为前一部分）" if truncated else ""
            blocks.append(f"[附件：{name} · {rel_path}]{note}\n```{lang}\n{body}\n```")
            continue

        size = att.get("size") or 0
        blocks.append(
            f"[附件] {name}（工作区：{rel_path}，{size} 字节，非文本）"
            "——无法直接内联，需要时用工具访问这个路径。"
        )

    if not images:
        return "\n\n".join(blocks)
    parts: list[dict[str, Any]] = [
        {"type": "text", "text": "\n\n".join(blocks) or "请看下面的图片。"}
    ]
    parts.extend(images[:MAX_IMAGES_PER_PROMPT])
    return parts


def image_allowance(messages: list[dict[str, Any]]) -> set[str]:
    """从后往前挑出还能带图的消息 id，避免历史里每张图都重复计费。"""
    allowed: set[str] = set()
    budget = MAX_IMAGES_PER_PROMPT
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        images = [
            a for a in (msg.get("attachments") or []) if isinstance(a, dict) and a.get("kind") == "image"
        ]
        if not images:
            continue
        if len(images) <= budget:
            allowed.add(str(msg.get("id") or ""))
            budget -= len(images)
        if budget <= 0:
            break
    return allowed


def has_image(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)


def strip_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把多模态消息退化成纯文本（后端不支持图片时兜底用）。"""
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        texts = [
            str(p.get("text") or "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        dropped = any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)
        body = "\n\n".join(t for t in texts if t)
        if dropped:
            body += "\n\n（本轮有图片附件，但当前模型不支持看图，图片已忽略。）"
        out.append({**msg, "content": body})
    return out
