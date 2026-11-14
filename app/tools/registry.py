"""工具注册表：内置工具 + 动态工具（MCP），统一异步执行。

内置工具可以是普通函数，也可以是 async 函数；
MCP 工具在连接成功后动态注册进来，名字形如 mcp__server__tool。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .jsonargs import loads_lenient


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any] | None = None
    category: str = "通用"
    dangerous: bool = False
    source: str = "builtin"
    meta: dict[str, Any] = field(default_factory=dict)

    def schema(self) -> dict[str, Any]:
        params = dict(self.parameters or {"type": "object", "properties": {}})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        if params.get("properties") is None:
            params["properties"] = {}
        # 明确禁止额外字段：模型乱塞参数时能被 schema 约束住，
        # 也避免某些端点因为多出来的键直接 400。
        params.setdefault("additionalProperties", False)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }

    # ---------------------------------------------------------- 参数校验

    def props(self) -> dict[str, Any]:
        return (self.parameters or {}).get("properties") or {}

    def required(self) -> list[str]:
        keys = [str(k) for k in ((self.parameters or {}).get("required") or [])]
        # 必填但 schema 里漏写属性的，按必填算
        return keys

    def brief(self) -> str:
        """一行式参数签名，用于错误提示。"""
        parts: list[str] = []
        required = set(self.required())
        for key, spec in self.props().items():
            spec = spec or {}
            kind = spec.get("type") or "any"
            if kind == "array":
                item = (spec.get("items") or {}).get("type")
                kind = f"{item}[]" if item else "array"
            enums = spec.get("enum")
            label = f"{key}:{kind}" if kind != "any" else key
            if enums:
                label += "=" + "|".join(str(e) for e in enums)
            parts.append(label if key in required else f"[{label}]")
        return ", ".join(parts) or "（无参数）"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "dangerous": self.dangerous,
            "source": self.source,
            "parameters": self.parameters,
        }


TOOLS: dict[str, ToolSpec] = {}
DYNAMIC: dict[str, ToolSpec] = {}


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    category: str = "通用",
    dangerous: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        params = parameters or {"type": "object", "properties": {}, "required": []}
        TOOLS[name] = ToolSpec(
            name=name,
            description=description,
            parameters=params,
            func=func,
            category=category,
            dangerous=dangerous,
            source="builtin",
        )
        return func

    return decorator


def set_dynamic_tools(specs: list[dict[str, Any]]) -> None:
    """由 MCP 管理器调用：整体替换动态工具集合。"""
    DYNAMIC.clear()
    for item in specs:
        name = item.get("name")
        if not name:
            continue
        DYNAMIC[name] = ToolSpec(
            name=name,
            description=item.get("description") or name,
            parameters=item.get("parameters") or {"type": "object", "properties": {}},
            func=None,
            category=item.get("category") or "MCP",
            dangerous=False,
            source="mcp",
            meta={"server": item.get("server"), "tool": item.get("tool")},
        )


def all_tools() -> dict[str, ToolSpec]:
    return {**TOOLS, **DYNAMIC}


def schema_list(disabled: list[str] | None = None) -> list[dict[str, Any]]:
    disabled_set = set(disabled or [])
    return [
        spec.schema() for name, spec in all_tools().items() if name not in disabled_set
    ]


def list_tools() -> list[dict[str, Any]]:
    return [spec.describe() for spec in all_tools().values()]


@dataclass
class ToolResult:
    ok: bool
    content: str
    duration: float = 0.0
    raw: Any = None
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


# 单次工具返回的字符上限：超过就砍中段并提示改用更精确的参数，
# 否则一次 read_file / MCP 查询就能把上下文顶满（后面每轮都跟着变慢）。
MAX_TOOL_OUTPUT = 32000


def _cap(content: str) -> str:
    if len(content) <= MAX_TOOL_OUTPUT:
        return content
    head = content[: int(MAX_TOOL_OUTPUT * 0.7)]
    tail = content[-int(MAX_TOOL_OUTPUT * 0.2) :]
    return (
        f"{head}\n\n…（结果过长，中间省略 {len(content) - MAX_TOOL_OUTPUT} 字符。"
        "请改用更精确的参数缩小范围：read_file 用 start_line/max_lines，"
        f"search_files 用更具体的正则，命令加过滤）…\n\n{tail}"
    )


# ------------------------------------------------------------------ 名字解析


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")


def resolve_name(name: str) -> tuple[str | None, str]:
    """把模型写的工具名对上真实工具。

    实测最常见的一类失败：MCP 工具实际叫 `mcp__srv__tool`，模型只写 `tool`。
    这里按「完全一致 → 归一化一致 → 后缀唯一匹配」三级兜底，
    匹配到唯一候选就直接用，并告诉调用方发生了重定向。
    """
    raw = (name or "").strip()
    if not raw:
        return None, ""
    pool = all_tools()
    if raw in pool:
        return raw, ""

    key = _norm(raw)
    alias = {_norm(n): n for n in pool}
    if key in alias:
        return alias[key], f"按归一化名字匹配到 {alias[key]}"

    if raw.startswith("functions."):
        return resolve_name(raw[len("functions.") :])

    # MCP 原始工具名（不带 mcp__server__ 前缀）
    for spec in pool.values():
        if spec.source == "mcp" and _norm(str(spec.meta.get("tool") or "")) == key:
            return spec.name, f"按 MCP 原始工具名匹配到 {spec.name}"

    # 后缀唯一匹配：foo_bar 命中了 a__b__foo_bar
    suffix = [n for n in pool if _norm(n).endswith("__" + key) or _norm(n).split("__")[-1] == key]
    if len(suffix) == 1:
        return suffix[0], f"按后缀匹配到 {suffix[0]}"
    return None, ""


def _similar(name: str, limit: int = 6) -> list[str]:
    key = _norm(name)
    scored: list[tuple[int, str]] = []
    for candidate in all_tools():
        other = _norm(candidate)
        score = 0
        if other.startswith(key) or key.startswith(other):
            score = 3
        elif key and (key in other or other in key):
            score = 2
        else:
            common = len(set(key) & set(other))
            if common >= max(3, len(key) // 2):
                score = 1
        if score:
            scored.append((score, candidate))
    scored.sort(key=lambda item: (-item[0], len(item[1])))
    return [n for _, n in scored[:limit]]


# ------------------------------------------------------------------ 参数整形


_TRUE = {"true", "1", "yes", "y", "on", "是"}
_FALSE = {"false", "0", "no", "n", "off", "否"}


def _coerce(key: str, value: Any, spec: dict[str, Any]) -> tuple[Any, str]:
    """按 schema 尽量把值转成正确类型，返回 (值, 错误说明)。"""
    spec = spec or {}
    kind = spec.get("type")
    if value is None:
        return None, ""

    if kind == "string":
        if isinstance(value, str):
            pass
        elif isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        else:
            value = str(value)
        enums = spec.get("enum")
        if enums and value not in enums:
            return value, f"{key} 只能是 {' / '.join(map(str, enums))}，收到的是「{value}」"
        return value, ""

    if kind == "integer":
        if isinstance(value, bool):
            return int(value), ""
        if isinstance(value, int):
            return value, ""
        if isinstance(value, float):
            return int(value), ""
        if isinstance(value, str):
            text = value.strip().replace(",", "").replace("，", "")
            match = re.match(r"^-?\d+(\.\d+)?", text)
            if match:
                return int(float(match.group(0))), ""
            return value, f"{key} 需要整数，收到的是「{value[:20]}」"
        return value, f"{key} 需要整数"

    if kind == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value), ""
        if isinstance(value, str):
            try:
                return float(value.strip().replace(",", "")), ""
            except ValueError:
                return value, f"{key} 需要数字，收到的是「{value[:20]}」"
        return value, f"{key} 需要数字"

    if kind == "boolean":
        if isinstance(value, bool):
            return value, ""
        if isinstance(value, (int, float)):
            return bool(value), ""
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _TRUE:
                return True, ""
            if low in _FALSE:
                return False, ""
            return value, f"{key} 需要 true/false，收到的是「{value[:20]}」"
        return value, f"{key} 需要 true/false"

    if kind == "array":
        if isinstance(value, list):
            return value, ""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return [], ""
            if text.startswith("["):
                parsed, info = loads_lenient(text)
                if isinstance(parsed, list):
                    return parsed, ""
            # 逗号分隔的裸列表也算
            return [p.strip() for p in re.split(r"[,，\n]", text) if p.strip()], ""
        return [value], ""

    if kind == "object":
        if isinstance(value, dict):
            return value, ""
        if isinstance(value, str):
            parsed, _info = loads_lenient(value)
            if isinstance(parsed, dict):
                return parsed, ""
            return value, f"{key} 需要 JSON 对象"
        return value, f"{key} 需要 JSON 对象"

    return value, ""


def prepare_arguments(
    spec: ToolSpec, arguments: Any, raw: str | None = None, strict: bool = True
) -> tuple[dict[str, Any], list[str], list[str]]:
    """把模型给的参数整理成能直接调用的 kwargs。

    返回 (kwargs, 阻断性错误, 提示)。
    """
    errors: list[str] = []
    notes: list[str] = []

    if isinstance(arguments, str):
        parsed, info = loads_lenient(arguments)
        arguments = parsed if isinstance(parsed, dict) else {}
        if info.get("truncated"):
            errors.append(
                "参数 JSON 被截断（通常是输出撞上 max_tokens 上限），内容不完整，已放弃执行。"
            )
        elif not info.get("ok"):
            errors.append(f"参数不是合法 JSON：{info.get('detail')}")

    if not isinstance(arguments, dict):
        arguments = {}

    raw_args = dict(arguments)
    if "_raw" in raw_args:
        inner = raw_args.pop("_raw")
        parsed, info = loads_lenient(inner if isinstance(inner, str) else raw)
        if isinstance(parsed, dict):
            raw_args.update(parsed)
            if info.get("repaired"):
                notes.append(f"参数已自动修复：{info.get('detail')}")
        else:
            errors.append(
                "参数不是合法 JSON（常见原因：内容太长被截断、字符串里的换行没转义）。"
            )

    props = spec.props()
    kwargs: dict[str, Any] = {}
    for key, value in raw_args.items():
        if key in props or not props or not strict:
            kwargs[key] = value
    unknown = [k for k in raw_args if k not in props and props] if strict else []

    if unknown and not errors:
        allowed = spec.brief()
        errors.append(
            f"参数名不对：{'、'.join(unknown)} 不存在。这个工具只接受 → {allowed}"
        )

    for key, value in list(kwargs.items()):
        coerced, problem = _coerce(key, value, props.get(key) or {})
        if problem:
            errors.append(problem)
        else:
            kwargs[key] = coerced

    missing = [k for k in spec.required() if kwargs.get(k) in (None, "", [], {})]
    if missing:
        detail = []
        for key in missing:
            meta = props.get(key) or {}
            desc = meta.get("description") or ""
            detail.append(f"{key}（{meta.get('type') or 'any'}{'：' + desc if desc else ''}）")
        errors.append(f"缺少必填参数：{'；'.join(detail)}")

    if errors:
        hint = spec.brief()
        errors.append(f"正确的参数格式 → {spec.brief() if hint else '（无参数）'}")
    return kwargs, errors, notes


async def aexecute(
    name: str, arguments: dict[str, Any] | None, raw: str | None = None
) -> ToolResult:
    started = time.perf_counter()
    resolved, note = resolve_name(name)
    if resolved is None:
        similar = _similar(name)
        tail = f"，你可能想用：{'、'.join(similar)}" if similar else ""
        return ToolResult(
            ok=False,
            content=f"工具 {name} 不存在{tail}。请只调用工具列表里出现过的名字。",
            error="not_found",
            meta={"requested": name},
        )
    spec = all_tools()[resolved]

    try:
        if spec.source == "mcp":
            from ..mcp_client import manager

            kwargs, errors, notes = prepare_arguments(
                spec, dict(arguments or {}), raw, strict=False
            )
            if errors:
                return ToolResult(
                    ok=False,
                    content="调用参数有误，未执行：\n- " + "\n- ".join(errors),
                    duration=time.perf_counter() - started,
                    error="bad_arguments",
                )
            result = await manager().call_tool(
                spec.meta.get("server") or "", spec.meta.get("tool") or resolved, kwargs
            )
            return ToolResult(
                ok=not result.startswith("[MCP 工具返回错误]"),
                content=_cap(result),
                duration=time.perf_counter() - started,
            )

        kwargs, errors, notes = prepare_arguments(spec, dict(arguments or {}), raw)
        if errors:
            return ToolResult(
                ok=False,
                content="调用参数有误，未执行：\n- " + "\n- ".join(errors),
                duration=time.perf_counter() - started,
                error="bad_arguments",
            )
        inline = [f"（{n}）" for n in ([note] if note else []) + notes]
        prefix = "".join(inline)

        if inspect.iscoroutinefunction(spec.func):
            raw_out = await spec.func(**kwargs)  # type: ignore[misc]
        else:
            raw_out = await asyncio.to_thread(spec.func, **kwargs)  # type: ignore[misc]
        duration = time.perf_counter() - started
        content = raw_out if isinstance(raw_out, str) else json.dumps(raw_out, ensure_ascii=False, default=str)
        content = _cap(content)
        if prefix:
            content = prefix + "\n" + content
        return ToolResult(ok=True, content=content, duration=duration, raw=raw_out)
    except TypeError as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return ToolResult(
            ok=False,
            content=f"参数不匹配：{detail}\n这个工具的参数格式 → {spec.brief()}",
            duration=time.perf_counter() - started,
            error="bad_arguments",
        )
    except Exception as exc:  # noqa: BLE001
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return ToolResult(
            ok=False,
            content=f"工具执行异常：{detail}",
            duration=time.perf_counter() - started,
            error=detail,
        )


def execute(name: str, arguments: dict[str, Any] | None) -> ToolResult:
    """同步入口（仅内置同步工具），保留给脚本/测试使用。"""
    resolved, _note = resolve_name(name)
    spec = all_tools().get(resolved or "")
    if spec is None or spec.func is None:
        return ToolResult(ok=False, content=f"工具 {name} 不可同步执行", error="not_found")
    started = time.perf_counter()
    kwargs, errors, _notes = prepare_arguments(spec, dict(arguments or {}))
    if errors:
        return ToolResult(
            ok=False,
            content="调用参数有误，未执行：\n- " + "\n- ".join(errors),
            duration=time.perf_counter() - started,
            error="bad_arguments",
        )
    try:
        raw = spec.func(**kwargs)
        content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
        return ToolResult(ok=True, content=content, duration=time.perf_counter() - started, raw=raw)
    except Exception as exc:  # noqa: BLE001
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return ToolResult(
            ok=False,
            content=f"工具执行异常：{detail}",
            duration=time.perf_counter() - started,
            error=detail,
        )
