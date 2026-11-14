"""HelloAgent FastAPI 应用：REST + SSE 流式接口。"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import platform
import re
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, agent, context as ctx_util, memory, model_hub, multimodal, runtime, skills, spaces, workspace
from . import schedule_parse, usage_report
from .llm import PROVIDER_KINDS, RunHandle, fetch_models, get_provider, reset_providers, vision_capable
from .llm.hf_local import cache_stats, clear_prefix_caches, evict_all_engines
from .runs import auto_title, manager as runs_manager
from .scheduler import scheduler
from .mcp_client import (
    get_server,
    list_servers,
    manager as mcp_manager,
    remove_server,
    test_config as mcp_test_config,
    upsert_server,
)
from .settings import (
    APP_NAME,
    APP_TAGLINE,
    THINKING_BUDGET,
    WEB_DIR,
    ensure_dirs,
    get_connection,
    load_config,
    paths_info,
    public_config,
    resolve_path,
    save_config,
)
from .store import store
from .tools import list_tools, schema_list

ACTIVE_RUNS: dict[str, RunHandle] = {}


# ------------------------------------------------------------------ 工作位置（任务 / 空间）
#
# 文件类接口都要知道「现在在哪个目录干活」：带 conversation_id 就跟随会话，
# 只带 space_id 就按空间，两个都没有就是任务（项目根目录）。

async def scope_ctx(
    space_id: str | None = None, conversation_id: str | None = None
) -> AsyncIterator[Any]:
    try:
        root, _space = spaces.resolve_root(space_id=space_id, conversation_id=conversation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = runtime.set_context(
        runtime.TurnContext(conversation_id=conversation_id or "", workspace_root=str(root))
    )
    try:
        yield root
    finally:
        runtime.reset(token)


async def conversation_ctx(conv_id: str) -> AsyncIterator[Any]:
    conv = store.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    root = spaces.root_for_conversation(conv)
    token = runtime.set_context(
        runtime.TurnContext(conversation_id=conv_id, workspace_root=str(root))
    )
    try:
        yield root
    finally:
        runtime.reset(token)


async def _warm_prefix(model: dict[str, Any]) -> dict[str, Any]:
    """should_stop：有对话在跑就让出 CPU。"""
    """把「系统提示 + 工具 schema」这段固定前缀先 prefill 好，首条消息也能秒开。"""
    cfg = load_config()
    tools = schema_list(agent._disabled_tools(cfg)) if cfg["tools"].get("enabled", True) else None
    conv = {
        "id": "__warm__",
        "system_prompt": "",
        "active_skills": [],
        "summary": "",
        "summary_until": 0,
    }
    # 只预热稳定的那段（系统提示 + 工具 schema），易变内容每轮都会变，预热没意义
    messages = [{"role": "system", "content": agent.build_system_prompt(conv, cfg, [])}]
    provider = get_provider(model)
    if not hasattr(provider, "warm"):
        return {"ok": False, "detail": "当前后端不需要预热"}
    try:
        return await provider.warm(messages, tools, should_stop=lambda: bool(runs_manager.by_conv))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    skills.ensure_defaults()
    spaces.ensure_seed()
    cfg = load_config()
    print(f"\n  {APP_NAME} v{__version__} · {APP_TAGLINE}")
    print(f"  配置文件: {resolve_path('data/config.json')}")
    print(f"  任务目录: {spaces.task_root()}")
    known_spaces = store.list_spaces()
    if known_spaces:
        names = "、".join(s["name"] for s in known_spaces[:6])
        print(f"  空间    : {len(known_spaces)} 个（{names}{'…' if len(known_spaces) > 6 else ''}）")
    print(f"  技能库  : {paths_info()['skills']}")

    preload = [
        m for m in cfg["models"] if m.get("provider") == "local_hf" and m.get("load_on_start")
    ]

    async def _preload(model: dict[str, Any]) -> None:
        try:
            provider = get_provider(model)
            await provider.load()
            print(f"  模型已就绪: {model.get('label')}")
            if model.get("provider") == "local_hf" and (load_config().get("perf") or {}).get(
                "warm_prefix", True
            ):
                info = await _warm_prefix(model)
                if info.get("ok"):
                    print(
                        f"  前缀已预热: {info['tokens']} tok / {info['seconds']}s"
                        "（首条消息不再等 prefill）"
                    )
                else:
                    print(f"  前缀预热跳过: {info.get('detail') or info}")
        except Exception as exc:  # noqa: BLE001
            print(f"  预加载失败（{model.get('label')}）: {exc}")

    tasks = [asyncio.create_task(_preload(m)) for m in preload]

    async def _boot_mcp() -> None:
        if not cfg.get("mcp", {}).get("auto_connect", True):
            return
        try:
            results = await mcp_manager().auto_start()
        except Exception as exc:  # noqa: BLE001
            print(f"  MCP 启动失败: {exc}")
            return
        ready = [r["name"] for r in results if r["state"] == "ready"]
        if ready:
            print(f"  MCP 已连接: {', '.join(ready)}")

    mcp_task = asyncio.create_task(_boot_mcp())
    scheduler.start()
    yield
    await scheduler.stop()
    mcp_task.cancel()
    for task in tasks:
        task.cancel()
    await mcp_manager().close_all()


app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan)


# ------------------------------------------------------------------ 基础

@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "app": APP_NAME,
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "time": time.time(),
    }


@app.get("/api/system")
async def system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": None,
        "torch": None,
        "device": None,
        "device_name": None,
        "total_vram": None,
        "paths": paths_info(),
        "mcp_available": True,
    }
    try:
        import os

        info["cpu_count"] = os.cpu_count()
    except Exception:  # noqa: BLE001
        pass
    try:
        import torch

        info["torch"] = torch.__version__
        if torch.cuda.is_available():
            info["device"] = "cuda"
            info["device_name"] = torch.cuda.get_device_name(0)
            info["total_vram"] = round(
                torch.cuda.get_device_properties(0).total_memory / 1024**3, 1
            )
        else:
            info["device"] = "cpu"
            info["device_name"] = platform.processor() or "CPU"
    except Exception:  # noqa: BLE001
        info["device"] = "unavailable"
    try:
        import transformers

        info["transformers"] = transformers.__version__
    except Exception:  # noqa: BLE001
        info["transformers"] = None
    try:
        import mcp as _mcp

        info["mcp"] = getattr(_mcp, "__version__", "installed")
    except Exception:  # noqa: BLE001
        info["mcp_available"] = False
        info["mcp"] = None
    info["workspace"] = workspace.stats()
    return info


# ------------------------------------------------------------------ 性能

@app.get("/api/perf")
async def perf_info() -> dict[str, Any]:
    """推理加速状态：配置 + 前缀缓存命中情况。"""
    cfg = load_config()
    perf = dict(cfg.get("perf") or {})
    stats = cache_stats()
    # KV 大小估算：Qwen3-0.6B 每 token 约 224KB
    cached = int(stats.get("cached_tokens") or 0)
    perf["cached_mb"] = round(cached * 0.224, 1)
    return {"config": perf, "cache": stats, "active_model_id": cfg.get("active_model_id")}


@app.post("/api/perf/reset")
async def perf_reset() -> dict[str, Any]:
    """清空前缀缓存（换系统提示词/工具后想重新计时用）。"""
    return {"ok": True, "engines": clear_prefix_caches()}


@app.post("/api/perf/warm")
async def perf_warm() -> dict[str, Any]:
    """手动预热固定前缀。"""
    cfg = load_config()
    model = next((m for m in cfg["models"] if m["id"] == cfg["active_model_id"]), None)
    if model is None or model.get("provider") != "local_hf":
        raise HTTPException(status_code=400, detail="当前模型不是本地 transformers 后端")
    return await _warm_prefix(model)


# ------------------------------------------------------------------ 配置

@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    cfg = public_config()
    cfg["provider_kinds"] = PROVIDER_KINDS
    cfg["paths"] = paths_info()
    cfg["thinking_levels"] = [
        {"value": "off", "label": "关闭"},
        {"value": "low", "label": "轻度"},
        {"value": "medium", "label": "标准"},
        {"value": "high", "label": "深入"},
    ]
    cfg["thinking_budget"] = THINKING_BUDGET
    return cfg


@app.put("/api/config")
async def put_config(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_config()
    incoming = payload or {}

    models = incoming.get("models")
    if isinstance(models, list):
        existing = {m["id"]: m for m in current["models"]}
        for model in models:
            if not isinstance(model, dict):
                continue
            if not model.get("api_key"):
                old = existing.get(model.get("id", ""))
                if old:
                    model["api_key"] = old.get("api_key", "")

    # 连接上的密钥同理：留空表示沿用已保存的密钥（避免每次编辑都要重填）
    connections = incoming.get("connections")
    if isinstance(connections, list):
        existing_conns = {c["id"]: c for c in current.get("connections") or []}
        for conn in connections:
            if not isinstance(conn, dict):
                continue
            if not conn.get("api_key"):
                old = existing_conns.get(conn.get("id", ""))
                if old:
                    conn["api_key"] = old.get("api_key", "")

    merged = {**current, **incoming}
    for section in ("defaults", "tools", "ui", "workspace", "skills", "memory", "mcp", "perf"):
        if section in incoming:
            merged[section] = {**current.get(section, {}), **(incoming[section] or {})}

    saved = save_config(merged)
    reset_providers()
    # int8 / 线程数 / 缓存额度变了必须丢掉旧引擎，否则会同时驻留两份权重
    if (current.get("perf") or {}) != (saved.get("perf") or {}):
        evict_all_engines()
    # MCP 内联开关变了要立刻重建动态工具表，否则要等下次连接才生效
    if (current.get("mcp") or {}).get("inline_tools") != (saved.get("mcp") or {}).get(
        "inline_tools"
    ):
        mcp_manager()._refresh_registry()  # noqa: SLF001
    view = public_config()
    view["provider_kinds"] = PROVIDER_KINDS
    view["paths"] = paths_info()
    # 工作目录可能变了，确保目录存在
    spaces.task_root().mkdir(parents=True, exist_ok=True)
    return view


# ------------------------------------------------------------------ 模型

@app.get("/api/models")
async def list_models(probe: bool = False) -> dict[str, Any]:
    cfg = load_config()
    conn_labels = {c["id"]: c.get("label") or c["id"] for c in cfg.get("connections") or []}
    items = []
    for model in cfg["models"]:
        provider = get_provider(model)
        if model.get("provider") == "local_hf":
            status = await provider.status()
        elif probe:
            try:
                status = await asyncio.wait_for(provider.status(), timeout=3.0)
            except asyncio.TimeoutError:
                status = {"state": "offline", "detail": "探测超时"}
        else:
            status = {
                "state": "unknown",
                "detail": "未探测",
                "base_url": model.get("base_url"),
                "model": model.get("model"),
            }
        items.append(
            {
                "id": model.get("id"),
                "label": model.get("label"),
                "provider": model.get("provider"),
                "connection_id": model.get("connection_id"),
                "connection_label": conn_labels.get(model.get("connection_id"), ""),
                "model": model.get("model"),
                "context_window": model.get("context_window"),
                "active": model.get("id") == cfg["active_model_id"],
                "vision": model.get("vision") or "auto",
                "supports_vision": vision_capable(model),
                "status": status,
            }
        )
    return {"models": items, "active_model_id": cfg["active_model_id"]}


@app.post("/api/connections/probe")
async def probe_connection(payload: dict[str, Any]) -> dict[str, Any]:
    """用「服务地址 + 密钥」拉一次可用模型列表，用于新增/编辑连接时挑模型。

    密钥留空时回落到该连接已保存的密钥，所以编辑连接不必重新粘贴 key。
    """
    base_url = str((payload or {}).get("base_url") or "").strip()
    api_key = str((payload or {}).get("api_key") or "")
    conn = get_connection(str((payload or {}).get("connection_id") or ""))
    if conn:
        base_url = base_url or str(conn.get("base_url") or "")
        if not api_key:
            api_key = str(conn.get("api_key") or "")
    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写服务地址")
    return await fetch_models(base_url, api_key)


def _require_model(model_id: str) -> dict[str, Any]:
    for model in load_config()["models"]:
        if model.get("id") == model_id:
            return model
    raise HTTPException(status_code=404, detail=f"模型档案不存在: {model_id}")


@app.post("/api/models/{model_id}/load")
async def load_model(model_id: str) -> dict[str, Any]:
    profile = _require_model(model_id)
    provider = get_provider(profile)
    try:
        await provider.load()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return await provider.status()


@app.post("/api/models/{model_id}/unload")
async def unload_model(model_id: str) -> dict[str, Any]:
    profile = _require_model(model_id)
    provider = get_provider(profile)
    await provider.unload()
    return await provider.status()


@app.get("/api/models/{model_id}/status")
async def model_status(model_id: str) -> dict[str, Any]:
    profile = _require_model(model_id)
    return await get_provider(profile).status()


# ------------------------------------------------------------------ 模型库

@app.get("/api/hub")
async def hub_info() -> dict[str, Any]:
    cfg = model_hub.hub_config()
    return {
        **cfg,
        "models_dir": str(model_hub.models_root()),
        "resolved_cache": str(model_hub.cache_path()),
        "known": model_hub.KNOWN_REPOS,
        "tasks": model_hub.active_tasks(),
    }


@app.post("/api/models/download")
async def models_download(payload: dict[str, Any]) -> dict[str, Any]:
    """从模型库下载权重到 models/<org>--<name>，完成后自动登记模型档案。"""
    try:
        return model_hub.start(
            str((payload or {}).get("repo_id") or ""),
            str((payload or {}).get("source") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/models/download/{task_id}")
async def models_download_status(task_id: str) -> dict[str, Any]:
    task = model_hub.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="下载任务不存在")
    return task


@app.post("/api/models/download/{task_id}/cancel")
async def models_download_cancel(task_id: str) -> dict[str, Any]:
    task = model_hub.cancel(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="下载任务不存在")
    return task


# ------------------------------------------------------------------ 工具

@app.get("/api/tools")
async def get_tools() -> dict[str, Any]:
    cfg = load_config()
    disabled = set(cfg["tools"]["disabled"])
    if not cfg["tools"].get("allow_shell", True):
        disabled.update({"run_command", "run_python"})
    return {
        "tools": list_tools(),
        "enabled": cfg["tools"]["enabled"],
        "disabled": sorted(disabled),
        "max_rounds": cfg["tools"]["max_rounds"],
        "max_rounds_limit": cfg["tools"].get("max_rounds_limit", 200),
        "allow_shell": cfg["tools"].get("allow_shell", True),
    }


# ------------------------------------------------------------------ 空间

@app.get("/api/spaces")
async def get_spaces() -> dict[str, Any]:
    return spaces.list_view()


@app.post("/api/spaces")
async def create_space(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        space = spaces.create(
            str(payload.get("name") or ""),
            mode=str(payload.get("mode") or "created"),
            parent=payload.get("parent"),
            path=payload.get("path"),
            icon=str(payload.get("icon") or ""),
            description=str(payload.get("description") or ""),
            instructions=str(payload.get("instructions") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"space": spaces.scope_view(space), "spaces": spaces.list_view()}


@app.patch("/api/spaces/{space_id}")
async def patch_space(space_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "path", "icon", "description", "instructions", "pinned", "archived"}
    fields = {k: v for k, v in (payload or {}).items() if k in allowed}
    try:
        spaces.update(space_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"spaces": spaces.list_view()}


@app.delete("/api/spaces/{space_id}")
async def delete_space(space_id: str, mode: str = "detach") -> dict[str, Any]:
    """移除空间：默认只解除登记（对话转成任务、磁盘文件保留）。"""
    try:
        result = spaces.remove(space_id, delete_conversations=(mode == "delete"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {**result, "spaces": spaces.list_view()}


@app.post("/api/spaces/browse")
async def browse_dirs(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return spaces.browse((payload or {}).get("path"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/spaces/reveal")
async def reveal_space(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return spaces.reveal((payload or {}).get("path"))


# ------------------------------------------------------------------ 工作区

@app.get("/api/workspace/tree")
async def workspace_tree(
    path: str = ".", max_entries: int = 400, _scope: Any = Depends(scope_ctx)
) -> dict[str, Any]:
    try:
        return workspace.tree(max_entries=max_entries, sub=path)
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/workspace/info")
async def workspace_info(_scope: Any = Depends(scope_ctx)) -> dict[str, Any]:
    return {"stats": workspace.stats(), "root": str(workspace.root())}


@app.get("/api/workspace/file")
async def workspace_file(
    path: str, max_bytes: int = 0, _scope: Any = Depends(scope_ctx)
) -> dict[str, Any]:
    try:
        return workspace.read_text(path, max_bytes=max_bytes or None)
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/workspace/file")
async def workspace_save(
    payload: dict[str, Any], _scope: Any = Depends(scope_ctx)
) -> dict[str, Any]:
    path = str(payload.get("path") or "")
    try:
        return workspace.write_text(path, str(payload.get("content") or ""))
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/workspace/file")
async def workspace_delete(path: str, _scope: Any = Depends(scope_ctx)) -> dict[str, Any]:
    try:
        return workspace.delete(path)
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/workspace/raw")
async def workspace_raw(path: str, _scope: Any = Depends(scope_ctx)) -> Any:
    try:
        target = workspace.resolve(path, must_exist=True)
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not target.is_file():
        raise HTTPException(status_code=400, detail="不是文件")
    media, _ = mimetypes.guess_type(str(target))
    return FileResponse(str(target), media_type=media or "application/octet-stream")


@app.get("/api/workspace/pdf")
async def workspace_pdf(
    path: str, refresh: int = 0, _scope: Any = Depends(scope_ctx)
) -> Any:
    """把 docx/pptx/xlsx 用本机 Office 渲染成 PDF，供右侧面板内嵌显示。

    面板里要「和原文件一样」，靠的就是 Office 自己的排版引擎；这里只做格式桥接。
    渲染失败返回 503 + 原因，前端退回下载卡片。
    """
    from . import office_pdf

    try:
        target = workspace.resolve(path, must_exist=True)
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not target.is_file():
        raise HTTPException(status_code=400, detail="不是文件")
    if target.suffix.lower() not in office_pdf.CONVERTIBLE:
        raise HTTPException(status_code=400, detail="这个格式不需要渲染")
    # COM 调用是阻塞的，丢到线程池里跑，别占住事件循环
    pdf = await asyncio.to_thread(office_pdf.convert, target, force=bool(refresh))
    if pdf is None:
        reason = (
            "没检测到可用的本机 Office"
            if not office_pdf.available()
            else "本机 Office 渲染失败"
        )
        raise HTTPException(status_code=503, detail=reason)
    return FileResponse(
        str(pdf), media_type="application/pdf", headers={"Cache-Control": "no-cache"}
    )


@app.post("/api/workspace/open")
async def workspace_open(
    payload: dict[str, Any] | None = None, _scope: Any = Depends(scope_ctx)
) -> dict[str, Any]:
    """用系统默认程序打开文件（docx → 本机 Word/WPS，pdf → 默认阅读器）。

    不做任何解析/转换：就是「双击打开」，版式由应用自己渲染。
    """
    return workspace.open_file((payload or {}).get("path"))


@app.post("/api/workspace/reveal-file")
async def workspace_reveal_file(
    payload: dict[str, Any] | None = None, _scope: Any = Depends(scope_ctx)
) -> dict[str, Any]:
    """在文件管理器中定位到某个文件。"""
    return workspace.reveal_file((payload or {}).get("path"))


@app.get("/ws/{path:path}")
async def workspace_static(path: str, _scope: Any = Depends(scope_ctx)) -> Any:
    """把工作区按原目录结构暴露出来，供 HTML 产物在沙箱 iframe 里加载相对资源。"""
    try:
        target = workspace.resolve(path, must_exist=True)
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="不是文件")
    media, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        str(target),
        media_type=media or "application/octet-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/workspace/upload")
async def workspace_upload(
    payload: dict[str, Any], _scope: Any = Depends(scope_ctx)
) -> dict[str, Any]:
    import base64

    path = str(payload.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="缺少文件名")
    try:
        raw = base64.b64decode(str(payload.get("data") or ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"内容不是合法的 base64：{exc}") from exc
    try:
        target = workspace.resolve(path)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return {"ok": True, "path": workspace.rel(target), "bytes": len(raw)}


@app.post("/api/workspace/resolve")
async def workspace_resolve(
    payload: dict[str, Any], _scope: Any = Depends(scope_ctx)
) -> dict[str, Any]:
    """把消息里出现的路径批量校验一遍，只返回工作区内真实存在的文件。"""
    raw_paths = payload.get("paths") or []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_paths[:60]:
        text = str(raw).strip().strip("`'\"（）()[]").replace("\\", "/")
        if not text:
            continue
        try:
            target = workspace.resolve(text, must_exist=True)
        except (PermissionError, ValueError, FileNotFoundError, OSError):
            continue
        if not target.is_file():
            continue
        meta = multimodal.artifact_meta(target)
        if meta["path"] in seen:
            continue
        seen.add(meta["path"])
        found.append(meta)
    return {"files": found}


# ------------------------------------------------------------------ 多模态输入


@app.post("/api/uploads")
async def upload_attachment(
    payload: dict[str, Any], _scope: Any = Depends(scope_ctx)
) -> dict[str, Any]:
    """聊天里的图片/文件：落盘到工作区 `uploads/`，返回卡片与模型都能用的元数据。"""
    import base64

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少文件名")
    try:
        raw = base64.b64decode(str(payload.get("data") or ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"内容不是合法的 base64：{exc}") from exc
    if not raw:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="单个文件不要超过 30MB")
    try:
        meta = multimodal.save_upload(name, raw)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    meta["source"] = "upload"
    return meta


@app.post("/api/workspace/reveal")
async def workspace_reveal(
    payload: dict[str, Any] | None = None, _scope: Any = Depends(scope_ctx)
) -> dict[str, Any]:
    return workspace.open_in_explorer()


# ------------------------------------------------------------------ 技能

@app.get("/api/skills")
async def get_skills() -> dict[str, Any]:
    return {"skills": skills.list_skills(), "dir": paths_info()["skills"]}


@app.get("/api/skills/raw")
async def skill_raw(path: str) -> Any:
    """技能库文件的原始内容（技能库在工作目录之外，产物卡片走这条通道）。

    注意必须声明在 /api/skills/{slug} 之前，否则 raw / file 会被当成 slug。
    """
    target = skills.resolve_file(path)
    if target is None:
        raise HTTPException(status_code=404, detail="技能文件不存在")
    media, _ = mimetypes.guess_type(str(target))
    return FileResponse(str(target), media_type=media or "application/octet-stream")


@app.post("/api/skills/open")
async def open_skill_file(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """用系统默认程序打开技能库里的文件（产物卡片点开时走这条）。"""
    target = skills.resolve_file(str((payload or {}).get("path") or ""))
    if target is None:
        return {"ok": False, "detail": "技能文件不存在"}
    return workspace.open_abs(target)


@app.get("/api/skills/file")
async def skill_file(path: str, max_bytes: int = 2000000) -> dict[str, Any]:
    """技能库文件的文本内容，返回结构与 /api/workspace/file 一致，前端预览器可直接复用。"""
    target = skills.resolve_file(path)
    if target is None:
        raise HTTPException(status_code=404, detail="技能文件不存在")
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if b"\x00" in data[:2048]:
        return {"path": path, "name": target.name, "binary": True, "size": len(data)}
    limit = max_bytes or 2000000
    text = data[:limit].decode("utf-8", errors="replace")
    return {
        "path": path,
        "name": target.name,
        "content": text,
        "binary": False,
        "size": len(data),
        "truncated": len(data) > limit,
    }


@app.get("/api/skills/{slug}")
async def get_skill(slug: str) -> dict[str, Any]:
    try:
        return skills.get(slug)
    except skills.SkillError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/skills")
async def create_skill(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return skills.create(
            str(payload.get("name") or ""),
            str(payload.get("content") or ""),
            overwrite=bool(payload.get("overwrite")),
        )
    except skills.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/skills/{slug}")
async def update_skill(slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return skills.update(
            slug,
            payload.get("content"),
            description=payload.get("description"),
        )
    except skills.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/skills/{slug}")
async def delete_skill(slug: str) -> dict[str, Any]:
    try:
        return skills.delete(slug)
    except skills.SkillError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/skills/{slug}/toggle")
async def toggle_skill(slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    return skills.set_enabled(slug, bool(payload.get("enabled", True)))


# ------------------------------------------------------------------ MCP

@app.get("/api/mcp/servers")
async def mcp_servers() -> dict[str, Any]:
    return {"servers": list_servers(), "config_path": str(paths_info()["mcp"])}


@app.post("/api/mcp/servers")
async def mcp_add(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        name = str(payload.get("name") or "")
        result = upsert_server(name, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.get("connect"):
        runtime = await mcp_manager().reconnect(name)
        result["state"] = runtime.state
        result["error"] = runtime.error
        result["tools"] = runtime.tool_summaries()
    return result


@app.delete("/api/mcp/servers/{name}")
async def mcp_delete(name: str) -> dict[str, Any]:
    await mcp_manager().disconnect(name)
    try:
        return remove_server(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/mcp/servers/{name}/connect")
async def mcp_connect(name: str) -> dict[str, Any]:
    runtime = await mcp_manager().reconnect(name)
    return {
        "name": name,
        "state": runtime.state,
        "error": runtime.error,
        "tools": runtime.tool_summaries(),
    }


@app.post("/api/mcp/servers/{name}/disconnect")
async def mcp_disconnect(name: str) -> dict[str, Any]:
    await mcp_manager().disconnect(name)
    return {"ok": True, "name": name, "state": "disconnected"}


@app.post("/api/mcp/test")
async def mcp_test(payload: dict[str, Any]) -> dict[str, Any]:
    """用未保存的配置试连接（含鉴权），成功返回工具列表。"""
    try:
        return await mcp_test_config(payload)
    except ValueError as exc:
        return {"ok": False, "state": "error", "error": str(exc), "tools": [], "tool_count": 0}


@app.get("/api/mcp/servers/{name}/tools")
async def mcp_server_tools(name: str, refresh: bool = False) -> dict[str, Any]:
    """拉取某个服务的完整工具列表（含 inputSchema），供调试面板使用。"""
    if not get_server(name):
        raise HTTPException(status_code=404, detail=f"MCP 服务不存在：{name}")
    try:
        if refresh:
            runtime = await mcp_manager().reconnect(name)
            if runtime.state != "ready":
                return {
                    "name": name,
                    "state": runtime.state,
                    "error": runtime.error,
                    "tools": [],
                }
        tools = await mcp_manager().list_tools(name)
        runtime = mcp_manager().runtimes.get(name)
        return {
            "name": name,
            "state": runtime.state if runtime else "disconnected",
            "error": runtime.error if runtime else "",
            "tools": tools,
            "count": len(tools),
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "state": "error", "error": str(exc), "tools": []}


@app.post("/api/mcp/servers/{name}/call")
async def mcp_call(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """在调试面板里直接请求某个 MCP 工具。"""
    tool = str(payload.get("tool") or "").strip()
    if not tool:
        raise HTTPException(status_code=400, detail="缺少 tool 参数")
    if not get_server(name):
        raise HTTPException(status_code=404, detail=f"MCP 服务不存在：{name}")
    arguments = payload.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"arguments 不是合法 JSON：{exc}") from exc
    if not isinstance(arguments, dict):
        raise HTTPException(status_code=400, detail="arguments 必须是 JSON 对象")
    result = await mcp_manager().call_tool_detail(name, tool, arguments)
    result["arguments"] = arguments
    return result


# ------------------------------------------------------------------ 记忆

@app.get("/api/memory")
async def get_memory() -> dict[str, Any]:
    return memory.overview()


@app.put("/api/memory")
async def put_memory(payload: dict[str, Any]) -> dict[str, Any]:
    memory.write_text(str(payload.get("text") or ""))
    return memory.overview()


@app.delete("/api/memory")
async def clear_memory(section: str | None = None) -> dict[str, Any]:
    memory.clear(section)
    return memory.overview()


# ------------------------------------------------------------------ 会话

@app.get("/api/conversations")
async def list_conversations(
    q: str | None = None, scope: str | None = None
) -> dict[str, Any]:
    """scope：不传=全部；`task`=任务；其它=空间 id。"""
    return {"conversations": store.list_conversations(q, scope=scope)}


@app.post("/api/conversations")
async def create_conversation(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = load_config()
    payload = payload or {}
    defaults = cfg["defaults"]
    space_id = payload.get("space_id") or None
    if space_id and not store.get_space(space_id):
        raise HTTPException(status_code=400, detail="空间不存在或已被移除")
    conv = store.create_conversation(
        title=payload.get("title") or "新对话",
        model_id=payload.get("model_id") or cfg["active_model_id"],
        system_prompt=defaults["system_prompt"],
        temperature=defaults["temperature"],
        top_p=defaults["top_p"],
        max_tokens=defaults["max_tokens"],
        tools_enabled=cfg["tools"]["enabled"],
        thinking=bool(payload.get("thinking", defaults.get("thinking", False))),
        thinking_level=payload.get("thinking_level") or defaults.get("thinking_level", "medium"),
        space_id=space_id,
    )
    if payload.get("skills"):
        conv = store.update_conversation(conv["id"], active_skills=payload["skills"]) or conv
    # 每个会话一个产物目录，避免所有生成的文件堆在工作目录根部
    try:
        out = spaces.ensure_conversation_dir(conv)
        if out:
            conv["output_dir"] = out
    except Exception:  # noqa: BLE001
        pass
    return conv


@app.patch("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not store.get_conversation(conv_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    if payload.get("space_id") and not store.get_space(str(payload["space_id"])):
        raise HTTPException(status_code=400, detail="空间不存在或已被移除")
    updated = store.update_conversation(conv_id, **payload)
    return updated or {}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str) -> dict[str, Any]:
    # 正在跑的这轮要先停掉，不然它会往已删除的会话里写消息
    run = runs_manager.active_run(conv_id)
    if run:
        runs_manager.stop(run.id)
    runs_manager.queues.pop(conv_id, None)
    store.delete_conversation(conv_id)
    return {"ok": True}


@app.get("/api/conversations/{conv_id}/messages")
async def get_messages(conv_id: str) -> dict[str, Any]:
    conv = store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"conversation": conv, "messages": store.list_messages(conv_id)}


# ------------------------------------------------------------------ 产物（artifacts）

# 会产出文件的工具：从调用参数里直接拿到路径，比正则猜准得多
PRODUCE_TOOLS = {"write_file", "edit_file"}
# 技能工具会写 SKILL.md，路径按 slug 推导
SKILL_TOOLS = {"skill_create", "skill_update"}
ARTIFACT_RE = re.compile(
    r"(?:[\w\u4e00-\u9fa5][\w\u4e00-\u9fa5_.\-]*/)*[\w\u4e00-\u9fa5][\w\u4e00-\u9fa5_.\-]*"
    r"\.(?:md|markdown|html|htm|csv|tsv|json|txt|log|py|js|mjs|ts|tsx|jsx|css|scss|xml"
    r"|yaml|yml|sql|sh|bat|png|jpe?g|gif|webp|svg|bmp|pdf|xlsx?|docx?|pptx?|zip)",
    re.I,
)


def _resolve_artifact(text: str, source: str) -> dict[str, Any] | None:
    """把一段文本解析成产物元数据。

    优先按技能文件解析（skills/ 前缀，在工作目录之外），否则走工作目录守卫；
    都不行就返回 None。技能文件用独立的 raw 路由提供内容。
    """
    from urllib.parse import quote

    raw = str(text or "").strip().strip("`'\"（）()[]").replace("\\", "/")
    if not raw or "://" in raw:
        return None
    target = skills.resolve_file(raw)
    if target is not None:
        rel = "skills/" + target.resolve().relative_to(skills.directory().resolve()).as_posix()
        stat = target.stat()
        kind = multimodal.kind_of(target.name)
        q = quote(rel)
        return {
            "path": rel,
            "name": target.name,
            "kind": kind,
            "lang": multimodal.lang_of(target.name),
            "mime": mimetypes.guess_type(str(target))[0] or "application/octet-stream",
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "source": source,
            "url": f"/api/skills/raw?path={q}",
            "preview": f"/api/skills/raw?path={q}",
            "renderable": kind in ("image", "markdown", "html", "table", "code", "text"),
        }
    try:
        target = workspace.resolve(raw, must_exist=True)
    except (PermissionError, ValueError, FileNotFoundError, OSError):
        return None
    if not target.is_file():
        return None
    return multimodal.artifact_meta(target, source=source)


@app.get("/api/conversations/{conv_id}/artifacts")
async def conversation_artifacts(
    conv_id: str, _scope: Any = Depends(conversation_ctx)
) -> dict[str, Any]:
    """会话里生成过/提到过的文件清单，按「哪条消息产的」归属。

    前端拿它画两样东西：每条回答下面的产物卡片，以及本轮结束时的产物汇总。
    除了工具调用产出的文件，还会合并运行期间自动监听到的文件变更
    （run_command / run_python / 技能写入等所有改动），删除的文件单独放在 removed 里。
    """
    conv = store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = store.list_messages(conv_id)

    items: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    removed_paths: set[str] = set()

    def add_removed(path: str, message_id: str) -> None:
        if path and path not in removed_paths:
            removed_paths.add(path)
            removed.append({"path": path, "message_id": message_id})

    def add(meta: dict[str, Any] | None, message_id: str) -> None:
        if not meta:
            return
        hit = index.get(meta["path"])
        if hit:
            # 同一个文件被写过多次：保留最早归属，刷新大小/时间
            for key in ("size", "mtime", "kind", "lang", "url", "preview", "renderable"):
                hit[key] = meta[key]
            return
        meta["message_id"] = message_id
        index[meta["path"]] = meta
        items.append(meta)

    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            if name in PRODUCE_TOOLS:
                add(_resolve_artifact(str(args.get("path") or ""), "tool"), msg["id"])
            elif name in SKILL_TOOLS:
                slug = str(args.get("name") or "").strip().lower()
                if slug:
                    add(
                        _resolve_artifact(f"skills/{slug}/SKILL.md", "skill"),
                        msg["id"],
                    )
        # 运行期间文件监听到的变更（覆盖 run_command / run_python 等所有改法）
        for change in (msg.get("stats") or {}).get("file_changes") or []:
            if not isinstance(change, dict):
                continue
            path = str(change.get("path") or "")
            if not path:
                continue
            if change.get("action") == "deleted":
                add_removed(path, msg["id"])
                continue
            meta = _resolve_artifact(path, "auto")
            if meta:
                add(meta, msg["id"])
            else:
                # 「建了又删」：变更发生时文件存在、现在没了，按删除展示
                add_removed(path, msg["id"])
        for match in ARTIFACT_RE.findall(msg.get("content") or ""):
            add(_resolve_artifact(match, "mention"), msg["id"])

    # 本轮（最后一条用户消息之后）产出的文件，供底部汇总卡片用
    turn_user = None
    for msg in reversed(messages):
        if msg.get("role") == "user":
            turn_user = msg
            break
    turn_ids = set()
    if turn_user:
        turn_ids = {
            msg["id"]
            for msg in messages
            if int(msg.get("seq") or 0) > int(turn_user.get("seq") or 0)
        }
    turn = [item for item in items if item.get("message_id") in turn_ids]
    turn_removed = [item for item in removed if item.get("message_id") in turn_ids]

    return {
        "artifacts": items,
        "turn": turn,
        "removed": removed,
        "turn_removed": turn_removed,
        "turn_user_id": turn_user["id"] if turn_user else None,
        "count": len(items),
    }


@app.get("/api/conversations/{conv_id}/usage")
async def get_usage(conv_id: str) -> dict[str, Any]:
    """Token 用量明细：逐轮 + 会话汇总 + 工具调用。"""
    conv = store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    return usage_report.build_report(conv, store.list_messages(conv_id), load_config())


@app.get("/api/conversations/{conv_id}/context")
async def get_context(conv_id: str) -> dict[str, Any]:
    conv = store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    cfg = load_config()
    profile = next(
        (m for m in cfg["models"] if m["id"] == (conv.get("model_id") or cfg["active_model_id"])),
        cfg["models"][0],
    )
    messages = agent.build_messages(conv, store.list_messages(conv_id), cfg, profile)
    tools_schema = None
    if cfg["tools"]["enabled"]:
        tools_schema = schema_list(agent._disabled_tools(cfg))
    return {
        "usage": ctx_util.usage(messages, profile, cfg, tools_schema),
        "summary": conv.get("summary"),
        "summary_until": conv.get("summary_until"),
        "compacted_at": conv.get("compacted_at"),
    }


@app.post("/api/conversations/{conv_id}/compact")
async def compact(conv_id: str) -> dict[str, Any]:
    conv = store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    cfg = load_config()
    profile = next(
        (m for m in cfg["models"] if m["id"] == (conv.get("model_id") or cfg["active_model_id"])),
        None,
    )
    if profile is None:
        raise HTTPException(status_code=400, detail="没有可用模型")
    handle = RunHandle()
    tools_schema = None
    if cfg["tools"]["enabled"]:
        tools_schema = schema_list(agent._disabled_tools(cfg))
    try:
        return await agent.compact_conversation(
            conversation=conv,
            profile=profile,
            store=store,
            handle=handle,
            force=True,
            tools=tools_schema,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"压缩失败：{exc}") from exc


@app.get("/api/conversations/{conv_id}/export")
async def export_conversation(conv_id: str) -> Any:
    conv = store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = store.list_messages(conv_id)
    lines = [
        f"# {conv['title']}",
        "",
        f"- 会话 ID：{conv_id}",
        f"- 创建时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(conv['created_at']))}",
        f"- 模型：{conv.get('model_id')}",
        "",
        "---",
        "",
    ]
    for msg in messages:
        role = msg["role"]
        content = (msg.get("content") or "").strip()
        if role == "user":
            lines += ["## 🧑 用户", "", content, ""]
            for att in msg.get("attachments") or []:
                kind = att.get("kind") or "file"
                lines += [f"> 附件（{kind}）：`{att.get('path')}`"]
            if msg.get("attachments"):
                lines.append("")
        elif role == "assistant":
            if msg.get("reasoning"):
                lines += ["<details><summary>思考过程</summary>", "", msg["reasoning"], "", "</details>", ""]
            if content:
                lines += ["## 🤖 助手", "", content, ""]
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                lines += [f"> 调用工具 `{fn.get('name')}`：`{fn.get('arguments')}`", ""]
        elif role == "tool":
            body = content[:1200]
            lines += [f"<details><summary>工具结果 · {msg.get('name')}</summary>", "", body, "", "</details>", ""]
    payload = "\n".join(lines)
    from urllib.parse import quote

    filename = f"{conv['title'][:30].replace('/', '_')}.md"
    return PlainTextResponse(
        payload,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


# ------------------------------------------------------------------ 对话（SSE）

class ChatRequest(BaseModel):
    conversation_id: str | None = None
    content: str = ""
    regenerate: bool = False
    request_id: str | None = None
    model_id: str | None = None
    thinking: bool | None = None
    thinking_level: str | None = None
    skills: list[str] | None = None
    # 多模态输入：图片/文档的元数据（已由 /api/uploads 落盘到工作区）
    attachments: list[dict[str, Any]] | None = None


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _run_response(run: Any, from_seq: int = 0, fresh: bool = False) -> StreamingResponse:
    """把一次 Run 的事件流成 SSE。客户端断开只会取消这个「观察者」，不会停掉生成。"""

    async def event_stream() -> AsyncIterator[str]:
        try:
            if fresh:
                yield _sse(
                    {
                        "type": "run_meta",
                        "run_id": run.id,
                        "conversation_id": run.conv_id,
                        "status": run.status,
                        "source": run.source,
                        "message_ids": list(run.message_ids),
                    }
                )
            async for event in runs_manager.follow(run, from_seq):
                yield _sse(event)
        except asyncio.CancelledError:  # 客户端切走/刷新：只是不看了
            raise
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/chat")
async def chat(payload: ChatRequest) -> StreamingResponse:
    if not payload.conversation_id:
        raise HTTPException(status_code=400, detail="缺少 conversation_id")
    if runs_manager.active_run(payload.conversation_id):
        raise HTTPException(
            status_code=409, detail="该会话正在生成，消息请先进队列（/queue）或等它结束"
        )
    try:
        run = await runs_manager.start(
            payload.conversation_id,
            content=payload.content,
            regenerate=payload.regenerate,
            run_id=payload.request_id,
            model_id=payload.model_id,
            thinking=payload.thinking,
            thinking_level=payload.thinking_level,
            skills=payload.skills,
            attachments=payload.attachments,
        )
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=404 if "会话不存在" in detail else 400, detail=detail) from exc
    return _run_response(run)


# ------------------------------------------------------------------ 运行状态 / 消息队列

@app.get("/api/runs")
async def list_runs() -> dict[str, Any]:
    """所有正在跑 / 排队中的运行 + 各会话的队列长度，前端用来画状态与徽标。"""
    return runs_manager.snapshot()


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    run = runs_manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行不存在或已过期")
    return {"run": run.summary()}


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str, from_seq: int = 0, fresh: bool = False) -> StreamingResponse:
    """从任意位置重放一次运行（切回来接着看）。"""
    run = runs_manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行不存在或已过期")
    return _run_response(run, from_seq=from_seq, fresh=fresh)


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str) -> dict[str, Any]:
    return {"ok": runs_manager.stop(run_id)}


@app.get("/api/conversations/{conv_id}/queue")
async def get_queue(conv_id: str) -> dict[str, Any]:
    run = runs_manager.active_run(conv_id)
    return {
        "items": runs_manager.queue_items(conv_id),
        "active": run.summary() if run else None,
    }


@app.post("/api/conversations/{conv_id}/queue")
async def enqueue_message(conv_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not store.get_conversation(conv_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        item = runs_manager.enqueue(
            conv_id,
            str(payload.get("content") or ""),
            attachments=payload.get("attachments") or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 会话空闲时立刻开跑，队列只用于「正忙」的情况
    if not runs_manager.active_run(conv_id):
        try:
            await runs_manager._drain(conv_id)  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
    return {"item": item, "items": runs_manager.queue_items(conv_id)}


@app.patch("/api/queue/{item_id}")
async def edit_queue_item(item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"item": runs_manager.update_item(item_id, str(payload.get("content") or ""))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/queue/{item_id}")
async def delete_queue_item(item_id: str) -> dict[str, Any]:
    try:
        runs_manager.remove_item(item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/queue/{item_id}/now")
async def queue_item_now(item_id: str) -> dict[str, Any]:
    try:
        item = runs_manager.send_now(item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@app.post("/api/conversations/{conv_id}/queue/reorder")
async def reorder_queue(conv_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    ids = [str(i) for i in (payload.get("ids") or [])]
    return {"items": runs_manager.reorder(conv_id, ids)}


# ------------------------------------------------------------------ 定时任务

@app.get("/api/schedules")
async def list_schedules() -> dict[str, Any]:
    now = time.time()
    items = []
    for schedule in store.list_schedules():
        space = None
        runs: list[dict[str, Any]] = []
        try:
            space = spaces.ensure_schedule_space(schedule)
        except Exception:  # noqa: BLE001
            space = store.get_space(schedule.get("space_id")) if schedule.get("space_id") else None
        if space:
            runs = [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "updated_at": c["updated_at"],
                    "message_count": c.get("message_count", 0),
                }
                for c in store.list_conversations(scope=space["id"])[:12]
            ]
        items.append(
            {
                **schedule,
                "summary": schedule_parse.describe(schedule),
                "next_run_at": schedule.get("next_run_at")
                or schedule_parse.next_run(schedule, now),
                "space": spaces.scope_view(space) if space else None,
                "runs": runs,
                "run_total": space.get("conversation_count", 0) if space else 0,
            }
        )
    return {
        "schedules": items,
        "kinds": [
            {"value": "once", "label": "只跑一次"},
            {"value": "daily", "label": "每天"},
            {"value": "weekly", "label": "每周"},
            {"value": "every", "label": "每隔一段时间"},
        ],
        "weekdays": schedule_parse.WEEKDAY_LABEL,
        "server_time": now,
    }


def _schedule_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """把界面的表单 / 自然语言统一成一条定时规则。"""
    fields: dict[str, Any] = {}
    when_text = str(payload.get("when") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    kind = str(payload.get("kind") or "").strip()

    if kind not in ("once", "daily", "weekly", "every"):
        if not when_text:
            raise ValueError("请填写时间规则（例如「每天 9:00」）")
        parsed = schedule_parse.parse(when_text)
        if not parsed.get("ok"):
            raise ValueError(parsed.get("hint") or "时间规则没看懂")
        kind = parsed["kind"]
        for key in ("at", "time_of_day", "weekday", "every_minutes"):
            if parsed.get(key) is not None:
                fields[key] = parsed[key]
        prompt = prompt or parsed.get("prompt") or ""

    if not prompt:
        raise ValueError("请填写任务内容：到点要让模型做什么")

    fields["kind"] = kind
    if kind == "once":
        at = payload.get("at", fields.get("at"))
        if at in (None, ""):
            raise ValueError("一次性任务需要指定执行时间")
        fields["at"] = float(at)
        if fields["at"] <= time.time():
            raise ValueError("执行时间必须是将来的时间")
    elif kind == "every":
        try:
            raw = payload.get("every_minutes") or fields.get("every_minutes") or 60
            fields["every_minutes"] = max(1, int(raw))
        except (TypeError, ValueError):
            raise ValueError("间隔分钟数不合法") from None
    else:
        text = str(payload.get("time_of_day") or fields.get("time_of_day") or "09:00")
        try:
            hour, minute = text.replace("：", ":")[:5].split(":")[:2]
            fields["time_of_day"] = f"{int(hour):02d}:{int(minute):02d}"
        except (ValueError, IndexError):
            fields["time_of_day"] = "09:00"
        if kind == "weekly":
            raw = payload.get("weekday")
            raw = fields.get("weekday") if raw is None else raw
            try:
                fields["weekday"] = max(0, min(6, int(raw or 0)))
            except (TypeError, ValueError):
                fields["weekday"] = 0

    fields["prompt"] = prompt
    fields["title"] = str(payload.get("title") or "").strip() or auto_title(prompt, 18)
    fields["conversation_id"] = payload.get("conversation_id") or None
    fields["model_id"] = payload.get("model_id") or None
    fields["enabled"] = bool(payload.get("enabled", True))
    fields["next_run_at"] = schedule_parse.next_run(fields)
    return fields


@app.post("/api/schedules")
async def create_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        fields = _schedule_fields(payload or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    schedule = store.add_schedule(**fields)
    try:
        space = spaces.ensure_schedule_space(schedule)
    except Exception:  # noqa: BLE001
        space = None
    return {
        **schedule,
        "summary": schedule_parse.describe(schedule),
        "space": spaces.scope_view(space) if space else None,
    }


@app.post("/api/schedules/parse")
async def parse_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    """一句话预览：只解析，不落库，让用户确认后再保存。"""
    parsed = schedule_parse.parse(str(payload.get("text") or ""))
    if parsed.get("ok"):
        parsed["next_run_at"] = schedule_parse.next_run(parsed)
    return parsed


@app.patch("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    schedule = store.get_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    updates = {k: v for k, v in (payload or {}).items() if k in store.SCHEDULE_FIELDS}
    merged = {**schedule, **updates}
    # 时间规则变了要重算下一次；纯开关不用动
    if any(k in updates for k in ("kind", "at", "time_of_day", "weekday", "every_minutes")):
        updates["next_run_at"] = schedule_parse.next_run(merged)
    updated = store.update_schedule(schedule_id, **updates) or merged
    if "title" in updates:
        try:
            spaces.retitle_schedule_space(schedule, updated.get("title") or "")
        except Exception:  # noqa: BLE001
            pass
    return {**updated, "summary": schedule_parse.describe(updated)}


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str) -> dict[str, Any]:
    schedule = store.get_schedule(schedule_id)
    store.delete_schedule(schedule_id)
    # 目录和历史对话保留下来，变成一个普通空间，方便回看
    space_id = (schedule or {}).get("space_id")
    if space_id and store.get_space(space_id):
        try:
            store.update_space(space_id, kind="space", icon="🗂")
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


@app.post("/api/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: str) -> dict[str, Any]:
    return await scheduler.run_now(schedule_id)


@app.post("/api/chat/stop")
async def stop_chat(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = (payload or {}).get("request_id") or ""
    return {"ok": runs_manager.stop(request_id)}


# ------------------------------------------------------------------ 静态资源

class _NoCacheStatic(StaticFiles):
    """前端改动要能立刻看到：每次都让浏览器走 ETag 协商，而不是吃本地缓存。

    文件没变时服务端返回 304，开销很小；改了 js/css 立刻生效。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/static", _NoCacheStatic(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        str(WEB_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/favicon.ico")
async def favicon() -> JSONResponse:
    return JSONResponse({"ok": True})
