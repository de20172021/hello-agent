"""上下文长度估算与压缩。

策略：
- 用启发式估算 token（有本地 tokenizer 时可以更准，但这里统一走估算，避免每次调模型）。
- 超过阈值时，把「较早的消息」交给同一个模型总结成结构化摘要，
  摘要存进会话表，只在提示词里替换掉那部分历史，原始消息仍然保留给用户查看。
- 后续再压缩时，会把已有摘要一起滚进去，不会丢失更早的信息。
"""

from __future__ import annotations

import json
from typing import Any

from .settings import load_config

SUMMARY_PROMPT = """你是一个对话压缩器。请把下面这段较早的对话压缩成一份结构化摘要。

必须保留：
1. 用户的真实目标与明确要求（含否定项，例如「不要用 xx」）；
2. 已经确认的事实、关键数据、结论；
3. 涉及的文件路径、命令、配置项、接口名等精确信息；
4. 已完成的事项与未完成的待办；
5. 用户表达过的偏好或纠正。

要求：
- 用简体中文，Markdown 小标题 + 短句，不超过 900 字；
- 不要编造没出现过的信息，不要评论，不要寒暄；
- 先给「一句话概览」，再分点列细节。"""


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return max(1, int(ascii_chars / 4 + other / 1.6))


# 一张图在不同后端里大约折算成 800~1300 token，取中间值记账
IMAGE_TOKENS = 1024


def content_tokens(content: Any) -> int:
    """消息内容可能是纯文本，也可能是 OpenAI 多模态数组，分开估算。"""
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                total += estimate_tokens(str(part or ""))
                continue
            kind = part.get("type")
            if kind == "text":
                total += estimate_tokens(str(part.get("text") or ""))
            elif kind in ("image_url", "image", "input_image"):
                total += IMAGE_TOKENS
            else:
                total += 32
        return total
    return estimate_tokens(str(content or ""))


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        total += 4
        total += content_tokens(msg.get("content"))
        total += estimate_tokens(str(msg.get("reasoning_content") or ""))
        calls = msg.get("tool_calls")
        if calls:
            total += estimate_tokens(json.dumps(calls, ensure_ascii=False))
    return total


def context_limit(profile: dict[str, Any], config: dict[str, Any] | None = None) -> int:
    cfg = config or load_config()
    try:
        limit = int(profile.get("context_window") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        limit = int(cfg["defaults"].get("context_window", 32768))
    return limit


def compact_trigger(config: dict[str, Any] | None = None, limit: int = 0) -> tuple[int, str]:
    """返回（触发压缩的 token 数，模式）。"""
    cfg = config or load_config()
    defaults = cfg["defaults"]
    mode = str(defaults.get("compact_mode") or "tokens").lower()
    if mode == "ratio":
        threshold = float(defaults.get("compact_threshold", 0.75))
        return int(limit * threshold), "ratio"
    budget = int(defaults.get("compact_tokens", 500_000) or 0)
    if budget <= 0:
        return limit, "tokens"
    return budget, "tokens"


def usage(
    messages: list[dict[str, Any]],
    profile: dict[str, Any],
    config: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cfg = config or load_config()
    tokens = estimate_messages(messages)
    if tools:
        # 工具 schema 会随每次请求发出，占的上下文不能忽略
        tokens += estimate_tokens(json.dumps(tools, ensure_ascii=False))
    limit = context_limit(profile, cfg)
    threshold = float(cfg["defaults"].get("compact_threshold", 0.75))
    trigger, mode = compact_trigger(cfg, limit)
    return {
        "tokens": tokens,
        "limit": limit,
        "percent": round(min(100.0, tokens / limit * 100), 1) if limit else 0.0,
        "threshold": threshold,
        "mode": mode,
        "trigger": trigger,
        "should_compact": tokens >= trigger,
        # 估算已超窗口：请求大概率会被模型以「超长」拒绝，交给 agent 的兜底压缩
        "over_limit": bool(limit) and tokens >= limit,
        "remaining": max(0, limit - tokens),
    }


def pick_split(messages: list[dict[str, Any]], keep_recent: int) -> int:
    """挑一个切割点：保留最后 keep_recent 条（尽量落在 user 消息上）。"""
    if not messages:
        return 0
    target = max(0, len(messages) - max(2, keep_recent))
    # 往前退到 user 消息边界，避免把一轮对话切成两半
    while target > 0 and messages[target].get("role") != "user":
        target -= 1
    # 跳过开头紧挨着的 tool 消息（它们属于被压缩的调用）
    while target < len(messages) and messages[target].get("role") == "tool":
        target += 1
    return target


def render_for_summary(
    messages: list[dict[str, Any]], previous_summary: str = "", budget: int = 24000
) -> str:
    chunks: list[str] = []
    used = 0
    for msg in messages:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role == "assistant":
            label = "助手"
            if msg.get("tool_calls"):
                names = [
                    (c.get("function") or {}).get("name", "?") for c in msg["tool_calls"]
                ]
                content = (content + f"\n（调用了工具：{', '.join(names)}）").strip()
        elif role == "tool":
            label = f"工具结果 {msg.get('name') or ''}".strip()
        else:
            label = "用户"
        if not content:
            continue
        if len(content) > 3000:
            content = content[:3000] + "…（截断）"
        line = f"【{label}】{content}"
        if used + len(line) > budget:
            chunks.append("…（更早内容因长度省略）")
            break
        chunks.append(line)
        used += len(line)
    body = "\n\n".join(chunks)
    if previous_summary:
        body = f"【已有摘要】\n{previous_summary}\n\n【较新的对话】\n{body}"
    return body


async def summarize(
    provider: Any,
    messages: list[dict[str, Any]],
    *,
    previous_summary: str = "",
    handle: Any = None,
) -> str:
    """调用同一个模型生成摘要（流式收集，不暴露给前端）。"""
    body = render_for_summary(messages, previous_summary)
    if not body.strip():
        return previous_summary
    payload = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": body},
    ]
    params = {"temperature": 0.2, "top_p": 0.9, "max_tokens": 900}
    parts: list[str] = []
    async for event in provider.stream(payload, None, params, handle):
        if event.get("type") == "delta":
            parts.append(event.get("text") or "")
    text = "".join(parts).strip()
    if len(text) > 3000:
        text = text[:3000] + "…"
    return text or previous_summary


def history_after(messages: list[dict[str, Any]], until_seq: int) -> list[dict[str, Any]]:
    """取出摘要覆盖范围之后的消息。"""
    if not until_seq:
        return messages
    tail = [m for m in messages if int(m.get("seq") or 0) > int(until_seq)]
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)
    return tail
