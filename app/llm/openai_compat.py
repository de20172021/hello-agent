"""OpenAI 兼容后端：LM Studio / Ollama / vLLM / DeepSeek 等。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

import httpx

from .. import multimodal
from ..tools.jsonargs import loads_lenient
from .base import BaseProvider, ProviderError, RunHandle, ToolCall

# 后端明确拒绝图片时的措辞（能拿到 HTTP 状态码时优先看状态码）
_VISION_REJECT_STATUS = ("http 400", "http 415", "http 422", "http 500")
_VISION_REJECT_WORDS = ("image", "vision", "multimodal", "image_url", "图片", "content type")


def _looks_like_vision_reject(message: str) -> bool:
    low = (message or "").lower()
    if not any(token in low for token in _VISION_REJECT_STATUS):
        return False
    return any(token in low for token in _VISION_REJECT_WORDS)


def root_url(base_url: str) -> str:
    """去掉末尾 /v1 或 /chat/completions 的服务根地址。"""
    url = str(base_url or "").strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url


def _trust_env(base_url: str) -> bool:
    host = str(base_url or "").lower()
    return not any(
        token in host for token in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1")
    )


async def fetch_models(
    base_url: str, api_key: str = "", timeout: float = 10.0
) -> dict[str, Any]:
    """拉取 OpenAI 兼容端点的可用模型列表（GET {root}/v1/models）。"""
    base_url = str(base_url or "").strip()
    if not base_url:
        return {"ok": False, "models": [], "detail": "请先填写服务地址"}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(
            timeout=timeout, trust_env=_trust_env(base_url)
        ) as client:
            resp = await client.get(f"{root_url(base_url)}/v1/models", headers=headers)
            if resp.status_code >= 400:
                body = resp.text[:200].replace("\n", " ")
                return {"ok": False, "models": [], "detail": f"HTTP {resp.status_code}：{body}"}
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "models": [], "detail": f"连接失败：{type(exc).__name__}"}

    items: list[dict[str, str]] = []
    for entry in data.get("data") or []:
        if isinstance(entry, dict) and entry.get("id"):
            items.append({"id": str(entry["id"])})
        elif isinstance(entry, str):
            items.append({"id": entry})
    items.sort(key=lambda x: x["id"])
    if not items:
        return {"ok": True, "models": [], "detail": "服务在线，但没有返回模型列表"}
    return {"ok": True, "models": items, "detail": f"{len(items)} 个模型可用"}


async def _iter_lines(lines, handle, poll: float = 0.25):
    """逐行读 SSE，同时每 poll 秒检查一次取消。

    为什么不用 `async for line in resp.aiter_lines()`：网关在「思考」阶段可能
    几十秒一个字节都不发，`async for` 会一直阻塞在下一个 chunk 上，
    用户点停止要等第一个 token 到达才生效。这里把读取放到独立 task 上轮询，
    一旦 cancelled 就把未完成的读取取消掉、关掉连接（顺带让服务端停止计费）。
    """
    it = lines.__aiter__()
    pending: asyncio.Future | None = None
    while True:
        if pending is None:
            pending = asyncio.ensure_future(it.__anext__())
        done, _ = await asyncio.wait({pending}, timeout=poll)
        if not done:
            if handle is not None and handle.cancelled:
                pending.cancel()
                return
            continue
        try:
            line = pending.result()
        except (StopAsyncIteration, asyncio.CancelledError):
            return
        pending = None
        yield line


class OpenAICompatProvider(BaseProvider):
    kind = "openai_compat"

    def __init__(self, profile: dict[str, Any]):
        super().__init__(profile)
        self.base_url = str(profile.get("base_url") or "").rstrip("/")
        self.model = str(profile.get("model") or "")
        self.api_key = str(profile.get("api_key") or "")

    # ---------------- 基础 ----------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @property
    def _root(self) -> str:
        """去掉末尾 /v1 的服务根地址，用于探测。"""
        url = self.base_url
        for suffix in ("/v1/chat/completions", "/chat/completions", "/v1"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    @property
    def _trust_env(self) -> bool:
        """本地服务绕开系统代理，否则 127.0.0.1 的请求会被代理劫持。"""
        host = self.base_url.lower()
        return not any(
            token in host for token in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1")
        )

    async def status(self) -> dict[str, Any]:
        base = {
            "profile_id": self.profile.get("id"),
            "provider": self.kind,
            "base_url": self.base_url,
            "model": self.model,
        }
        if not self.base_url:
            return {**base, "state": "error", "detail": "未配置 base_url"}
        result = await fetch_models(self.base_url, self.api_key, timeout=4.0)
        if not result["ok"]:
            offline = result["detail"].startswith("连接失败")
            return {**base, "state": "offline" if offline else "error", "detail": result["detail"]}
        models = [m["id"] for m in result["models"]]
        return {**base, "state": "ready", "detail": result["detail"], "models": models[:80]}

    # ---------------- 推理 ----------------

    def _thinking_style(self) -> str:
        """不同服务商控制思考的字段不一样，auto 时按模型名猜。"""
        style = str(self.profile.get("thinking_param") or "auto").lower()
        if style != "auto":
            return style
        marker = f"{self.model} {self.base_url}".lower()
        if "qwen" in marker:
            return "chat_template_kwargs"
        if any(k in marker for k in ("deepseek-reasoner", "o1", "o3", "o4", "gpt-5", "reasoning")):
            return "reasoning_effort"
        return "none"

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": float(params.get("temperature", 0.6)),
            "top_p": float(params.get("top_p", 0.95)),
            "max_tokens": int(params.get("max_tokens", 2048)),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        thinking = bool(params.get("thinking", False))
        level = str(params.get("thinking_level") or "medium")
        style = self._thinking_style()
        if style == "chat_template_kwargs":
            payload["chat_template_kwargs"] = {"enable_thinking": thinking}
        elif style == "enable_thinking":
            payload["enable_thinking"] = thinking
        elif style == "reasoning_effort":
            payload["reasoning_effort"] = level if level in ("low", "medium", "high") else "medium"
        return payload

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        handle: RunHandle | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """发请求；后端不吃图片时剥掉图片重试一次，避免整轮直接失败。"""
        params = params or {}
        current = list(messages)
        for attempt in range(2):
            try:
                async for event in self._stream_once(current, tools, params, handle):
                    yield event
                return
            except ProviderError as exc:
                if attempt == 0 and multimodal.has_image(current) and _looks_like_vision_reject(str(exc)):
                    current = multimodal.strip_images(current)
                    yield {
                        "type": "notice",
                        "message": "当前模型不接受图片输入，已把附件改写成文字说明重试（可在模型设置里指定是否支持看图）",
                    }
                    continue
                raise

    async def _stream_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        params: dict[str, Any],
        handle: RunHandle | None,
    ) -> AsyncIterator[dict[str, Any]]:
        url = f"{self.base_url}/chat/completions"
        started = time.perf_counter()
        first_at: float | None = None
        buffers: dict[int, dict[str, Any]] = {}
        finish_reason = ""

        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout, trust_env=self._trust_env) as client:
            try:
                async with client.stream(
                    "POST", url, json=self._payload(messages, tools, params), headers=self._headers()
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "ignore")[:500]
                        raise ProviderError(f"HTTP {resp.status_code}: {body}")
                    async for line in _iter_lines(resp.aiter_lines(), handle):
                        if handle is not None and handle.cancelled:
                            break
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            data = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue

                        choice = (data.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])

                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            yield {"type": "reasoning", "text": reasoning}

                        content = delta.get("content")
                        if content:
                            if first_at is None:
                                first_at = time.perf_counter()
                                yield {"type": "first_token", "ttft": first_at - started}
                            yield {"type": "delta", "text": content}

                        for tc in delta.get("tool_calls") or []:
                            idx = int(tc.get("index", 0))
                            slot = buffers.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]

                        usage = data.get("usage")
                        if usage:
                            yield {
                                "type": "usage",
                                "prompt_tokens": usage.get("prompt_tokens"),
                                "completion_tokens": usage.get("completion_tokens"),
                            }
            except httpx.HTTPError as exc:
                raise ProviderError(f"请求失败：{exc}") from exc

        calls: list[ToolCall] = []
        truncated = finish_reason == "length"
        for idx, slot in sorted(buffers.items()):
            if not slot["name"]:
                continue
            raw_args = slot["arguments"] or "{}"
            parsed, info = loads_lenient(raw_args)
            parse_error = ""
            if info.get("truncated"):
                parse_error = (
                    "参数在传输中被截断（输出达到 max_tokens 上限），内容不完整，不能执行"
                )
            elif not info.get("ok") or not isinstance(parsed, dict):
                parse_error = f"参数不是合法 JSON：{info.get('detail') or '无法解析'}"
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{idx}",
                    name=slot["name"],
                    arguments=parsed if isinstance(parsed, dict) else {},
                    raw_arguments=raw_args,
                    parse_error=parse_error,
                )
            )
        if calls:
            yield {"type": "tool_calls", "calls": calls, "truncated": truncated}
        yield {"type": "usage", "duration": time.perf_counter() - started}
