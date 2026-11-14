"""流式文本解析小工具：thinking 拆分、工具调用抽取。"""

from __future__ import annotations

import json
import re
from typing import Iterable

from .base import ToolCall
from ..tools.jsonargs import loads_lenient

TOOL_OPEN = "<tool_call>"
TOOL_CLOSE = "</tool_call>"
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

_TOOL_BLOCK_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)


class ThinkSplitter:
    """把模型输出拆成 reasoning / content 两路，支持跨 chunk 的标签。"""

    def __init__(self) -> None:
        self.mode = "detect"  # detect -> reasoning / content
        self.buf = ""
        self.thought_any = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self.buf += chunk
        while self.buf:
            if self.mode == "detect":
                stripped = self.buf.lstrip()
                if stripped.startswith(THINK_OPEN):
                    self.buf = stripped[len(THINK_OPEN) :]
                    self.mode = "reasoning"
                    continue
                # 还在标签前缀阶段，先攒着
                if len(stripped) < len(THINK_OPEN) and THINK_OPEN.startswith(stripped):
                    return out
                self.mode = "content"
                continue

            if self.mode == "reasoning":
                idx = self.buf.find(THINK_CLOSE)
                if idx >= 0:
                    if idx:
                        out.append(("reasoning", self.buf[:idx]))
                        self.thought_any = True
                    self.buf = self.buf[idx + len(THINK_CLOSE) :]
                    self.mode = "content"
                    continue
                keep = len(THINK_CLOSE) - 1
                if len(self.buf) > keep:
                    part = self.buf[:-keep]
                    out.append(("reasoning", part))
                    self.thought_any = True
                    self.buf = self.buf[-keep:]
                return out

            # content
            out.append(("delta", self.buf))
            self.buf = ""
        return out

    def flush(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.buf:
            kind = "reasoning" if self.mode == "reasoning" else "delta"
            out.append((kind, self.buf))
            self.buf = ""
        return out


class ToolCallGuard:
    """从流式文本里剥离 <tool_call> 块，避免工具调用 XML 闪现在气泡里。"""

    def __init__(self) -> None:
        self.buf = ""
        self.in_call = False
        self.current = ""
        self.blocks: list[str] = []

    def feed(self, text: str) -> list[str]:
        visible: list[str] = []
        self.buf += text
        while self.buf:
            if not self.in_call:
                idx = self.buf.find(TOOL_OPEN)
                if idx >= 0:
                    if idx:
                        visible.append(self.buf[:idx])
                    self.buf = self.buf[idx + len(TOOL_OPEN) :]
                    self.in_call = True
                    self.current = ""
                    continue
                hold = self._partial_suffix(self.buf, TOOL_OPEN)
                if hold:
                    visible.append(self.buf[:-hold])
                    self.buf = self.buf[-hold:]
                else:
                    visible.append(self.buf)
                    self.buf = ""
                break

            idx = self.buf.find(TOOL_CLOSE)
            if idx >= 0:
                self.current += self.buf[:idx]
                self.blocks.append(self.current)
                self.current = ""
                self.in_call = False
                self.buf = self.buf[idx + len(TOOL_CLOSE) :]
                continue
            self.current += self.buf
            self.buf = ""
        return [v for v in visible if v]

    def flush(self) -> list[str]:
        out: list[str] = []
        if self.in_call:
            # 没闭合的工具块，按内容吞掉
            self.blocks.append(self.current)
            self.current = ""
            self.in_call = False
        if self.buf:
            out.append(self.buf)
            self.buf = ""
        return out

    def parsed_calls(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for idx, block in enumerate(self.blocks):
            call = parse_tool_block(block, f"call_{idx}")
            if call:
                calls.append(call)
        return calls

    @staticmethod
    def _partial_suffix(buf: str, tag: str) -> int:
        """返回 buf 末尾属于 tag 前缀的长度，用于跨 chunk 缓冲。"""
        limit = min(len(tag) - 1, len(buf))
        for size in range(limit, 0, -1):
            if buf.endswith(tag[:size]):
                return size
        return 0


class TagStripper:
    """流式剔除指定标签（例如思考块被强制截断后模型补出的 <｜end▁of▁thinking｜>）。"""

    def __init__(self, tags: tuple[str, ...] = ("<｜end▁of▁thinking｜>", "<think>")) -> None:
        self.tags = tags
        self.buf = ""
        self.max_len = max(len(t) for t in tags) if tags else 0

    def feed(self, text: str) -> str:
        self.buf += text
        for tag in self.tags:
            self.buf = self.buf.replace(tag, "")
        hold = 0
        for size in range(min(self.max_len - 1, len(self.buf)), 0, -1):
            suffix = self.buf[-size:]
            if any(tag.startswith(suffix) for tag in self.tags):
                hold = size
                break
        if hold:
            visible, self.buf = self.buf[:-hold], self.buf[-hold:]
        else:
            visible, self.buf = self.buf, ""
        return visible

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return out


def parse_tool_block(block: str, fallback_id: str = "call") -> ToolCall | None:
    raw = block.strip()
    if not raw:
        return None
    data, info = loads_lenient(raw)
    if not isinstance(data, dict):
        # 名字能从残缺文本里抠出来时，至少让调用方知道模型想干什么
        guess = re.search(r'"name"\s*:\s*"([^"]+)"', raw)
        if guess:
            return ToolCall(
                id=fallback_id,
                name=guess.group(1),
                arguments={},
                raw_arguments=raw,
                parse_error=f"参数不是合法 JSON：{info.get('detail') or '无法解析'}",
            )
        return None

    name = data.get("name") or data.get("function") or ""
    if isinstance(data.get("function"), dict):
        name = data["function"].get("name") or name
        data.setdefault("arguments", data["function"].get("arguments", {}))
    args = data.get("arguments", data.get("parameters", {}))
    parse_error = "参数在传输中被截断（输出达到上限），内容不完整，不能执行" if info.get("truncated") else ""
    if isinstance(args, str):
        parsed, args_info = loads_lenient(args)
        if not parse_error and args_info.get("truncated"):
            parse_error = "参数被截断（输出达到上限），内容不完整，不能执行"
        elif not args_info.get("ok"):
            parse_error = f"参数不是合法 JSON：{args_info.get('detail') or '无法解析'}"
        args = parsed if isinstance(parsed, dict) else {}
    if not isinstance(args, dict):
        args = {}
    if not name:
        return None
    return ToolCall(
        id=str(data.get("id") or fallback_id),
        name=str(name),
        arguments=args,
        raw_arguments=json.dumps(args, ensure_ascii=False),
        parse_error=parse_error,
    )


def extract_tool_calls(text: str) -> tuple[str, list[ToolCall]]:
    """从完整文本中抽出工具调用，返回 (清理后的文本, 调用列表)。"""
    calls: list[ToolCall] = []
    for idx, match in enumerate(_TOOL_BLOCK_RE.findall(text)):
        call = parse_tool_block(match, f"call_{idx}")
        if call:
            calls.append(call)
    cleaned = _TOOL_BLOCK_RE.sub("", text).strip()
    return cleaned, calls


def strip_tool_blocks(text: str) -> str:
    return _TOOL_BLOCK_RE.sub("", text).strip()


def iter_visible(chunks: Iterable[str]) -> list[str]:
    guard = ToolCallGuard()
    out: list[str] = []
    for chunk in chunks:
        out.extend(guard.feed(chunk))
    out.extend(guard.flush())
    return out
