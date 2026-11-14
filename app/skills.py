"""技能系统：skills/<slug>/SKILL.md 的增删改查与注入。

一个技能就是一份 Markdown 说明（带 frontmatter），描述某类任务的
处理流程、注意事项和可用工具。模型通过 use_skill 工具按需加载，
用户也可以在输入框里直接勾选技能，本轮强制注入。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .settings import SKILLS_DIR, ensure_dirs, load_config

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
# 消息里的 /技能 标记：/ 前面必须是行首或空白，避免误伤 URL 和路径
TOKEN_RE = re.compile(r"(?<![\w/])\/([a-z0-9][a-z0-9_\-]{1,47})")


class SkillError(ValueError):
    """技能内容或名称不合法。"""


_seeded = False


def _dir() -> Path:
    global _seeded
    ensure_dirs()
    if not _seeded:
        _seeded = True
        _seed_defaults()
    return SKILLS_DIR


def directory() -> Path:
    """技能库根目录（确保已初始化）。"""
    return _dir()


def _seed_defaults() -> None:
    """首次启动时放两个示例技能。

    用 `.seeded` 标记文件判断「是否播种过」，而不是「目录是否为空」——
    否则用户把技能全删光后一重启，示例技能又会冒出来。
    """
    base = SKILLS_DIR
    marker = base / ".seeded"
    if marker.exists():
        return
    base.mkdir(parents=True, exist_ok=True)
    if not any(p.is_dir() and not p.name.startswith(".") for p in base.iterdir()):
        for slug, content in DEFAULT_SKILLS.items():
            meta, body = parse(content)
            path = base / slug / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(build(meta, body), encoding="utf-8")
    try:
        marker.write_text("seeded\n", encoding="utf-8")
    except OSError:
        pass


def _parse_value(raw: str) -> Any:
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def parse(text: str) -> tuple[dict[str, Any], str]:
    """解析 frontmatter，返回 (元信息, 正文)。手写解析，避免额外依赖。"""
    raw = (text or "").replace("\r\n", "\n")
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw.strip()
    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = _parse_value(value)
    return meta, raw[match.end() :].strip()


def build(meta: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key in ("name", "description", "version", "tags", "author"):
        if key in meta and meta[key] not in (None, "", []):
            value = meta[key]
            if isinstance(value, list):
                value = "[" + ", ".join(str(v) for v in value) + "]"
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"


def _skill_file(slug: str) -> Path:
    return _dir() / slug / "SKILL.md"


def _meta_of(slug: str) -> dict[str, Any] | None:
    path = _skill_file(slug)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = parse(text)
    meta.setdefault("name", slug)
    meta.setdefault("description", "")
    if isinstance(meta.get("tags"), str):
        meta["tags"] = [t.strip() for t in str(meta["tags"]).split(",") if t.strip()]
    meta["slug"] = slug
    meta["path"] = str(path)
    meta["body_chars"] = len(body)
    meta["updated_at"] = path.stat().st_mtime
    return meta


def list_skills() -> list[dict[str, Any]]:
    disabled = set(load_config().get("skills", {}).get("disabled") or [])
    items: list[dict[str, Any]] = []
    base = _dir()
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        meta = _meta_of(entry.name)
        if not meta:
            continue
        meta["enabled"] = entry.name not in disabled
        items.append(meta)
    return items


def get(slug: str) -> dict[str, Any]:
    slug = (slug or "").strip()
    meta = _meta_of(slug)
    if not meta:
        raise SkillError(f"技能不存在：{slug}")
    text = _skill_file(slug).read_text(encoding="utf-8")
    _, body = parse(text)
    return {**meta, "body": body, "raw": text}


def read_body(slug: str) -> str:
    return get(slug)["body"]


def create(slug: str, content: str, *, overwrite: bool = False) -> dict[str, Any]:
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise SkillError("技能名只能用小写字母、数字、下划线和短横线（1-48 字符），例如 pdf-report")
    path = _skill_file(slug)
    if path.exists() and not overwrite:
        raise SkillError(f"技能已存在：{slug}（如需修改请用 skill_update）")
    meta, body = parse(content)
    if not body:
        raise SkillError("技能正文不能为空")
    if not meta.get("description"):
        first_line = next((l.strip("# ") for l in body.splitlines() if l.strip()), "")
        meta["description"] = first_line[:120]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(meta, body), encoding="utf-8")
    return _meta_of(slug) or {}


def update(slug: str, content: str | None = None, *, description: str | None = None) -> dict[str, Any]:
    slug = (slug or "").strip().lower()
    path = _skill_file(slug)
    if not path.exists():
        raise SkillError(f"技能不存在：{slug}")
    if content is None:
        meta, body = parse(path.read_text(encoding="utf-8"))
        if description is not None:
            meta["description"] = description
        content = build(meta, body)
    else:
        meta, body = parse(content)
        if description is not None:
            meta["description"] = description
        content = build(meta, body)
    path.write_text(content, encoding="utf-8")
    return _meta_of(slug) or {}


def delete(slug: str) -> dict[str, Any]:
    slug = (slug or "").strip()
    target = _dir() / slug
    if not target.exists():
        raise SkillError(f"技能不存在：{slug}")
    for child in sorted(target.rglob("*"), reverse=True):
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    target.rmdir()
    _forget_references(slug)
    return {"ok": True, "slug": slug}


def _forget_references(slug: str) -> None:
    """技能被删后，清掉指向它的残留引用。

    两处会残留：配置里的停用名单（skills.disabled），以及每个会话的
    active_skills。不清的话会话还会继续尝试注入这个已不存在的技能，
    用户侧就会看到「技能不存在」。
    """
    from .settings import load_config, save_config

    cfg = load_config()
    disabled = set(cfg.get("skills", {}).get("disabled") or [])
    if slug in disabled:
        disabled.discard(slug)
        cfg.setdefault("skills", {})["disabled"] = sorted(disabled)
        save_config(cfg)

    try:
        from .store import store

        for conv in store.list_conversations():
            active = conv.get("active_skills") or []
            if slug in active:
                store.update_conversation(
                    conv["id"], active_skills=[s for s in active if s != slug]
                )
    except Exception:  # noqa: BLE001 — 清理残留是尽力而为，不该让删除失败
        pass


def prune(names: list[str]) -> list[str]:
    """过滤掉已不存在的技能名（会话里可能残留已删技能的 slug）。"""
    existing = {s["slug"] for s in list_skills()}
    return [n for n in dict.fromkeys(names or []) if n in existing]


def set_enabled(slug: str, enabled: bool) -> dict[str, Any]:
    from .settings import load_config, save_config

    cfg = load_config()
    disabled = set(cfg["skills"].get("disabled") or [])
    if enabled:
        disabled.discard(slug)
    else:
        disabled.add(slug)
    cfg["skills"]["disabled"] = sorted(disabled)
    save_config(cfg)
    return {"slug": slug, "enabled": enabled}


def enabled_skills() -> list[dict[str, Any]]:
    return [s for s in list_skills() if s.get("enabled")]


def extract_tokens(text: str) -> tuple[str, list[str]]:
    """解析消息里的 /技能 标记。

    返回 (改写后的文本, 命中的技能列表)。命中的 `/slug` 会被改写成
    `【使用技能：slug】`——标记留在原位置，模型能看到它修饰的是哪段话；
    没命中的（不是已启用技能的 slug）原样保留，不影响 URL / 路径。
    """
    if not text or "/" not in text:
        return text, []
    enabled = {s["slug"] for s in enabled_skills()}
    if not enabled:
        return text, []
    found: list[str] = []

    def repl(match: re.Match) -> str:
        slug = match.group(1).lower()
        if slug not in enabled:
            return match.group(0)
        if slug not in found:
            found.append(slug)
        return f"【使用技能：{slug}】"

    return TOKEN_RE.sub(repl, text), found


def resolve_file(rel: str) -> Path | None:
    """把 `skills/<slug>/SKILL.md` 这样的相对路径解析成真实文件。

    只允许访问技能库目录内部，越界或不存在返回 None。
    供产物卡片 / 预览接口使用（技能库在工作目录之外，走独立通道）。
    """
    cleaned = str(rel or "").replace("\\", "/").strip().strip("\"'").lstrip("/")
    if not cleaned.startswith("skills/"):
        return None
    base = _dir().resolve()
    target = (base / cleaned[len("skills/"):]).resolve()
    if target != base and base not in target.parents:
        return None
    if not target.is_file():
        return None
    return target


def prompt_index() -> str:
    """系统提示词里的技能目录（只列名称与用途，正文按需加载）。"""
    skills = enabled_skills()
    if not skills:
        return ""
    lines = [
        "## 可用技能（Skills）",
        f"技能库目录：{SKILLS_DIR}（每个技能是 `<目录>/SKILL.md`）。"
        "当任务与某个技能描述匹配时，先调用 `use_skill` 读取它的完整流程，再按流程执行；"
        "用户也可以在输入框里指定技能，那部分会自动注入。",
        "",
    ]
    for skill in skills:
        tags = skill.get("tags") or []
        tag_text = f"（{', '.join(tags)}）" if tags else ""
        lines.append(f"- `{skill['slug']}` — {skill['description']}{tag_text}")
    return "\n".join(lines)


def load_for_turn(names: list[str]) -> str:
    """把用户选中的技能正文拼成注入块。"""
    blocks: list[str] = []
    for name in names or []:
        try:
            skill = get(name)
        except SkillError:
            continue
        blocks.append(
            f"### 技能：{skill.get('name') or skill['slug']}\n"
            f"（来源：{skill['path']}）\n\n{skill['body']}"
        )
    if not blocks:
        return ""
    return (
        "## 本轮启用的技能\n"
        "用户明确指定了下面的技能，请严格按照其中的流程和约束完成本次任务：\n\n"
        + "\n\n---\n\n".join(blocks)
    )


DEFAULT_SKILLS: dict[str, str] = {
    "code-review": """---
name: 代码审查
description: 按固定清单审查一段代码或一个文件，输出可执行的问题列表
tags: [code, review]
---

# 代码审查流程

1. 先用 `read_file` 或 `list_files` 读到真实代码，不要凭猜测评审。
2. 需要验证行为时，用 `run_python` 或 `run_command` 在工作区跑最小复现。
3. 按下面的清单检查，只报告「确有问题」的项，不要凑数：
   - 正确性：边界条件、空值、异常路径、并发/资源泄漏
   - 安全：命令注入、路径穿越、密钥硬编码
   - 可维护性：重复逻辑、超长函数、命名歧义
   - 性能：不必要的循环、重复 IO、N+1 查询
4. 输出格式：

   | 严重度 | 位置 | 问题 | 建议 |
   | --- | --- | --- | --- |

   严重度只用 高 / 中 / 低。最后用一句话给出整体结论。
5. 如果用户只要求「看一下」，不要输出完整表格，只列出高优先级问题。
""",
    "data-report": """---
name: 数据分析报告
description: 读取工作区里的 CSV/Excel/JSON 数据，做统计并输出结论与图表建议
tags: [data, report]
---

# 数据分析流程

1. 用 `list_files` 找到数据文件，`read_file` 读取前 100 行确认字段与编码。
2. 用 `run_python` 执行 pandas 分析（若环境没有 pandas，改用 csv 模块）。
   脚本写到工作区 `.sandbox/` 下，输出只保留关键数字，避免刷屏。
3. 结论先行：先用 3 条以内的要点给出发现，再给支撑数据。
4. 涉及金额、比例时统一保留两位小数，并说明口径与样本量。
5. 需要可视化时，用 `run_python` 生成图片到工作区并告诉用户文件路径。
""",
}


def ensure_defaults() -> None:
    """首次启动时放两个示例技能，让用户知道格式长什么样。"""
    _dir()


def export_json() -> str:
    return json.dumps(list_skills(), ensure_ascii=False, indent=2)
