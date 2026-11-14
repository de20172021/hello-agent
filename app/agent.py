"""Agent 主循环：提示词装配 + 上下文压缩 + 流式生成 + 工具调用。"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import platform
import time
from typing import Any, AsyncIterator

from . import context as ctx_util
from . import memory, multimodal, runtime, skills, spaces, workspace
from .llm import ProviderError, RunHandle, get_provider, vision_capable
from .mcp_client import tool_hint as mcp_hint
from .settings import MCP_CONFIG_PATH, SKILLS_DIR, get_model_profile, load_config, paths_info
from .store import Store, store
from .tools import ToolResult, aexecute, schema_list

WORKSPACE_RULES = """## 工作方式

- 需要事实时先查再用：时间/计算/网络/文件/命令都有对应工具，不要凭空假设。
- 调工具前先确认参数：键名和取值照工具 schema 来，不要自己加没定义的键。
- 只写一次最长能写的内容是有限的：单个文件超过 1500 字就分段 `write_file`（第 1 段 append=false，后续 append=true）。
- 工具报错时先读错误里的「正确的参数格式」，改对再试；同一条命令不要原样重试第二次。
- 文件操作都在当前工作目录里进行，路径用相对路径（例如 `reports/a.md`）；写完后在回答里给出文件路径，用户会在界面上的产物卡片里看到它。
- 用户可能直接发图片或文档：图片会一起发给你看（多模态模型），其它文件会被内联或保存到工作目录 `uploads/` 下；需要细节时用工具读原文件。
- 处理多步任务时先在心里拆好步骤，再按顺序调用工具；不要一次并发太多无关工具。
- 执行命令或脚本后，先看 stdout/stderr 再下结论；失败要说明原因和下一步，不要假装成功。
- 回答用简体中文和 Markdown，结构清晰；不要输出与任务无关的自我描述。
- 生成的成品（报告、网页、表格、图表）优先用 write_file 落到当前工作目录，用户在界面上可以直接预览 Markdown / HTML。"""


def build_system_prompt(
    conversation: dict[str, Any],
    config: dict[str, Any],
    active_skills: list[str] | None = None,
) -> str:
    """拼系统提示词。

    注意：这里只能放**稳定的内容**。易变的（当前时间、工作区文件清单）放在
    `build_volatile_prompt` 里，由 build_messages 追加到提示词末尾 —— 否则
    每轮一个时间戳，前缀 KV 缓存会从头失效。
    """
    base = (conversation.get("system_prompt") or "").strip() or config["defaults"]["system_prompt"]
    paths = paths_info()
    space = None
    space_id = conversation.get("space_id")
    if space_id:
        space = store.get_space(space_id)
    scope = spaces.scope_view(space)
    work_dir = spaces.workdir_for(conversation)
    where = (
        "任务（每个对话一个独立会话目录）"
        if scope["type"] == "task"
        else "空间"
    )

    parts: list[str] = [base, ""]

    env_lines = [
        "## 运行环境",
        f"- 系统：{platform.system()} {platform.release()}",
        f"- 工作位置：{scope['title']}（{where}）",
        f"- 工作目录（你唯一可读写的目录）：`{work_dir}`",
        f"- 技能库（固定）：`{paths['skills']}`，每个技能是 `<名字>/SKILL.md`",
        f"- MCP 配置（固定）：`{paths['mcp']}`",
        f"- 长期记忆（固定）：`{paths['memory']}`",
    ]
    parts.append("\n".join(env_lines))

    if scope.get("instructions"):
        parts.append(f"## 这个空间的约定\n{scope['instructions'].strip()}")

    parts.append(WORKSPACE_RULES)

    if config.get("memory", {}).get("enabled", True):
        block = memory.prompt_block()
        if block:
            parts.append(block)

    skill_index = skills.prompt_index()
    if skill_index:
        parts.append(skill_index)

    parts.append(mcp_hint())

    injected = skills.load_for_turn(active_skills or [])
    if injected:
        parts.append(injected)

    parts.append(
        "## 自我扩展\n"
        "- 用户要求接入新的 MCP 服务时，用 `mcp_add_server` 写配置、`mcp_connect` 连接，"
        "工具会立即出现在你的工具列表里。\n"
        "- 用户要求沉淀方法或规范时，用 `skill_create` 写成技能，以后可以让用户勾选复用。\n"
        "- 发现值得长期记住的偏好或事实时，用 `memory_save` 写入记忆；用户纠正时先 `memory_forget` 再记。"
    )
    return "\n\n".join(part for part in parts if part is not None).strip()


def build_volatile_prompt() -> str:
    """每轮都可能变的内容：放到提示词最后，避免打断前面的缓存。"""
    now = _dt.datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    lines = [
        "## 当前状态",
        f"- 现在时间：{now.strftime('%Y-%m-%d %H:%M')}（周{weekday}）",
    ]
    top_level: list[str] = []
    try:
        for entry in sorted(workspace.root().iterdir(), key=lambda p: p.name)[:20]:
            top_level.append(f"{'📁' if entry.is_dir() else '📄'} {entry.name}")
    except OSError:
        pass
    lines.append("- 工作目录顶层内容：" + ("、".join(top_level) if top_level else "（空）"))
    return "\n".join(lines)


def _fill_cancelled_tool_calls(store, conv_id: str, calls: list, handled: set[str]) -> None:
    """给还没拿到结果的工具调用补一条占位回复。

    OpenAI 协议要求 assistant 里的每个 tool_call 后面必须跟着对应的 tool 消息，
    中途被停止时少一条都会让下一轮请求直接 400。
    """
    for call in calls:
        if call.id in handled:
            continue
        try:
            store.add_message(
                conv_id,
                "tool",
                content="[已取消]",
                tool_call_id=call.id,
                name=call.name,
                stats={"ok": False, "cancelled": True},
            )
        except Exception:  # noqa: BLE001
            pass


def build_messages(
    conversation: dict[str, Any],
    messages: list[dict[str, Any]],
    config: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """把数据库消息转成模型输入，摘要覆盖的部分用一条 system 消息替代。

    带附件的用户消息会按模型能力组装成多模态内容（图片走 image_url，
    文本附件直接内联），纯文本模型则降级成文字说明。
    """
    active = conversation.get("active_skills") or []
    system = build_system_prompt(conversation, config, active)
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]

    if profile is None:
        profile = get_model_profile(conversation.get("model_id")) or {}
    vision = vision_capable(profile)

    summary = (conversation.get("summary") or "").strip()
    until = int(conversation.get("summary_until") or 0)
    if summary:
        out.append(
            {
                "role": "system",
                "content": (
                    "以下是本次会话较早内容的摘要（原始消息仍保留在界面上，"
                    "需要精确细节时可以请用户回看）：\n\n" + summary
                ),
            }
        )

    tail = messages
    if until:
        tail = [m for m in messages if int(m.get("seq") or 0) > until]
        while tail and tail[0].get("role") == "tool":
            tail.pop(0)

    # 历史里的图片不必每轮都重发一遍：只保留最近几张，其余降级成文字说明
    keep_images = multimodal.image_allowance(tail) if vision else set()

    for msg in tail:
        role = msg["role"]
        if role in ("user", "assistant"):
            content: Any = msg.get("content") or ""
            attachments = msg.get("attachments") or []
            if role == "user" and attachments:
                content = multimodal.build_user_content(
                    content,
                    attachments,
                    vision=vision,
                    include_images=str(msg.get("id") or "") in keep_images,
                )
            item: dict[str, Any] = {"role": role, "content": content}
            if role == "assistant":
                if msg.get("reasoning"):
                    item["reasoning_content"] = msg["reasoning"]
                if msg.get("tool_calls"):
                    item["tool_calls"] = msg["tool_calls"]
            out.append(item)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "content": msg.get("content") or "",
                    "tool_call_id": msg.get("tool_call_id") or "",
                    "name": msg.get("name") or "",
                }
            )

    # 易变内容插在最后一条用户消息之前（工具轮里没有用户消息就追加到末尾），
    # 这样前面「系统提示 + 工具 schema + 历史」这一段整体保持稳定。
    volatile = {"role": "system", "content": build_volatile_prompt()}
    if out and out[-1].get("role") == "user":
        out.insert(len(out) - 1, volatile)
    else:
        out.append(volatile)
    return out


def resolve_params(
    conversation: dict[str, Any], config: dict[str, Any], profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    defaults = config["defaults"]
    level = conversation.get("thinking_level") or defaults.get("thinking_level", "medium")
    thinking = conversation.get("thinking")
    if thinking is None:
        thinking = bool(profile.get("enable_thinking") if profile else defaults.get("thinking"))
    if str(level) == "off":
        thinking = False
    return {
        "conversation_id": conversation.get("id"),
        "temperature": conversation.get("temperature")
        if conversation.get("temperature") is not None
        else defaults["temperature"],
        "top_p": conversation.get("top_p")
        if conversation.get("top_p") is not None
        else defaults["top_p"],
        "max_tokens": conversation.get("max_tokens")
        if conversation.get("max_tokens")
        else defaults["max_tokens"],
        "system_prompt": conversation.get("system_prompt") or defaults["system_prompt"],
        "thinking": bool(thinking),
        "thinking_level": level,
    }


def _disabled_tools(config: dict[str, Any]) -> list[str]:
    disabled = set(config["tools"].get("disabled") or [])
    if not config["tools"].get("allow_shell", True):
        disabled.update({"run_command", "run_python"})
    if not config["memory"].get("enabled", True):
        disabled.update({"memory_save", "memory_forget", "memory_read"})
    return sorted(disabled)


# 各家后端「上下文超长」的措辞都不一样（OpenAI 兼容端点 / vLLM / 通义 / 本地 transformers），
# 统一用关键词判定，命中后压缩历史并重试同一轮。
OVERFLOW_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "context window",
    "too many tokens",
    "token limit",
    "reduce the length",
    "prompt is too long",
    "input is too long",
    "input length",
    "range of input length",
    "exceeds the maximum",
    "context overflow",
    "超出上下文",
    "上下文长度",
    "超过最大",
)


def is_context_overflow(message: str) -> bool:
    low = (message or "").lower()
    if not low:
        return False
    return any(hint in low for hint in OVERFLOW_HINTS)


async def compact_conversation(
    *,
    conversation: dict[str, Any],
    profile: dict[str, Any],
    store: Store,
    handle: RunHandle,
    force: bool = False,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把较早的消息压缩成摘要，返回压缩结果（不流式）。"""
    config = load_config()
    conv_id = conversation["id"]
    all_msgs = store.list_messages(conv_id)
    until = int(conversation.get("summary_until") or 0)
    pending = [m for m in all_msgs if int(m.get("seq") or 0) > until]
    keep = int(config["defaults"].get("compact_keep_recent", 8))
    split = ctx_util.pick_split(pending, keep)
    if split <= 0:
        return {"ok": False, "detail": "可压缩的历史不足", "messages": len(pending)}

    to_sum = pending[:split]
    provider = get_provider(profile)
    before = ctx_util.usage(
        build_messages(conversation, all_msgs, config, profile), profile, config, tools
    )
    summary = await ctx_util.summarize(
        provider,
        to_sum,
        previous_summary=conversation.get("summary") or "",
        handle=handle,
    )
    if not summary:
        return {"ok": False, "detail": "摘要生成失败"}

    until_seq = int(to_sum[-1]["seq"])
    preview = {**conversation, "summary": summary, "summary_until": until_seq}
    new_usage = ctx_util.usage(
        build_messages(preview, all_msgs, config, profile), profile, config, tools
    )
    if not force and new_usage["tokens"] >= before["tokens"]:
        return {
            "ok": False,
            "detail": "压缩后长度没有减少，本次跳过",
            "usage": before,
        }

    store.set_summary(conv_id, summary, until_seq)
    conversation["summary"] = summary
    conversation["summary_until"] = until_seq
    conversation["compacted_at"] = time.time()

    return {
        "ok": True,
        "summary": summary,
        "covered_messages": len(to_sum),
        "summary_until": until_seq,
        "before": before,
        "usage": new_usage,
    }


async def stream_turn(
    *,
    conversation: dict[str, Any],
    profile: dict[str, Any],
    store: Store,
    handle: RunHandle,
) -> AsyncIterator[dict[str, Any]]:
    config = load_config()
    conv_id = conversation["id"]
    provider = get_provider(profile)

    tools_cfg = config["tools"]
    tools_enabled = bool(tools_cfg["enabled"])
    disabled = _disabled_tools(config)
    tools_schema = schema_list(disabled) if tools_enabled else None
    params = resolve_params(conversation, config, profile)

    active_skills = conversation.get("active_skills") or []
    space = store.get_space(conversation.get("space_id")) if conversation.get("space_id") else None
    scope = spaces.scope_view(space)
    try:
        spaces.ensure_conversation_dir(conversation)
    except Exception:  # noqa: BLE001
        pass
    workdir = str(spaces.workdir_for(conversation))
    scope["workdir"] = workdir
    turn_ctx = runtime.TurnContext(
        conversation_id=conv_id,
        workspace_root=workdir,
        skill_names=list(active_skills),
        model_id=profile.get("id"),
    )
    token = runtime.set_context(turn_ctx)

    try:
        messages = build_messages(conversation, store.list_messages(conv_id), config, profile)
        usage = ctx_util.usage(messages, profile, config, tools_schema)

        yield {
            "type": "start",
            "conversation_id": conv_id,
            "model": profile.get("label") or profile.get("id"),
            "provider": profile.get("provider"),
            "tools": bool(tools_schema),
            "thinking": params["thinking"],
            "thinking_level": params["thinking_level"],
            "skills": active_skills,
            "scope": scope,
            "usage": usage,
        }

        # ---------------- 上下文压缩 ----------------
        auto_compact = bool(config["defaults"].get("auto_compact", True))
        if tools_enabled:
            pass  # 工具不影响压缩判断
        if auto_compact and usage["should_compact"]:
            yield {"type": "compaction", "phase": "start", "usage": usage}
            try:
                result = await compact_conversation(
                    conversation=conversation,
                    profile=profile,
                    store=store,
                    handle=handle,
                    tools=tools_schema,
                )
            except Exception as exc:  # noqa: BLE001
                yield {"type": "compaction", "phase": "error", "message": str(exc)}
            else:
                if result.get("ok"):
                    messages = build_messages(
                        conversation, store.list_messages(conv_id), config, profile
                    )
                    usage = result["usage"]
                    yield {
                        "type": "compaction",
                        "phase": "done",
                        "summary": result["summary"],
                        "covered": result["covered_messages"],
                        "usage": usage,
                    }
                else:
                    yield {"type": "compaction", "phase": "skipped", "message": result.get("detail")}

        yield {"type": "context", "usage": usage}

        max_rounds = int(tools_cfg.get("max_rounds", 50))
        for round_idx in range(max_rounds):
            # 排队消息的插入点：下一轮模型调用前自然进上下文，不打断当前执行
            # （Claude Code / Cline 的做法；想打断可以用「停止」按钮）
            take = getattr(handle, "take_pending", None)
            if callable(take):
                for injected in take():
                    body = (injected or "").strip()
                    if not body:
                        continue
                    user_msg = store.add_message(conv_id, "user", content=body)
                    messages.append({"role": "user", "content": body})
                    yield {
                        "type": "user_injected",
                        "message_id": user_msg["id"],
                        "content": body,
                    }

            message = store.add_message(conv_id, "assistant", content="")
            message_id = message["id"]
            yield {"type": "message_start", "message_id": message_id, "round": round_idx}

            # 模型报「上下文超长」时：压缩较早历史，再把同一轮重跑一次。
            # 只重试一次，避免在摘要也没用的情况下死循环。
            overflow_retried = False
            compact_on_overflow = bool(config["defaults"].get("compact_on_overflow", True))

            while True:
                text = ""
                reasoning = ""
                calls = []
                calls_truncated = False
                usage_info = {}
                ttft = None
                started = time.perf_counter()
                last_flush = started
                failed = None

                try:
                    async for event in provider.stream(messages, tools_schema, params, handle):
                        kind = event.get("type")
                        if kind == "first_token":
                            ttft = float(event.get("ttft") or 0.0)
                            yield {"type": "meta", "message_id": message_id, "ttft": ttft}
                        elif kind == "delta":
                            chunk = event["text"]
                            if not chunk:
                                continue
                            if ttft is None:
                                ttft = time.perf_counter() - started
                            text += chunk
                            yield {"type": "delta", "message_id": message_id, "text": chunk}
                        elif kind == "reasoning":
                            reasoning += event["text"]
                            yield {
                                "type": "reasoning",
                                "message_id": message_id,
                                "text": event["text"],
                            }
                        elif kind == "thinking_done":
                            yield {
                                "type": "thinking_done",
                                "message_id": message_id,
                                "budget": event.get("budget"),
                                "tokens": event.get("tokens"),
                            }
                        elif kind == "tool_calls":
                            calls = event.get("calls") or []
                            calls_truncated = bool(event.get("truncated"))
                        elif kind == "notice":
                            # 后端降级提示（比如不吃图片），透传给界面提示用户
                            yield {
                                "type": "notice",
                                "message_id": message_id,
                                "message": event.get("message") or "",
                            }
                        elif kind == "usage":
                            usage_info.update({k: v for k, v in event.items() if k != "type"})

                        now = time.perf_counter()
                        if now - last_flush > 1.0:
                            store.update_message(message_id, content=text, reasoning=reasoning)
                            last_flush = now
                except ProviderError as exc:
                    failed = str(exc)
                except Exception as exc:  # noqa: BLE001
                    failed = f"{type(exc).__name__}: {exc}"

                if not (
                    failed
                    and not overflow_retried
                    and compact_on_overflow
                    and auto_compact
                    and is_context_overflow(failed)
                ):
                    break

                overflow_retried = True
                yield {
                    "type": "compaction",
                    "phase": "start",
                    "reason": "context_overflow",
                    "message": failed,
                }
                try:
                    result = await compact_conversation(
                        conversation=conversation,
                        profile=profile,
                        store=store,
                        handle=handle,
                        force=True,
                        tools=tools_schema,
                    )
                except Exception as exc:  # noqa: BLE001
                    yield {"type": "compaction", "phase": "error", "message": str(exc)}
                else:
                    if result.get("ok"):
                        messages = build_messages(
                            conversation, store.list_messages(conv_id), config, profile
                        )
                        yield {
                            "type": "compaction",
                            "phase": "done",
                            "reason": "context_overflow",
                            "summary": result["summary"],
                            "covered": result["covered_messages"],
                            "usage": result["usage"],
                        }
                        # 让前端把这一轮已经画出来的半成品清掉，重试用同一个消息 ID
                        yield {
                            "type": "message_start",
                            "message_id": message_id,
                            "round": round_idx,
                            "reset": True,
                        }
                        continue
                    yield {
                        "type": "compaction",
                        "phase": "skipped",
                        "message": result.get("detail"),
                    }
                break

            duration = time.perf_counter() - started
            if failed:
                store.update_message(
                    message_id,
                    content=text or f"⚠️ 生成失败：{failed}",
                    reasoning=reasoning,
                )
                yield {"type": "error", "message_id": message_id, "message": failed}
                return

            completion_tokens = usage_info.get("completion_tokens") or ctx_util.estimate_tokens(text)
            if not completion_tokens and calls:
                completion_tokens = ctx_util.estimate_tokens(
                    json.dumps([c.to_openai() for c in calls], ensure_ascii=False)
                )
            gen_duration = float(usage_info.get("duration") or duration)
            # tok/s 只算"读完之后吐字"的那段时间，否则首字延迟会被摊进去显得极慢
            decode_window = max(0.001, gen_duration - float(ttft or 0.0))
            # 输出侧三分项：本地后端有精确值，其它后端按文本估算
            reasoning_tokens = usage_info.get("reasoning_tokens")
            if reasoning_tokens is None:
                reasoning_tokens = ctx_util.estimate_tokens(reasoning)
            content_tokens = usage_info.get("content_tokens")
            if content_tokens is None:
                content_tokens = ctx_util.estimate_tokens(text)
            tool_call_tokens = usage_info.get("tool_call_tokens")
            if tool_call_tokens is None:
                tool_call_tokens = (
                    ctx_util.estimate_tokens(
                        json.dumps([c.to_openai() for c in calls], ensure_ascii=False)
                    )
                    if calls
                    else 0
                )
            stats = {
                "ttft": round(ttft, 3) if ttft else None,
                "duration": round(duration, 3),
                "gen_duration": round(gen_duration, 3),
                "tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "content_tokens": content_tokens,
                "tool_call_tokens": tool_call_tokens,
                "other_tokens": usage_info.get("other_tokens"),
                "tps": round(completion_tokens / decode_window, 1) if decode_window > 0 else None,
                "prompt_tokens": usage_info.get("prompt_tokens"),
                "reused_tokens": usage_info.get("reused_tokens"),
                "prefill_tokens": usage_info.get("prefill_tokens"),
                "cache_hit": usage_info.get("cache_hit"),
                "round": round_idx,
                "thinking": params["thinking"],
            }

            if handle.cancelled:
                if not text and not reasoning:
                    # 一个字都没吐出来就被停掉（比如还在等首字）：不留空消息
                    try:
                        store.delete_message(message_id)
                    except Exception:  # noqa: BLE001
                        pass
                    yield {"type": "done", "conversation_id": conv_id, "cancelled": True}
                    return
                store.update_message(
                    message_id,
                    content=text,
                    reasoning=reasoning,
                    stats={**stats, "cancelled": True},
                )
                yield {"type": "stats", "message_id": message_id, "stats": stats}
                yield {"type": "done", "conversation_id": conv_id, "cancelled": True}
                return

            if calls and tools_enabled:
                store.update_message(
                    message_id,
                    content=text,
                    reasoning=reasoning,
                    tool_calls=[c.to_openai() for c in calls],
                    stats=stats,
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": text,
                        "reasoning_content": reasoning,
                        "tool_calls": [c.to_openai() for c in calls],
                    }
                )
                yield {"type": "stats", "message_id": message_id, "stats": stats}

                if calls_truncated:
                    yield {
                        "type": "notice",
                        "message_id": message_id,
                        "message": (
                            f"这一轮输出达到了上限（max_tokens={params.get('max_tokens')}），"
                            "工具参数被截断，内容不完整。可以调大「设置 → 偏好 → 最大输出 tokens」，"
                            "或者让它分段写入。"
                        ),
                    }

                handled: set[str] = set()
                for call in calls:
                    if handle.cancelled:
                        _fill_cancelled_tool_calls(store, conv_id, calls, handled)
                        yield {"type": "done", "conversation_id": conv_id, "cancelled": True}
                        return
                    yield {
                        "type": "tool_call",
                        "message_id": message_id,
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    try:
                        if call.parse_error:
                            result = ToolResult(
                                ok=False,
                                content=(
                                    f"{call.parse_error}。\n"
                                    "请改成更小的参数重新调用：写长文件时用 write_file 分段 + append=true，"
                                    "每段控制在 1000 字以内；参数必须是合法 JSON，换行要转义成 \\n。"
                                ),
                                error="bad_arguments",
                            )
                        else:
                            result = await aexecute(
                                call.name, call.arguments, raw=call.raw_arguments or None
                            )
                    except asyncio.CancelledError:
                        # 工具执行到一半被停止：补一条占位结果，否则历史里会留下
                        # 「有 tool_calls 没有 tool 回复」的残缺消息，下一轮直接被拒
                        _fill_cancelled_tool_calls(store, conv_id, calls, handled)
                        raise
                    handled.add(call.id)
                    payload = result.content if result.ok else f"[error] {result.content}"
                    tool_msg = store.add_message(
                        conv_id,
                        "tool",
                        content=payload,
                        tool_call_id=call.id,
                        name=call.name,
                        stats={
                            "duration": round(result.duration, 3),
                            "ok": result.ok,
                            # 工具返回会整段回灌进下一轮提示词，它的 token 数必须记账
                            "tokens": ctx_util.estimate_tokens(payload),
                            "args_tokens": ctx_util.estimate_tokens(
                                call.arguments
                                if isinstance(call.arguments, str)
                                else json.dumps(call.arguments, ensure_ascii=False)
                            ),
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "content": payload,
                            "tool_call_id": call.id,
                            "name": call.name,
                        }
                    )
                    yield {
                        "type": "tool_result",
                        "message_id": tool_msg["id"],
                        "call_id": call.id,
                        "name": call.name,
                        "ok": result.ok,
                        "content": payload,
                        "duration": round(result.duration, 3),
                    }
                continue

            store.update_message(
                message_id,
                content=text,
                reasoning=reasoning,
                stats=stats,
            )
            yield {"type": "stats", "message_id": message_id, "stats": stats}
            final_usage = ctx_util.usage(
                build_messages(conversation, store.list_messages(conv_id), config, profile),
                profile,
                config,
                tools_schema,
            )
            yield {"type": "context", "usage": final_usage}
            yield {"type": "done", "conversation_id": conv_id, "cancelled": False}
            return

        yield {
            "type": "error",
            "message": (
                f"已达到最大工具调用轮数（{max_rounds}），已停止。"
                "可在「设置 → 偏好 → 工具调用最大轮数」调大后重试。"
            ),
        }
    finally:
        runtime.reset(token)
        store.touch_conversation(conv_id)
