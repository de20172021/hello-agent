"""MCP 客户端：连接本地/远程 MCP 服务，把它们的工具接进对话。

配置文件固定在 data/mcp.json，结构：
{
  "servers": {
    "名字": {
      "transport": "stdio" | "http" | "sse",
      "command": "npx", "args": [...], "env": {...}, "cwd": "...",   // stdio
      "url": "http://...", "headers": {...}, "token": "...",          // http / sse
      "enabled": true,
      "auto_start": true,
      "description": "可选说明"
    }
  }
}

鉴权：
- http / sse 用 headers（可写 Authorization / X-API-Key 等），或用 token 字段自动补
  `Authorization: Bearer <token>`。
- stdio 用 env 传 API Key（会与当前进程环境变量合并）。
- 所有字符串值都支持 ${ENV_VAR} 占位符，从系统环境变量取值，避免把密钥写进配置文件。

连接是进程级复用的：启动时连一次，之后所有会话共享；
每个 MCP 工具会以 mcp__<server>__<tool> 的名字出现在工具列表里。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .settings import MCP_CONFIG_PATH, ensure_dirs, load_config

SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_\-]")
ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
CONNECT_TIMEOUT = 45.0
CALL_TIMEOUT = 120.0
MAX_TOOL_RESULT = 20000

_SECRET_KEYS = ("authorization", "token", "api_key", "apikey", "secret", "password", "key")


# ------------------------------------------------------------------ 配置读写

def _read_config() -> dict[str, Any]:
    ensure_dirs()
    try:
        data = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    servers = data.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    return {"servers": servers}


def _write_config(data: dict[str, Any]) -> None:
    ensure_dirs()
    tmp = MCP_CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, MCP_CONFIG_PATH)


def _expand(text: str) -> str:
    """把 ${ENV_VAR} 换成环境变量值。"""
    return ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), str(text))


def _expand_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): _expand(str(v)) for k, v in raw.items() if str(k).strip()}


def _missing_vars(*chunks: Any) -> list[str]:
    """收集仍然残留的 ${VAR}，用于给出明确的配置错误提示。"""
    text = json.dumps(chunks, ensure_ascii=False, default=str)
    return sorted({m for m in ENV_VAR_RE.findall(text)})


def normalize_config(cfg: dict[str, Any], *, require_name: str = "") -> dict[str, Any]:
    """把前端/模型传来的松散配置规范化为落盘结构，顺便做校验。"""
    transport = str(cfg.get("transport") or "stdio").lower()
    if transport in ("streamable_http", "streamable-http", "streamablehttp"):
        transport = "http"
    if transport not in ("stdio", "http", "sse"):
        raise ValueError("transport 只能是 stdio / http / sse")
    clean: dict[str, Any] = {
        "transport": transport,
        "enabled": bool(cfg.get("enabled", True)),
        "auto_start": bool(cfg.get("auto_start", True)),
        "description": str(cfg.get("description") or ""),
    }
    if transport == "stdio":
        command = str(cfg.get("command") or "").strip()
        if not command:
            raise ValueError("stdio 服务必须提供启动命令，例如 npx / uvx / python")
        clean["command"] = command
        args = cfg.get("args") or []
        clean["args"] = [str(a) for a in args] if isinstance(args, list) else []
        clean["env"] = _expand_map(cfg.get("env"))
        if cfg.get("cwd"):
            clean["cwd"] = str(cfg["cwd"])
    else:
        url = str(cfg.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("http / sse 服务需要以 http:// 或 https:// 开头的 url")
        clean["url"] = url
        headers = _expand_map(cfg.get("headers"))
        token = str(cfg.get("token") or "").strip()
        if token:
            clean["token"] = _expand(token)
            if not any(k.lower() == "authorization" for k in headers):
                headers["Authorization"] = f"Bearer {clean['token']}"
        clean["headers"] = headers
    return clean


def auth_headers(cfg: dict[str, Any]) -> dict[str, str]:
    """最终发给 http/sse 的请求头。"""
    headers = dict(cfg.get("headers") or {})
    token = str(cfg.get("token") or "").strip()
    if token and not any(k.lower() == "authorization" for k in headers):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}••••{value[-4:]}"


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for key, val in headers.items():
        if any(s in key.lower() for s in _SECRET_KEYS):
            out[key] = _mask(val)
        else:
            out[key] = val
    return out


def mask_env(env: dict[str, str]) -> dict[str, str]:
    out = {}
    for key, val in env.items():
        if any(s in key.lower() for s in _SECRET_KEYS):
            out[key] = _mask(val)
        else:
            out[key] = val
    return out


def list_servers(*, reveal_secrets: bool = False) -> list[dict[str, Any]]:
    data = _read_config()
    out: list[dict[str, Any]] = []
    for name, cfg in data["servers"].items():
        if not isinstance(cfg, dict):
            continue
        runtime = _manager.runtimes.get(name)
        headers = dict(cfg.get("headers") or {})
        env = dict(cfg.get("env") or {})
        if not reveal_secrets:
            headers = mask_headers(headers)
            env = mask_env(env)
        out.append(
            {
                "name": name,
                "transport": cfg.get("transport", "stdio"),
                "command": cfg.get("command"),
                "args": cfg.get("args") or [],
                "cwd": cfg.get("cwd") or "",
                "url": cfg.get("url"),
                "headers": headers,
                "env": env,
                "token": _mask(str(cfg.get("token"))) if cfg.get("token") and not reveal_secrets else (cfg.get("token") or ""),
                "auth": bool(cfg.get("headers") or cfg.get("token") or cfg.get("env")),
                "description": cfg.get("description", ""),
                "enabled": bool(cfg.get("enabled", True)),
                "auto_start": bool(cfg.get("auto_start", True)),
                "state": runtime.state if runtime else "disconnected",
                "detail": runtime.error if runtime else "",
                "tools": runtime.tool_summaries() if runtime else [],
                "tool_count": len(runtime.tools) if runtime else 0,
            }
        )
    return sorted(out, key=lambda s: s["name"])


def get_server(name: str) -> dict[str, Any] | None:
    return _read_config()["servers"].get(name)


def _restore_masked(clean: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """前端展示的是打码后的密钥；原样提交回来时保留旧值，避免一编辑就把密钥抹掉。"""
    MASK = "••••"
    for field_name in ("headers", "env"):
        new_map = dict(clean.get(field_name) or {})
        old_map = dict(old.get(field_name) or {})
        for key, val in new_map.items():
            if MASK in str(val) and key in old_map:
                new_map[key] = old_map[key]
        clean[field_name] = new_map
    token = str(clean.get("token") or "")
    if MASK in token and old.get("token"):
        clean["token"] = old["token"]
        if clean.get("headers"):
            for key, val in clean["headers"].items():
                if MASK in str(val) and "authorization" in key.lower():
                    clean["headers"][key] = f"Bearer {old['token']}"
    return clean


def upsert_server(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    name = (name or "").strip()
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\s]{0,39}$", name):
        raise ValueError("服务名只能用字母、数字、下划线、短横线（1-40 字符）")
    clean = normalize_config(cfg)
    data = _read_config()
    clean = _restore_masked(clean, data["servers"].get(name) or {})
    data["servers"][name] = clean
    _write_config(data)
    return {"ok": True, "name": name, "config": {**clean, "headers": mask_headers(clean.get("headers") or {})}}


def remove_server(name: str) -> dict[str, Any]:
    data = _read_config()
    if name not in data["servers"]:
        raise KeyError(f"MCP 服务不存在：{name}")
    data["servers"].pop(name)
    _write_config(data)
    return {"ok": True, "name": name}


def _resolve_command(command: str) -> tuple[str, list[str]]:
    """Windows 上 npx/npm 是 .cmd，直接开进程会失败，需要 cmd /c 包一层。"""
    found = shutil.which(command)
    path = found or command
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        return "cmd", ["/c", path]
    return path, []


def _explain_error(exc: Exception) -> str:
    if isinstance(exc, BaseExceptionGroup):
        inner = [_explain_inner(e) for e in exc.exceptions]
        text = "; ".join(t for t in inner if t) or f"{type(exc).__name__}: {exc}"
    else:
        text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if any(code in low for code in ("401", "unauthorized", "403", "forbidden")):
        return text + " —— 看起来是鉴权问题：请在服务配置里补 headers（如 Authorization: Bearer …）或 token"
    if "timed out" in low or isinstance(exc, asyncio.TimeoutError):
        return text + " —— 连接超时：检查地址是否可达，远程服务是否需要走代理"
    return text


def _explain_inner(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(t for t in (_explain_inner(e) for e in exc.exceptions) if t)
    msg = str(exc) or type(exc).__name__
    code = getattr(exc, "code", None)
    return f"{msg}（code={code}）" if code not in (None, "") else msg


def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".local")


def _httpx_module():
    try:
        import httpx2  # MCP SDK 2.x 依赖

        return httpx2
    except ImportError:  # pragma: no cover
        import httpx

        return httpx


def _new_http_client(url: str, headers: dict[str, str] | None = None):
    """本地地址强制绕过系统代理，否则 127.0.0.1 的请求会被代理吞掉。"""
    hx = _httpx_module()
    timeout = hx.Timeout(30.0, read=300.0)
    return hx.AsyncClient(
        headers=headers or None,
        timeout=timeout,
        trust_env=not _is_local(url),
    )


async def _http_status_hint(url: str, headers: dict[str, str]) -> str:
    """MCP SDK 会把 HTTP 状态码吞成通用错误，这里单独探一次拿到真实状态。"""
    try:
        hx = _httpx_module()
        async with hx.AsyncClient(
            timeout=8.0, trust_env=not _is_local(url), follow_redirects=True
        ) as client:
            resp = await client.get(url, headers=headers or {})
            code = resp.status_code
    except Exception as exc:  # noqa: BLE001
        return f"无法连接到 {url}（{type(exc).__name__}: {exc}）"
    if code in (401, 403):
        carried = "已携带请求头" if headers else "没有携带任何鉴权请求头"
        return f"HTTP {code}：鉴权失败，当前{carried}，请检查 token / headers 是否正确或已过期"
    if code == 404:
        return "HTTP 404：地址路径可能不对，检查是否需要 /mcp 之类的前缀"
    if code >= 500:
        return f"HTTP {code}：服务端错误，检查远程服务日志"
    return f"HTTP {code}：地址可达但不是有效的 MCP 端点（或握手被拒绝）"


async def explain_failure(conf: dict[str, Any], exc: Exception) -> str:
    text = _explain_error(exc)
    if (conf.get("transport") or "stdio") in ("http", "sse") and conf.get("url"):
        low = text.lower()
        if any(k in low for k in ("error response", "unhandled", "taskgroup", "internal error", "not found", "bad gateway")):
            text += " —— " + await _http_status_hint(conf["url"], auth_headers(conf))
    return text


# ------------------------------------------------------------------ 运行时

@dataclass
class ServerRuntime:
    name: str
    state: str = "disconnected"
    error: str = ""
    session: Any = None
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    stack: AsyncExitStack | None = None
    started_at: float = 0.0
    config: dict[str, Any] = field(default_factory=dict)

    def tool_summaries(self) -> list[dict[str, str]]:
        return [
            {
                "name": tool["name"],
                "exposed": tool["exposed"],
                "description": (tool.get("description") or "")[:160],
            }
            for tool in self.tools.values()
        ]

    def tool_details(self) -> list[dict[str, Any]]:
        out = []
        for tool in self.tools.values():
            out.append(
                {
                    "name": tool["name"],
                    "exposed": tool["exposed"],
                    "description": tool.get("description") or "",
                    "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        return sorted(out, key=lambda t: t["name"])


class MCPManager:
    def __init__(self) -> None:
        self.runtimes: dict[str, ServerRuntime] = {}
        self._lock = asyncio.Lock()

    # ---------------- 连接管理 ----------------

    async def _open_transport(self, stack: AsyncExitStack, conf: dict[str, Any], server: str) -> tuple[Any, Any]:
        transport = (conf.get("transport") or "stdio").lower()
        headers = auth_headers(conf)

        if transport == "http":
            from mcp.client.streamable_http import streamable_http_client

            http_client = _new_http_client(conf["url"], headers or None)
            await stack.enter_async_context(http_client)
            try:
                streams = await stack.enter_async_context(
                    streamable_http_client(conf["url"], http_client=http_client)
                )
            except TypeError:  # 旧版本签名
                streams = await stack.enter_async_context(streamable_http_client(conf["url"]))
            return streams[0], streams[1]

        if transport == "sse":
            from mcp.client.sse import sse_client

            def _factory(headers=None, timeout=None, auth=None):  # noqa: ANN001
                return _new_http_client(conf["url"], headers)

            streams = await stack.enter_async_context(
                sse_client(
                    conf["url"],
                    headers=headers or None,
                    timeout=CONNECT_TIMEOUT,
                    httpx_client_factory=_factory,
                )
            )
            return streams[0], streams[1]

        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        missing = _missing_vars(conf.get("env"), conf.get("args"), conf.get("command"))
        if missing:
            raise ValueError(f"环境变量未设置：{', '.join(missing)}")
        base_cmd, prefix = _resolve_command(str(conf["command"]))
        params = StdioServerParameters(
            command=base_cmd,
            args=prefix + [str(a) for a in (conf.get("args") or [])],
            env={
                **os.environ,
                **{k: v for k, v in (conf.get("env") or {}).items()},
                "MCP_SERVER_NAME": server,
            },
            cwd=conf.get("cwd") or None,
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        return read, write

    async def _collect_tools(self, session: Any, name: str, runtime: ServerRuntime) -> None:
        listing = await asyncio.wait_for(session.list_tools(), timeout=CONNECT_TIMEOUT)
        runtime.tools.clear()
        for tool in getattr(listing, "tools", []) or []:
            exposed = f"mcp__{SAFE_NAME_RE.sub('_', name)}__{SAFE_NAME_RE.sub('_', tool.name)}"
            schema = getattr(tool, "input_schema", None) or {"type": "object", "properties": {}}
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            runtime.tools[exposed] = {
                "name": tool.name,
                "exposed": exposed,
                "description": getattr(tool, "description", "") or f"{name} 提供的工具",
                "parameters": schema,
            }

    async def connect(self, name: str, cfg: dict[str, Any] | None = None) -> ServerRuntime:
        conf = cfg or get_server(name)
        if not conf:
            raise KeyError(f"MCP 服务不存在：{name}")

        async with self._lock:
            old = self.runtimes.get(name)
            if old and old.state in ("ready", "connecting"):
                return old
            runtime = ServerRuntime(name=name, state="connecting", config=conf)
            self.runtimes[name] = runtime

            stack = AsyncExitStack()
            try:
                read, write = await self._open_transport(stack, conf, name)
                from mcp import ClientSession

                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT)
                await self._collect_tools(session, name, runtime)

                runtime.session = session
                runtime.stack = stack
                runtime.started_at = time.time()
                runtime.state = "ready"
                runtime.error = ""
            except Exception as exc:  # noqa: BLE001
                runtime.state = "error"
                runtime.error = await explain_failure(conf, exc)
                try:
                    await stack.aclose()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._refresh_registry()
            return runtime

    async def disconnect(self, name: str) -> None:
        runtime = self.runtimes.pop(name, None)
        if runtime and runtime.stack:
            try:
                await runtime.stack.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._refresh_registry()

    async def reconnect(self, name: str) -> ServerRuntime:
        await self.disconnect(name)
        return await self.connect(name)

    async def close_all(self) -> None:
        for name in list(self.runtimes):
            await self.disconnect(name)

    async def auto_start(self) -> list[dict[str, Any]]:
        data = _read_config()["servers"]
        results = []
        for name, conf in data.items():
            if not isinstance(conf, dict):
                continue
            if not conf.get("enabled", True) or not conf.get("auto_start", True):
                continue
            runtime = await self.connect(name, conf)
            results.append({"name": name, "state": runtime.state, "error": runtime.error})
        return results

    # ---------------- 工具调用 ----------------

    async def ensure_ready(self, server: str) -> ServerRuntime:
        runtime = self.runtimes.get(server)
        if runtime is None or runtime.state != "ready" or runtime.session is None:
            runtime = await self.connect(server)
        if runtime.state != "ready" or runtime.session is None:
            raise RuntimeError(f"MCP 服务 {server} 不可用：{runtime.error or '未连接'}")
        return runtime

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> str:
        runtime = await self.ensure_ready(server)
        result = await asyncio.wait_for(
            runtime.session.call_tool(tool, arguments or {}), timeout=CALL_TIMEOUT
        )
        return _render_result(result)

    async def call_tool_detail(
        self, server: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """调试面板用：返回内容、是否错误、耗时。"""
        started = time.perf_counter()
        runtime = await self.ensure_ready(server)
        try:
            result = await asyncio.wait_for(
                runtime.session.call_tool(tool, arguments or {}), timeout=CALL_TIMEOUT
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "is_error": True,
                "content": f"调用超时（>{CALL_TIMEOUT:.0f}s）",
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "is_error": True,
                "content": _explain_error(exc),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        is_error = bool(getattr(result, "is_error", False))
        return {
            "ok": not is_error,
            "is_error": is_error,
            "content": _render_result(result),
            "structured": getattr(result, "structured_content", None),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "server": server,
            "tool": tool,
        }

    async def list_tools(self, name: str) -> list[dict[str, Any]]:
        runtime = await self.ensure_ready(name)
        await self._collect_tools(runtime.session, name, runtime)
        self._refresh_registry()
        return runtime.tool_details()

    async def refresh_tools(self, name: str) -> ServerRuntime:
        runtime = self.runtimes.get(name)
        if runtime is None or runtime.state != "ready":
            return await self.connect(name)
        await self._collect_tools(runtime.session, name, runtime)
        self._refresh_registry()
        return runtime

    # ---------------- 注册到工具表 ----------------

    def _refresh_registry(self) -> None:
        from .tools.registry import set_dynamic_tools

        if not load_config().get("mcp", {}).get("inline_tools", False):
            # 按需模式：不把 MCP 工具塞进提示词，模型先用 mcp_list_tools 查、再 mcp_call_tool 调。
            # 否则上百个工具的 schema 能把提示词撑到两万 token。
            set_dynamic_tools([])
            return

        specs = []
        for server, runtime in self.runtimes.items():
            if runtime.state != "ready":
                continue
            for exposed, tool in runtime.tools.items():
                specs.append(
                    {
                        "name": exposed,
                        "description": f"[MCP:{server}] {tool.get('description') or tool['name']}",
                        "parameters": tool.get("parameters"),
                        "category": f"MCP · {server}",
                        "server": server,
                        "tool": tool["name"],
                    }
                )
        set_dynamic_tools(specs)


async def test_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """用一份还没保存的配置试连接，成功则返回工具列表。"""
    name = "_probe"
    conf = normalize_config(cfg, require_name=name)
    runtime = ServerRuntime(name=name, state="connecting", config=conf)
    stack = AsyncExitStack()
    started = time.perf_counter()
    try:
        read, write = await _manager._open_transport(stack, conf, name)
        from mcp import ClientSession

        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT)
        await _manager._collect_tools(session, name, runtime)
        return {
            "ok": True,
            "state": "ready",
            "error": "",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "tools": runtime.tool_details(),
            "tool_count": len(runtime.tools),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "state": "error",
            "error": await explain_failure(conf, exc),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "tools": [],
            "tool_count": 0,
        }
    finally:
        try:
            await stack.aclose()
        except Exception:  # noqa: BLE001
            pass


def _render_result(result: Any) -> str:
    is_error = bool(getattr(result, "is_error", False))
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(getattr(block, "text", ""))
        elif kind == "image":
            parts.append(f"（图片结果：{getattr(block, 'mime_type', 'image')}，已省略二进制）")
        elif kind == "resource":
            parts.append(f"（资源：{getattr(block, 'uri', '')}）")
        else:
            parts.append(str(block))
    structured = getattr(result, "structured_content", None)
    if not parts and structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))
    body = "\n".join(p for p in parts if p).strip() or "（MCP 工具没有返回文本内容）"
    if len(body) > MAX_TOOL_RESULT:
        body = body[:MAX_TOOL_RESULT] + "\n…（结果过长已截断）"
    return ("[MCP 工具返回错误] " if is_error else "") + body


_manager = MCPManager()


def manager() -> MCPManager:
    return _manager


def tool_hint() -> str:
    """系统提示词里描述的 MCP 状态。"""
    servers = list_servers()
    if not servers:
        return (
            "## MCP 服务\n"
            f"当前没有配置 MCP 服务。配置文件固定为 {MCP_CONFIG_PATH}。"
            "如果用户要求接入某个 MCP（例如 filesystem、git、浏览器等），"
            "用 `mcp_add_server` 工具写入配置，再用 `mcp_connect` 建立连接即可，无需重启服务。"
            "远程服务需要鉴权时，在配置里传 token 或 headers。"
        )
    lines = [
        "## MCP 服务",
        f"配置文件：{MCP_CONFIG_PATH}。已接入的服务：",
    ]
    for item in servers:
        state = {"ready": "已连接", "error": "连接失败", "connecting": "连接中"}.get(
            item["state"], item["state"]
        )
        detail = item.get("error") or item.get("detail") or ""
        suffix = f"（{detail}）" if item["state"] == "error" and detail else ""
        auth = "，需鉴权" if item["auth"] else ""
        lines.append(
            f"- {item['name']}：{state}{suffix}，{item['tool_count']} 个工具{auth}"
            + (f"，{item['description']}" if item["description"] else "")
        )
    inlined = load_config().get("mcp", {}).get("inline_tools", False)
    if inlined:
        lines.append(
            "MCP 工具名格式为 `mcp__<服务名>__<工具名>`，调用时必须写全名（不要只写工具名），"
            "否则会提示「工具不存在」。"
            "需要查看某个服务的工具或调试调用时，用 mcp_list_tools / mcp_call_tool。"
            "新增或修改用 mcp_add_server（支持 token / headers / env 鉴权）/ mcp_remove_server / mcp_connect。"
        )
    else:
        lines.append(
            "这些服务的工具没有直接列出（避免提示词过长）。要用 MCP 时按两步走："
            "① 先 `mcp_list_tools(name=服务名)` 拿到工具名和参数；"
            "② 再 `mcp_call_tool(name=服务名, tool=工具名, arguments={...})` 调用，"
            "工具名和参数照抄列表里的，不要自己编。"
            "新增或修改服务用 mcp_add_server / mcp_remove_server / mcp_connect。"
        )
    return "\n".join(lines)
