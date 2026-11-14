"""单轮对话的运行时上下文。

工具执行时拿不到 FastAPI 的请求对象，所以用 contextvars 传递
「当前会话 / 工作区根目录 / 本轮技能」等信息。
asyncio.to_thread 会自动复制 context，线程里也能读到。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnContext:
    conversation_id: str = ""
    workspace_root: str | None = None
    skill_names: list[str] = field(default_factory=list)
    model_id: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "workspace_root": self.workspace_root,
            "skill_names": list(self.skill_names),
            "model_id": self.model_id,
        }


_CTX: ContextVar[TurnContext | None] = ContextVar("helloagent_turn", default=None)


def current() -> TurnContext:
    return _CTX.get() or TurnContext()


def set_context(ctx: TurnContext) -> Token:
    return _CTX.set(ctx)


def reset(token: Token) -> None:
    try:
        _CTX.reset(token)
    except ValueError:
        pass
