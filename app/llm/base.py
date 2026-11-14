"""推理后端抽象层。"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


class ProviderError(RuntimeError):
    """后端调用失败（会被转成 SSE error 事件）。"""


# 模型名里出现这些片段时，按「能看图」处理（auto 模式只用来做提示，不做硬判断）
_VISION_HINTS = (
    "vl", "vision", "omni", "multimodal", "llava", "internvl", "minicpm-v",
    "pixtral", "moondream", "glm-4v", "gpt-4o", "gpt-4.1", "gpt-5", "claude",
    "gemini", "step-1v", "qvq",
)


def vision_hint(profile: dict[str, Any]) -> bool:
    marker = f"{profile.get('model') or ''} {profile.get('label') or ''}".lower()
    return any(token in marker for token in _VISION_HINTS)


def vision_capable(profile: dict[str, Any]) -> bool:
    """是否把图片按多模态消息发出去。

    三档（模型档案里的 `vision`）：
    - on  / off：用户明确指定；
    - auto（默认）：OpenAI 兼容端点先按「能看图」发送，后端若不接受，
      provider 会自动剥掉图片改成文字说明再重试一次；本地 transformers
      走的是纯文本管线，直接降级成文字说明。
    """
    flag = str((profile or {}).get("vision") or "auto").lower()
    if flag in ("on", "true", "yes", "1"):
        return True
    if flag in ("off", "false", "no", "0"):
        return False
    return (profile or {}).get("provider") == "openai_compat"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    # 参数没能解析成 JSON 时的说明；非空表示这次调用不该被执行
    parse_error: str = ""

    def to_openai(self) -> dict[str, Any]:
        args = self.raw_arguments or json.dumps(self.arguments, ensure_ascii=False)
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": args},
        }


class RunHandle:
    """一次生成请求的取消句柄，协程与工作线程共享。"""

    def __init__(self) -> None:
        self.thread_stop = threading.Event()
        self.cancelled = False
        # 排队消息的插入通道：send_now 把文本放进来，agent 主循环在
        # 下一轮模型调用前取走并写进上下文，不打断当前执行
        self._pending: list[str] = []

    def cancel(self) -> None:
        self.cancelled = True
        self.thread_stop.set()

    @property
    def stopping(self) -> bool:
        return self.cancelled or self.thread_stop.is_set()

    def inject(self, text: str) -> None:
        if text and text.strip():
            self._pending.append(text)

    def take_pending(self) -> list[str]:
        msgs, self._pending = self._pending, []
        return msgs


class BaseProvider:
    kind = "base"

    def __init__(self, profile: dict[str, Any]):
        self.profile = profile or {}

    # --- 生命周期 ---
    async def status(self) -> dict[str, Any]:
        return {"state": "ready", "detail": "无需加载"}

    async def load(self) -> dict[str, Any]:
        return await self.status()

    async def unload(self) -> None:
        return None

    # --- 推理 ---
    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        handle: RunHandle | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """产出事件：delta / reasoning / tool_calls / usage。"""
        raise NotImplementedError

    # --- 工具 ---
    @staticmethod
    def to_hf_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把内部消息格式转成 transformers chat template 需要的结构。"""
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            item: dict[str, Any] = {"role": role, "content": msg.get("content") or ""}
            if role == "assistant" and msg.get("tool_calls"):
                item["tool_calls"] = msg["tool_calls"]
            if role == "tool":
                item["content"] = msg.get("content") or ""
            out.append(item)
        return out
