"""元能力工具：技能管理、MCP 配置、长期记忆。

这些工具让模型可以「自己长本事」——按用户要求新建技能、
接入 MCP 服务、把重要事实写进长期记忆。
"""

from __future__ import annotations

import json
from typing import Any

from .. import memory, skills
from ..mcp_client import list_servers, manager, remove_server, upsert_server
from ..settings import MCP_CONFIG_PATH, SKILLS_DIR
from .jsonargs import loads_lenient
from .registry import tool


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _as_map(value: Any, label: str) -> tuple[dict[str, str], str]:
    """env / headers 既接受对象，也接受 JSON 字符串（兼容旧调用）。"""
    if value in (None, "", {}):
        return {}, ""
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}, ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}, ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}, f"{label} 需要是对象或 JSON 字符串，例如 {{\"API_KEY\":\"xxx\"}}"
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}, ""
    return {}, f"{label} 需要是对象或 JSON 字符串，例如 {{\"API_KEY\":\"xxx\"}}"


# ---------------------------------------------------------------- 技能

@tool(
    name="skill_list",
    description=f"列出技能库里的所有技能（目录固定为 {SKILLS_DIR}）。",
    parameters={"type": "object", "properties": {}, "required": []},
    category="技能",
)
def skill_list() -> str:
    items = skills.list_skills()
    if not items:
        return "技能库为空"
    return "\n".join(
        f"- {s['slug']}｜{s.get('name')}｜{'启用' if s.get('enabled') else '停用'}｜"
        f"{s.get('description', '')[:100]}"
        for s in items
    )


@tool(
    name="use_skill",
    description=(
        "加载某个技能的完整说明（SKILL.md 正文），然后按里面的步骤执行任务。"
        "当技能索引里出现与任务匹配的技能时，先调用它再动手。名称必须是 skill_list 里的 slug。"
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "技能 slug，例如 weekly-report"}},
        "required": ["name"],
    },
    category="技能",
)
def use_skill(name: str) -> str:
    try:
        skill = skills.get(name)
    except skills.SkillError as exc:
        available = "、".join(s["slug"] for s in skills.list_skills()) or "（空）"
        return f"{exc}。可用技能：{available}"
    body = skill["body"]
    if len(body) > 14000:
        body = body[:14000] + "\n…（技能正文过长已截断）"
    return f"# 技能：{skill.get('name') or skill['slug']}\n\n{body}"


@tool(
    name="skill_create",
    description=(
        f"新建一个技能，写入 {SKILLS_DIR}/<name>/SKILL.md。"
        "content 用 Markdown，建议以 `---\\nname: 显示名\\ndescription: 一句话说明\\n---` 开头。"
        "写清楚适用场景、步骤、输出格式，让以后遇到同类任务能照做。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能标识，只能用小写字母/数字/下划线/短横线，如 weekly-report",
            },
            "content": {"type": "string", "description": "SKILL.md 全文"},
            "overwrite": {"type": "boolean", "description": "已存在时是否覆盖"},
        },
        "required": ["name", "content"],
    },
    category="技能",
)
def skill_create(name: str, content: str, overwrite: bool = False) -> str:
    try:
        meta = skills.create(name, content, overwrite=bool(overwrite))
    except skills.SkillError as exc:
        return f"创建失败：{exc}"
    return _json({"ok": True, **{k: meta.get(k) for k in ("slug", "name", "description", "path")}})


@tool(
    name="skill_update",
    description="修改已有技能的正文或描述（content 传全文，会整体替换）。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名（slug）"},
            "content": {"type": "string", "description": "新的 SKILL.md 全文；只改描述时可留空"},
            "description": {"type": "string", "description": "只更新一句话描述"},
        },
        "required": ["name"],
    },
    category="技能",
)
def skill_update(name: str, content: str = "", description: str = "") -> str:
    try:
        meta = skills.update(
            name, content or None, description=description or None
        )
    except skills.SkillError as exc:
        return f"修改失败：{exc}"
    return _json({"ok": True, "slug": meta.get("slug"), "path": meta.get("path")})


@tool(
    name="skill_delete",
    description="删除一个技能及其目录。",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "技能名（slug）"}},
        "required": ["name"],
    },
    category="技能",
    dangerous=True,
)
def skill_delete(name: str) -> str:
    try:
        return _json(skills.delete(name))
    except skills.SkillError as exc:
        return f"删除失败：{exc}"


# ---------------------------------------------------------------- MCP

@tool(
    name="mcp_list_servers",
    description=f"列出已配置的 MCP 服务、连接状态与可用工具（配置固定为 {MCP_CONFIG_PATH}）。",
    parameters={"type": "object", "properties": {}, "required": []},
    category="MCP",
)
def mcp_list_servers() -> str:
    servers = list_servers()
    if not servers:
        return "还没有配置任何 MCP 服务"
    return _json(servers)


@tool(
    name="mcp_add_server",
    description=(
        "新增或更新一个 MCP 服务配置（写入 data/mcp.json），保存后用 mcp_connect 连接，无需重启。"
        "stdio 例子：transport=stdio, command=npx, args=['-y','@modelcontextprotocol/server-filesystem','<绝对路径>']；"
        "http 例子：transport=http, url=https://example.com/mcp, token=<令牌>。"
        "密钥可以用 ${ENV_VAR} 占位，避免把明文写进配置。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "服务名，唯一，例如 filesystem"},
            "transport": {
                "type": "string",
                "enum": ["stdio", "http", "sse"],
                "description": "传输方式，本地进程用 stdio，远程服务用 http/sse。默认 stdio",
                "default": "stdio",
            },
            "command": {"type": "string", "description": "stdio 启动命令，如 npx / uvx / python"},
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "命令参数列表，例如 ['-y','@modelcontextprotocol/server-filesystem','D:/work']",
            },
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "环境变量键值对，例如 {\"API_KEY\":\"xxx\"}；也接受 JSON 字符串",
            },
            "url": {"type": "string", "description": "http / sse 的服务地址"},
            "token": {"type": "string", "description": "访问令牌，会自动加 Authorization: Bearer"},
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "额外请求头，例如 {\"X-API-Key\":\"xxx\"}；也接受 JSON 字符串",
            },
            "description": {"type": "string", "description": "这个服务能做什么，一句话"},
            "enabled": {
                "type": "boolean",
                "description": "是否启用，默认 true",
                "default": True,
            },
        },
        "required": ["name"],
    },
    category="MCP",
    dangerous=True,
)
async def mcp_add_server(
    name: str,
    transport: str = "stdio",
    command: str = "",
    args: list[str] | None = None,
    env: Any = None,
    url: str = "",
    token: str = "",
    headers: Any = None,
    description: str = "",
    enabled: bool = True,
) -> str:
    env_map, env_problem = _as_map(env, "env")
    header_map, header_problem = _as_map(headers, "headers")
    if env_problem or header_problem:
        return env_problem or header_problem
    try:
        result = upsert_server(
            name,
            {
                "transport": transport or "stdio",
                "command": command,
                "args": args or [],
                "env": env_map,
                "url": url,
                "token": token,
                "headers": header_map,
                "description": description,
                "enabled": enabled,
            },
        )
    except ValueError as exc:
        return f"配置失败：{exc}"
    return _json({**result, "next": "调用 mcp_connect 建立连接后即可使用它的工具"})


@tool(
    name="mcp_remove_server",
    description="删除一个 MCP 服务配置并断开连接。",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "服务名"}},
        "required": ["name"],
    },
    category="MCP",
    dangerous=True,
)
async def mcp_remove_server(name: str) -> str:
    try:
        await manager().disconnect(name)
        return _json(remove_server(name))
    except KeyError as exc:
        return str(exc)


@tool(
    name="mcp_connect",
    description="连接（或重连）一个已配置的 MCP 服务，返回发现的工具列表。",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "服务名"}},
        "required": ["name"],
    },
    category="MCP",
)
async def mcp_connect(name: str) -> str:
    runtime = await manager().reconnect(name)
    if runtime.state != "ready":
        return f"连接失败：{runtime.error}"
    tools = [
        {"name": t["exposed"], "original": t["name"], "description": (t.get("description") or "")[:120]}
        for t in runtime.tools.values()
    ]
    return _json({"ok": True, "server": name, "tools": tools})


@tool(
    name="mcp_list_tools",
    description=(
        "拉取某个 MCP 服务的工具清单和参数 schema。"
        "调用该服务的工具前不确定参数时先用它；返回里的 exposed 是可直接调用的完整名字。"
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "MCP 服务名"}},
        "required": ["name"],
    },
    category="MCP",
)
async def mcp_list_tools(name: str) -> str:
    try:
        tools = await manager().list_tools(name)
    except Exception as exc:  # noqa: BLE001
        return f"获取失败：{exc}"
    if not tools:
        return f"{name} 没有暴露任何工具"
    return _json(
        [
            {
                "name": t["name"],
                "exposed": t["exposed"],
                "description": (t.get("description") or "")[:200],
                "parameters": t.get("parameters"),
            }
            for t in tools
        ]
    )


@tool(
    name="mcp_call_tool",
    description=(
        "调用某个 MCP 服务的指定工具（不需要它已注册到工具列表）。"
        "tool 填原始工具名（不带 mcp__ 前缀），arguments 直接给对象，键名照抄 mcp_list_tools 里的 schema。"
        "不确定参数时先用 mcp_list_tools 看一遍。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "MCP 服务名，例如 caihui_mcp"},
            "tool": {"type": "string", "description": "原始工具名，不带 mcp__ 前缀"},
            "arguments": {
                "type": "object",
                "description": "工具参数对象，例如 {\"stock_code\":\"000001\"}；没有参数就传空对象",
            },
        },
        "required": ["name", "tool"],
    },
    category="MCP",
    dangerous=True,
)
async def mcp_call_tool(name: str, tool: str, arguments: Any = None) -> str:
    if isinstance(arguments, str):
        parsed, info = loads_lenient(arguments)
        if not info.get("ok"):
            return f"arguments 不是合法 JSON：{info.get('detail')}"
        arguments = parsed
    if arguments in (None, ""):
        args: dict[str, Any] = {}
    elif isinstance(arguments, dict):
        args = arguments
    else:
        return "arguments 需要是对象，例如 {\"path\":\"README.md\"}"
    result = await manager().call_tool_detail(name, tool, args)
    return _json(
        {
            "ok": result["ok"],
            "is_error": result["is_error"],
            "duration_ms": result["duration_ms"],
            "content": result["content"][:6000],
        }
    )


@tool(
    name="mcp_disconnect",
    description="断开一个 MCP 服务的连接（保留配置）。",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "服务名"}},
        "required": ["name"],
    },
    category="MCP",
)
async def mcp_disconnect(name: str) -> str:
    await manager().disconnect(name)
    return _json({"ok": True, "server": name, "state": "disconnected"})


# ---------------------------------------------------------------- 记忆

@tool(
    name="memory_read",
    description="读取长期记忆文件（跨会话保存的用户偏好、项目约定、事实）。",
    parameters={"type": "object", "properties": {}, "required": []},
    category="记忆",
)
def memory_read() -> str:
    text = memory.read_text()
    if not text.strip():
        return "长期记忆为空"
    return text[:12000]


@tool(
    name="memory_save",
    description=(
        "把值得长期记住的事实写进记忆文件（之后会自动出现在系统提示里）。"
        "适合用户的稳定偏好、技术栈约定、长期项目背景、明确做过的决定；不要记一次性的临时内容。"
        "一件事一条，写成主谓宾都齐全的短句。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "一句话事实，例如「用户偏好用 TypeScript 而不是 JavaScript」"},
            "section": {
                "type": "string",
                "enum": ["用户偏好", "项目约定", "事实与决定"],
                "description": "归类段落，默认「事实与决定」",
                "default": "事实与决定",
            },
        },
        "required": ["content"],
    },
    category="记忆",
)
def memory_save(content: str, section: str = "事实与决定") -> str:
    try:
        return _json(memory.save(content, section))
    except ValueError as exc:
        return f"保存失败：{exc}"


@tool(
    name="memory_forget",
    description="从长期记忆里删除包含某个关键词的条目（用户说「忘掉…」或纠正旧信息时使用）。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词"},
            "section": {"type": "string", "description": "限定段落，可留空"},
        },
        "required": ["query"],
    },
    category="记忆",
)
def memory_forget(query: str, section: str = "") -> str:
    try:
        return _json(memory.forget(query, section or None))
    except ValueError as exc:
        return f"删除失败：{exc}"


# ---------------------------------------------------------------- 定时任务

@tool(
    name="schedule_create",
    description=(
        "创建定时任务：到点自动在一个会话里发一条消息给模型。"
        "when 直接填用户原话（「每天 9:00」「每周一 10:30」「每隔 2 小时」「明天 15:00」），不要自己换算成 cron。"
        "prompt 要自包含——到点执行时看不到当前这段对话，必须写清楚做什么、输出什么。"
        "不填 conversation_id 会自动建一个「⏰ 标题」的会话，每次结果都追加在里面。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "任务名，简短，例如「每日新闻摘要」"},
            "when": {
                "type": "string",
                "description": "时间规则，用用户原话，例如「每天 9:00」「每周五 18:00」「每隔 30 分钟」",
            },
            "prompt": {
                "type": "string",
                "description": "到点要执行的任务，写清楚背景、要求、输出格式",
            },
            "conversation_id": {"type": "string", "description": "写进哪个会话；留空则自动新建"},
        },
        "required": ["when", "prompt"],
    },
    category="定时任务",
)
def schedule_create(when: str, prompt: str, title: str = "", conversation_id: str = "") -> str:
    import time as _time

    from .. import schedule_parse
    from ..runs import auto_title
    from ..store import store

    parsed = schedule_parse.parse(when)
    if not parsed.get("ok"):
        return f"时间规则没看懂：{parsed.get('hint')}"
    fields: dict[str, Any] = {
        "title": (title or auto_title(prompt, 18))[:18],
        "prompt": prompt,
        "conversation_id": conversation_id or None,
        "kind": parsed["kind"],
        "enabled": True,
    }
    for key in ("at", "time_of_day", "weekday", "every_minutes"):
        if parsed.get(key) is not None:
            fields[key] = parsed[key]
    fields["next_run_at"] = schedule_parse.next_run(fields)
    schedule = store.add_schedule(**fields)
    next_at = schedule.get("next_run_at")
    return _json(
        {
            "ok": True,
            "id": schedule["id"],
            "title": schedule["title"],
            "when": schedule_parse.describe(schedule),
            "next_run": _time.strftime("%Y-%m-%d %H:%M", _time.localtime(next_at))
            if next_at
            else None,
            "note": "任务会在后台到点自动执行，结果写进它自己的会话里",
        }
    )


@tool(
    name="schedule_list",
    description="列出所有定时任务：时间规则、下次触发、上次运行状态、目标会话。",
    parameters={"type": "object", "properties": {}, "required": []},
    category="定时任务",
)
def schedule_list() -> str:
    import time as _time

    from .. import schedule_parse
    from ..store import store

    items = store.list_schedules()
    if not items:
        return "还没有定时任务"
    lines = []
    for item in items:
        next_at = item.get("next_run_at") or schedule_parse.next_run(item)
        lines.append(
            f"- {item['id']}｜{item.get('title') or '未命名'}｜{schedule_parse.describe(item)}｜"
            f"{'启用' if item.get('enabled') else '停用'}｜"
            f"下次 {_time.strftime('%m-%d %H:%M', _time.localtime(next_at)) if next_at else '无'}"
            f"｜已跑 {item.get('run_count') or 0} 次"
            f"｜会话 {item.get('conversation_id') or '（自动新建）'}"
        )
    return "\n".join(lines)


@tool(
    name="schedule_delete",
    description="删除一个定时任务（id 从 schedule_list 拿）。",
    parameters={
        "type": "object",
        "properties": {"schedule_id": {"type": "string", "description": "任务 id，如 s_xxxx"}},
        "required": ["schedule_id"],
    },
    category="定时任务",
    dangerous=True,
)
def schedule_delete(schedule_id: str) -> str:
    from ..store import store

    if store.get_schedule(schedule_id) is None:
        return f"任务不存在：{schedule_id}"
    store.delete_schedule(schedule_id)
    return _json({"ok": True, "deleted": schedule_id})


@tool(
    name="schedule_toggle",
    description="启用或停用一个定时任务。",
    parameters={
        "type": "object",
        "properties": {
            "schedule_id": {"type": "string", "description": "任务 id"},
            "enabled": {"type": "boolean", "description": "true 启用 / false 停用"},
        },
        "required": ["schedule_id", "enabled"],
    },
    category="定时任务",
)
def schedule_toggle(schedule_id: str, enabled: bool) -> str:
    from .. import schedule_parse
    from ..store import store

    schedule = store.get_schedule(schedule_id)
    if schedule is None:
        return f"任务不存在：{schedule_id}"
    next_at = schedule_parse.next_run(schedule) if enabled else None
    store.update_schedule(schedule_id, enabled=bool(enabled), next_run_at=next_at)
    return _json({"ok": True, "id": schedule_id, "enabled": bool(enabled)})
