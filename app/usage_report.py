"""会话 Token 用量报告。

数据来源分两层，面板上会分别标注：

- **实测**：模型后端返回的 `prompt_tokens` / `reused_tokens` / `completion_tokens`
  以及输出三分项（思考 / 正文 / 工具调用），存在每条 assistant 消息的 `stats` 里；
- **估算**：输入提示词的构成拆分（固定开销 / 历史 / 本轮问题 / 工具返回）按字符
  估算后，再按实测总数等比例校准，保证各项之和等于实测输入 token。

「轮」= 一次用户提问；一轮里可能有多次模型生成和多次工具调用（工具轮）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import agent
from .context import estimate_tokens, usage as ctx_usage
from .settings import load_config
from .tools import schema_list


def _fixed_tokens(conversation: dict[str, Any], config: dict[str, Any]) -> dict[str, int]:
    """每轮都要重发的固定内容：系统提示 + 工具 schema + 易变尾块。"""
    active = conversation.get("active_skills") or []
    system = agent.build_system_prompt(conversation, config, active)
    system_tokens = estimate_tokens(system)
    tools_tokens = 0
    if config["tools"]["enabled"]:
        try:
            tools = schema_list(agent._disabled_tools(config))  # noqa: SLF001
            tools_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            tools_tokens = 0
    volatile_tokens = estimate_tokens(agent.build_volatile_prompt())
    return {
        "system": system_tokens,
        "tools_schema": tools_tokens,
        "volatile": volatile_tokens,
        "total": system_tokens + tools_tokens + volatile_tokens,
    }


def _output_of(message: dict[str, Any]) -> dict[str, int]:
    stats = message.get("stats") or {}
    calls = message.get("tool_calls") or []
    reasoning = int(stats.get("reasoning_tokens") or 0)
    if not reasoning:
        reasoning = estimate_tokens(message.get("reasoning") or "")
    content = int(stats.get("content_tokens") or 0)
    if not content:
        content = estimate_tokens(message.get("content") or "")
    tool_calls = int(stats.get("tool_call_tokens") or 0)
    if not tool_calls and calls:
        tool_calls = estimate_tokens(json.dumps(calls, ensure_ascii=False))
    other = int(stats.get("other_tokens") or 0)
    total = max(int(stats.get("tokens") or 0), reasoning + content + tool_calls + other)
    return {
        "reasoning": reasoning,
        "content": content,
        "tool_calls": tool_calls,
        "other": other,
        "total": total,
    }


def _tool_result_tokens(message: dict[str, Any]) -> int:
    stats = message.get("stats") or {}
    value = int(stats.get("tokens") or 0)
    if value:
        return value
    return estimate_tokens(message.get("content") or "")


def _split_input(measured: int, fixed_est: int, parts: dict[str, int]) -> dict[str, int]:
    """把实测输入拆成固定开销 + 历史 / 问题 / 工具返回。

    固定开销先按校准后的值占位（不超过实测总数），余额按估算比例分给其余三项，
    保证四项之和 == 实测输入。
    """
    out = {"fixed": 0, "history": 0, "question": 0, "tool_results": 0}
    if measured <= 0:
        return out
    fixed = max(0, min(measured, int(round(fixed_est))))
    out["fixed"] = fixed
    budget = measured - fixed
    total = sum(max(0, v) for v in parts.values())
    if budget <= 0:
        return out
    if total <= 0:
        out["history"] = budget
        return out
    scaled = {k: int(round(max(0, v) * budget / total)) for k, v in parts.items()}
    diff = budget - sum(scaled.values())
    if diff:
        key = max(scaled, key=lambda k: scaled[k])
        scaled[key] = max(0, scaled[key] + diff)
    out.update(scaled)
    return out


def _fixed_factor(steps: list[dict[str, Any]], fixed_est: int) -> float:
    """反推固定开销的校准系数。

    挑「可变部分最小」的那一次生成来算：那一步的输入几乎全是系统提示 + 工具 schema，
    误差最小。用中位数反而会被长历史轮次的估算误差带偏。
    """
    if fixed_est <= 0:
        return 1.0
    candidates = [
        step
        for step in steps
        if int(step.get("_measured") or 0) > 0
    ]
    if not candidates:
        return 1.0
    best = min(candidates, key=lambda s: sum(s.get("_var_parts", {}).values()))
    ratio = (int(best["_measured"]) - sum(best.get("_var_parts", {}).values())) / fixed_est
    return max(0.6, min(2.0, ratio))


def build_report(
    conversation: dict[str, Any],
    messages: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = config or load_config()
    fixed = _fixed_tokens(conversation, cfg)
    summary_tokens = estimate_tokens(conversation.get("summary") or "")
    covered_until = int(conversation.get("summary_until") or 0)

    # 前面已经发出去的对话内容（估算），用于拆分「历史」与「本轮新增」
    history_est = fixed["total"] + summary_tokens
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def ensure_turn(created_at: float | None = None) -> dict[str, Any]:
        nonlocal current
        if current is None:
            current = {
                "index": len(turns) + 1,
                "created_at": created_at,
                "question": {"tokens": 0, "chars": 0, "preview": ""},
                "steps": [],
                "tool_calls": [],
            }
            turns.append(current)
        return current

    for msg in messages:
        role = msg.get("role")
        stats = msg.get("stats") or {}
        content = msg.get("content") or ""
        # 被摘要覆盖的消息已经不在提示词里，不该再计入「历史」
        in_prompt = not covered_until or int(msg.get("seq") or 0) > covered_until

        if role == "user":
            current = None  # 每条用户消息开始新的一轮
            turn = ensure_turn(msg.get("created_at"))
            turn["question"] = {
                "tokens": estimate_tokens(content),
                "chars": len(content),
                "preview": content.strip().replace("\n", " ")[:90],
            }
            if in_prompt:
                history_est += estimate_tokens(content)
            continue

        if role == "assistant":
            turn = ensure_turn(msg.get("created_at"))
            prompt = int(stats.get("prompt_tokens") or 0)
            tool_tokens_in_turn = sum(
                s.get("result_tokens", 0) for s in turn["steps"] if s["type"] == "tool"
            )
            var_parts = {
                # history_est 已包含本轮问题与工具返回，这里减掉避免重复计算
                "history": max(
                    0,
                    history_est
                    - fixed["total"]
                    - summary_tokens
                    - turn["question"]["tokens"]
                    - tool_tokens_in_turn,
                ),
                "question": turn["question"]["tokens"],
                "tool_results": tool_tokens_in_turn,
            }
            measured = prompt or (fixed["total"] + summary_tokens + sum(var_parts.values()))
            turn["steps"].append(
                {
                    "type": "model",
                    "message_id": msg.get("id"),
                    "created_at": msg.get("created_at"),
                    "thinking": bool(stats.get("thinking")),
                    "ttft": stats.get("ttft"),
                    "duration": stats.get("duration"),
                    "tps": stats.get("tps"),
                    "input": {
                        "prompt": measured,
                        "reused": int(stats.get("reused_tokens") or 0),
                        "prefill": int(
                            stats.get("prefill_tokens")
                            if stats.get("prefill_tokens") is not None
                            else measured
                        ),
                        "estimated": not prompt,
                        "split": {"fixed": 0, "history": 0, "question": 0, "tool_results": 0},
                    },
                    "output": _output_of(msg),
                    "tool_calls": [
                        (c.get("function") or {}).get("name") or "?"
                        for c in (msg.get("tool_calls") or [])
                    ],
                    "_measured": measured,
                    "_var_parts": var_parts,
                }
            )
            if in_prompt:
                # 历史里只回传正文与工具调用：Qwen 模板不会把上一轮的思考内容再发一遍
                history_est += estimate_tokens(content) + (
                    estimate_tokens(json.dumps(msg.get("tool_calls"), ensure_ascii=False))
                    if msg.get("tool_calls")
                    else 0
                )
            continue

        if role == "tool":
            turn = ensure_turn(msg.get("created_at"))
            entry = {
                "type": "tool",
                "message_id": msg.get("id"),
                "name": msg.get("name") or "?",
                "ok": bool(stats.get("ok", True)),
                "duration": stats.get("duration"),
                "result_tokens": _tool_result_tokens(msg),
                "args_tokens": int(stats.get("args_tokens") or 0),
                "preview": (msg.get("content") or "").strip().replace("\n", " ")[:90],
            }
            turn["steps"].append(entry)
            turn["tool_calls"].append(
                {
                    "name": entry["name"],
                    "ok": entry["ok"],
                    "duration": entry["duration"],
                    "result_tokens": entry["result_tokens"],
                }
            )
            if in_prompt:
                history_est += entry["result_tokens"]
            continue

    # ---------------- 逐轮汇总 ----------------
    all_models = [s for t in turns for s in t["steps"] if s["type"] == "model"]
    factor = _fixed_factor(all_models, fixed["total"] + summary_tokens)
    for step in all_models:
        step["input"]["split"] = _split_input(
            step["input"]["prompt"],
            (fixed["total"] + summary_tokens) * factor,
            step["_var_parts"],
        )
        step.pop("_measured", None)
        step.pop("_var_parts", None)

    for turn in turns:
        models = [s for s in turn["steps"] if s["type"] == "model"]
        tools = [s for s in turn["steps"] if s["type"] == "tool"]
        turn["input"] = {
            "prompt": sum(s["input"]["prompt"] for s in models),
            "reused": sum(s["input"]["reused"] for s in models),
            "prefill": sum(s["input"]["prefill"] for s in models),
            "split": {
                key: sum(s["input"]["split"][key] for s in models)
                for key in ("fixed", "history", "question", "tool_results")
            },
        }
        turn["output"] = {
            key: sum(s["output"][key] for s in models)
            for key in ("reasoning", "content", "tool_calls", "other", "total")
        }
        turn["tool_summary"] = {
            "calls": len(tools),
            "failures": len([s for s in tools if not s["ok"]]),
            "result_tokens": sum(s["result_tokens"] for s in tools),
            "duration": round(sum(float(s["duration"] or 0) for s in tools), 3),
        }
        turn["duration"] = round(sum(float(s["duration"] or 0) for s in models), 3)
        turn["thinking"] = any(s["thinking"] for s in models)

    # ---------------- 会话汇总 ----------------
    def total(path: tuple[str, str]) -> int:
        return sum(turn[path[0]][path[1]] for turn in turns)

    by_tool: dict[str, dict[str, Any]] = {}
    for turn in turns:
        for call in turn["tool_calls"]:
            item = by_tool.setdefault(
                call["name"], {"name": call["name"], "calls": 0, "failures": 0, "tokens": 0}
            )
            item["calls"] += 1
            item["failures"] += 0 if call["ok"] else 1
            item["tokens"] += int(call["result_tokens"] or 0)

    prompt_total = total(("input", "prompt"))
    reused_total = total(("input", "reused"))
    profile = _profile_of(conversation, cfg)
    context = ctx_usage(
        agent.build_messages(conversation, messages, cfg, profile),
        profile,
        cfg,
        schema_list(agent._disabled_tools(cfg))  # noqa: SLF001
        if cfg["tools"]["enabled"]
        else None,
    )

    totals = {
        "turns": len(turns),
        "models": sum(len([s for s in t["steps"] if s["type"] == "model"]) for t in turns),
        "input": {
            "prompt": prompt_total,
            "reused": reused_total,
            "prefill": total(("input", "prefill")),
            "cache_rate": round(reused_total / prompt_total * 100, 1) if prompt_total else 0.0,
            "split": {
                key: sum(t["input"]["split"][key] for t in turns)
                for key in ("fixed", "history", "question", "tool_results")
            },
        },
        "output": {
            key: total(("output", key))
            for key in ("reasoning", "content", "tool_calls", "other", "total")
        },
        "tool": {
            "calls": sum(t["tool_summary"]["calls"] for t in turns),
            "failures": sum(t["tool_summary"]["failures"] for t in turns),
            "result_tokens": sum(t["tool_summary"]["result_tokens"] for t in turns),
            "duration": round(sum(t["tool_summary"]["duration"] for t in turns), 3),
            "by_tool": sorted(by_tool.values(), key=lambda x: -x["calls"]),
        },
        "duration": round(sum(t["duration"] for t in turns), 3),
        "question_tokens": sum(t["question"]["tokens"] for t in turns),
        "context": context,
    }

    return {
        "conversation_id": conversation.get("id"),
        "model": profile.get("label") or profile.get("id"),
        "provider": profile.get("provider"),
        "fixed": fixed,
        "summary_tokens": summary_tokens,
        "turns": turns,
        "totals": totals,
        "generated_at": time.time(),
    }


def _profile_of(conversation: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    target = conversation.get("model_id") or config.get("active_model_id")
    return next(
        (m for m in config["models"] if m["id"] == target),
        config["models"][0],
    )
