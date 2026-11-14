"""配置管理：读写 data/config.json，提供默认模型档案、路径约定与采样参数。

产品里所有「位置固定」的目录都在这里集中定义，并会被写进系统提示词，
这样模型自己也知道技能、MCP、工作区、记忆分别放在哪。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "chat.db"

# ---- 固定位置（会展示在系统提示词里，不要随意改动）----
SKILLS_DIR = BASE_DIR / "skills"
WORKSPACE_DIR = BASE_DIR / "workspace"
MEMORY_DIR = DATA_DIR / "memory"
MEMORY_PATH = MEMORY_DIR / "MEMORY.md"
MCP_CONFIG_PATH = DATA_DIR / "mcp.json"

APP_NAME = "HelloAgent"
APP_TAGLINE = "本地智能体工作台"

DEFAULT_SYSTEM_PROMPT = (
    "你是 HelloAgent，一个完全运行在用户本机的 AI 智能体。"
    "回答要准确、具体、结构清晰，默认使用简体中文和 Markdown 排版。"
    "当问题涉及实时信息、精确计算或本地文件时，先调用工具拿到事实再作答，不要凭空编造。"
)

THINKING_LEVELS = ("off", "low", "medium", "high")

# 工具调用轮数配置的上下限（前端滑块/输入框也用它）
MAX_ROUNDS_LIMIT = 200
DEFAULT_MAX_ROUNDS = 50
THINKING_BUDGET = {"low": 320, "medium": 900, "high": 2400}

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 3,
    "active_model_id": "local-qwen3",
    # 连接 = 一个服务端点（base_url + api_key）。模型只引用 connection_id，
    # 同一个连接下可以挂任意多个模型，换模型不用重填地址和密钥。
    "connections": [],
    "models": [
        {
            "id": "local-qwen3",
            "label": "Qwen3-0.6B · 本地",
            "provider": "local_hf",
            "model_path": "models/Qwen/Qwen3-0___6B",
            "device": "auto",
            "dtype": "auto",
            "enable_thinking": False,
            "context_window": 40960,
            "load_on_start": True,
        },
        {
            "id": "lmstudio",
            "label": "LM Studio · local-model",
            "provider": "openai_compat",
            "base_url": "http://127.0.0.1:1234/v1",
            "model": "local-model",
            "api_key": "",
            "context_window": 32768,
        },
        {
            "id": "ollama",
            "label": "Ollama · qwen3:0.6b",
            "provider": "openai_compat",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:0.6b",
            "api_key": "",
            "context_window": 32768,
        },
    ],
    "defaults": {
        "temperature": 0.6,
        "top_p": 0.95,
        "max_tokens": 8192,
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "thinking": False,
        "thinking_level": "medium",
        "auto_compact": True,
        # 压缩触发方式：
        #   tokens —— 累计提示词超过 compact_tokens 才主动压缩（默认 50 万，基本不压）；
        #   ratio  —— 按上下文窗口的占比压缩（旧的 0.75 逻辑）。
        # 两种方式都不影响「模型报上下文超长时自动压缩并重试」这条兜底。
        "compact_mode": "tokens",
        "compact_tokens": 500_000,
        "compact_threshold": 0.75,
        "compact_keep_recent": 8,
        "compact_on_overflow": True,
        "context_window": 32768,
    },
    "tools": {
        "enabled": True,
        "disabled": [],
        # 单次提问内最多允许的「模型输出 + 工具调用」循环轮数。
        # 长任务（读代码、批量改文件、跑测试）动辄几十轮，默认给足 50 轮；
        # 可在「设置 → 偏好」里调，1~200。
        "max_rounds": DEFAULT_MAX_ROUNDS,
        "max_rounds_limit": MAX_ROUNDS_LIMIT,
        "allow_shell": True,
        "shell_timeout": 30,
    },
    "workspace": {
        "path": "workspace",
        "max_read_bytes": 200000,
        "max_tree_entries": 400,
    },
    "skills": {
        "disabled": [],
    },
    # 模型库：从 ModelScope / HuggingFace 下载权重时用
    "hub": {
        "source": "modelscope",
        "cache_dir": "models/.cache",
        # HuggingFace 源走镜像（国内直连经常被代理拦成 502），留空则用官方地址
        "hf_endpoint": "https://hf-mirror.com",
    },
    "memory": {
        "enabled": True,
        "max_chars": 6000,
    },
    "mcp": {
        "auto_connect": True,
        # 是否把所有 MCP 工具直接内联进提示词。金融/浏览器类 MCP 动辄上百个工具，
        # 内联后提示词能到 2 万 token（CPU 上光读入就要三分钟），默认改为按需发现。
        "inline_tools": False,
    },
    "perf": {
        # 复用已 prefill 过的提示词前缀（系统提示 + 工具 schema + 历史）
        "prefix_cache": True,
        # 缓存占用的 token 额度，KV 约 224KB/token（Qwen3-0.6B），4096 约 900MB
        "cache_tokens": 4096,
        "cache_entries": 2,
        # INT8 动态量化：CPU 解码受内存带宽限制时提速 1.2~1.7 倍
        "int8": False,
        # 0 = 交给 torch 自己决定
        "threads": 0,
        # 启动后在后台把「系统提示 + 工具 schema」这段固定前缀先 prefill 好
        "warm_prefix": True,
        # OpenAI 兼容端点的最大并行生成数（本地 transformers 后端固定为 1）
        "parallel_runs": 2,
    },
    "ui": {
        "theme": "light",
        "workspace_open": False,
    },
}

_LOCK = threading.RLock()
_cache: dict[str, Any] | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _normalize(cfg: dict[str, Any]) -> dict[str, Any]:
    """补齐缺失字段，保证老配置文件也能用。"""
    cfg = _deep_merge(DEFAULT_CONFIG, cfg)
    cfg["version"] = DEFAULT_CONFIG["version"]
    models = [m for m in cfg.get("models", []) if isinstance(m, dict) and m.get("id")]
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for m in models:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        m.setdefault("label", m["id"])
        m.setdefault("provider", "openai_compat")
        # 多模态：auto = OpenAI 兼容端点先按能看图发，被拒就自动降级成文字；
        # on / off 由用户明确指定（本地纯文本模型建议 off）
        vision = str(m.get("vision") or "auto").lower()
        m["vision"] = vision if vision in ("auto", "on", "off") else "auto"
        try:
            m["context_window"] = int(_clamp(m.get("context_window", 32768), 1024, 2_000_000, 32768))
        except (TypeError, ValueError):
            m["context_window"] = 32768
        cleaned.append(m)
    cfg["models"] = cleaned or copy.deepcopy(DEFAULT_CONFIG["models"])
    _normalize_connections(cfg)

    ids = {m["id"] for m in cfg["models"]}
    if cfg.get("active_model_id") not in ids:
        cfg["active_model_id"] = cfg["models"][0]["id"]

    defaults = cfg.setdefault("defaults", {})
    defaults["temperature"] = _clamp(defaults.get("temperature", 0.6), 0.0, 2.0, 0.6)
    defaults["top_p"] = _clamp(defaults.get("top_p", 0.95), 0.05, 1.0, 0.95)
    defaults["max_tokens"] = int(_clamp(defaults.get("max_tokens", 8192), 16, 32768, 8192))
    if not isinstance(defaults.get("system_prompt"), str):
        defaults["system_prompt"] = DEFAULT_SYSTEM_PROMPT
    defaults["thinking"] = bool(defaults.get("thinking", False))
    level = str(defaults.get("thinking_level") or "medium").lower()
    defaults["thinking_level"] = level if level in THINKING_LEVELS else "medium"
    defaults["auto_compact"] = bool(defaults.get("auto_compact", True))
    mode = str(defaults.get("compact_mode") or "tokens").lower()
    defaults["compact_mode"] = mode if mode in ("tokens", "ratio") else "tokens"
    defaults["compact_tokens"] = int(
        _clamp(defaults.get("compact_tokens", 500_000), 2000, 5_000_000, 500_000)
    )
    defaults["compact_threshold"] = _clamp(defaults.get("compact_threshold", 0.75), 0.3, 0.95, 0.75)
    defaults["compact_on_overflow"] = bool(defaults.get("compact_on_overflow", True))
    defaults["compact_keep_recent"] = int(
        _clamp(defaults.get("compact_keep_recent", 8), 2, 40, 8)
    )
    defaults["context_window"] = int(
        _clamp(defaults.get("context_window", 32768), 1024, 2_000_000, 32768)
    )

    tools = cfg.setdefault("tools", {})
    tools["enabled"] = bool(tools.get("enabled", True))
    tools["disabled"] = [str(x) for x in (tools.get("disabled") or [])]
    tools["max_rounds"] = int(
        _clamp(tools.get("max_rounds", DEFAULT_MAX_ROUNDS), 1, MAX_ROUNDS_LIMIT, DEFAULT_MAX_ROUNDS)
    )
    tools["max_rounds_limit"] = MAX_ROUNDS_LIMIT
    tools["allow_shell"] = bool(tools.get("allow_shell", True))
    tools["shell_timeout"] = int(_clamp(tools.get("shell_timeout", 30), 5, 300, 30))

    workspace = cfg.setdefault("workspace", {})
    workspace["path"] = str(workspace.get("path") or "workspace")
    workspace["max_read_bytes"] = int(
        _clamp(workspace.get("max_read_bytes", 200000), 2000, 5_000_000, 200000)
    )
    workspace["max_tree_entries"] = int(
        _clamp(workspace.get("max_tree_entries", 400), 20, 5000, 400)
    )

    skills = cfg.setdefault("skills", {})
    skills["disabled"] = [str(x) for x in (skills.get("disabled") or [])]

    hub = cfg.setdefault("hub", {})
    source = str(hub.get("source") or "modelscope").lower()
    hub["source"] = source if source in ("modelscope", "huggingface") else "modelscope"
    hub["cache_dir"] = str(hub.get("cache_dir") or "models/.cache")
    hub["hf_endpoint"] = str(hub.get("hf_endpoint") or "")

    memory = cfg.setdefault("memory", {})
    memory["enabled"] = bool(memory.get("enabled", True))
    memory["max_chars"] = int(_clamp(memory.get("max_chars", 6000), 200, 40000, 6000))

    mcp_cfg = cfg.setdefault("mcp", {})
    mcp_cfg["auto_connect"] = bool(mcp_cfg.get("auto_connect", True))
    mcp_cfg["inline_tools"] = bool(mcp_cfg.get("inline_tools", False))

    perf = cfg.setdefault("perf", {})
    perf["prefix_cache"] = bool(perf.get("prefix_cache", True))
    perf["cache_tokens"] = int(_clamp(perf.get("cache_tokens", 4096), 256, 32768, 4096))
    perf["cache_entries"] = int(_clamp(perf.get("cache_entries", 2), 1, 8, 2))
    perf["int8"] = bool(perf.get("int8", False))
    perf["threads"] = int(_clamp(perf.get("threads", 0), 0, 128, 0))
    perf["warm_prefix"] = bool(perf.get("warm_prefix", True))

    return cfg


def _host_label(base_url: str) -> str:
    """从服务地址里取个像样的默认连接名。"""
    try:
        host = urlparse(str(base_url or "")).hostname or ""
    except ValueError:
        host = ""
    return host or "新连接"


def _next_conn_id(taken: dict[str, Any]) -> str:
    n = 1
    while f"c{n}" in taken:
        n += 1
    return f"c{n}"


def _normalize_connections(cfg: dict[str, Any]) -> None:
    """连接与模型解耦。

    连接保存 base_url / api_key，模型只保留 connection_id + model 名。
    为了让推理后端不用关心这层结构，这里把连接上的地址和密钥回写到每个模型上
    （模型是连接的投影，改连接即改它下面所有模型）。

    老配置（version <= 2）里每个模型自包含 base_url/api_key，会按地址自动合并成连接。
    """
    conns: list[dict[str, Any]] = []
    for raw in cfg.get("connections") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        base_url = str(raw.get("base_url") or "").strip()
        conns.append(
            {
                "id": str(raw["id"]),
                "label": str(raw.get("label") or _host_label(base_url)),
                "base_url": base_url,
                "api_key": str(raw.get("api_key") or ""),
            }
        )
    by_id = {c["id"]: c for c in conns}
    by_url = {c["base_url"].rstrip("/"): c for c in conns if c["base_url"]}

    remote = [m for m in cfg["models"] if m.get("provider") == "openai_compat"]
    for model in cfg["models"]:
        if model.get("provider") != "openai_compat":
            model.pop("connection_id", None)
            continue
        base_url = str(model.get("base_url") or "").strip()
        conn = by_id.get(str(model.get("connection_id") or ""))
        if conn is None:
            conn = by_url.get(base_url.rstrip("/")) if base_url else None
        if conn is None:
            if not base_url:
                # 没有地址也没连接的孤儿模型：留着但标记为空，前端会提示补配置
                model["connection_id"] = ""
                model["base_url"] = ""
                model["api_key"] = ""
                continue
            same = [m for m in remote if str(m.get("base_url") or "").strip().rstrip("/") == base_url.rstrip("/")]
            label = str(model.get("label") or "") if len(same) == 1 else _host_label(base_url)
            conn = {
                "id": _next_conn_id(by_id),
                "label": label or _host_label(base_url),
                "base_url": base_url,
                "api_key": str(model.get("api_key") or ""),
            }
            conns.append(conn)
            by_id[conn["id"]] = conn
            by_url[base_url.rstrip("/")] = conn
        model["connection_id"] = conn["id"]
        model["base_url"] = conn["base_url"]
        model["api_key"] = conn["api_key"]
        # 老配置里模型名常常就是连接名（比如都叫「阿里」），列表里会分不清哪个模型，
        # 这种情况直接换成真实模型名，用户还能自己再改。
        if str(model.get("label") or "") == conn["label"] and model.get("model"):
            model["label"] = str(model["model"])

    cfg["connections"] = conns


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, num))


def load_config() -> dict[str, Any]:
    global _cache
    with _LOCK:
        if _cache is not None:
            return _cache

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
        _cache = _normalize(raw)
        if not CONFIG_PATH.exists():
            _write(_cache)
        return _cache


def _write(cfg: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """保存并返回规范化后的配置。"""
    global _cache
    with _LOCK:
        normalized = _normalize(cfg)
        _write(normalized)
        _cache = normalized
        return normalized


def public_config() -> dict[str, Any]:
    """给前端的配置视图，api_key 做脱敏处理。"""
    cfg = copy.deepcopy(load_config())
    for conn in cfg.get("connections") or []:
        key = conn.get("api_key") or ""
        conn["has_api_key"] = bool(key)
        conn["api_key"] = ""
    for model in cfg["models"]:
        key = model.get("api_key") or ""
        model["has_api_key"] = bool(key)
        model["api_key"] = ""
    return cfg


def get_connection(connection_id: str | None) -> dict[str, Any] | None:
    for conn in load_config().get("connections") or []:
        if conn.get("id") == connection_id:
            return conn
    return None


def get_model_profile(model_id: str | None) -> dict[str, Any] | None:
    cfg = load_config()
    target = model_id or cfg["active_model_id"]
    for model in cfg["models"]:
        if model["id"] == target:
            return model
    return None


def resolve_path(path: str | None) -> Path:
    """把配置里的相对路径解析成绝对路径。"""
    raw = Path(str(path or "").strip().strip('"'))
    if not raw.is_absolute():
        raw = (BASE_DIR / raw).resolve()
    return raw


def model_path_of(profile: dict[str, Any]) -> Path:
    return resolve_path(profile.get("model_path"))


def workspace_dir() -> Path:
    path = resolve_path(load_config().get("workspace", {}).get("path") or "workspace")
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_dirs() -> None:
    """启动时把产品用到的固定目录都建好。"""
    for path in (DATA_DIR, SKILLS_DIR, WORKSPACE_DIR, MEMORY_DIR):
        path.mkdir(parents=True, exist_ok=True)
    if not MCP_CONFIG_PATH.exists():
        MCP_CONFIG_PATH.write_text(
            json.dumps({"servers": {}}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if not MEMORY_PATH.exists():
        MEMORY_PATH.write_text(_default_memory(), encoding="utf-8")
    readme = WORKSPACE_DIR / "README.md"
    if not readme.exists():
        readme.write_text(_workspace_readme(), encoding="utf-8")


def _workspace_readme() -> str:
    return (
        "# 工作区\n\n"
        "这是模型和你的共享目录：模型读文件、写文件、跑脚本都在这里进行。\n\n"
        "- 在界面右侧「工作区」面板里可以浏览、预览这里的所有文件\n"
        "- 会话里生成的文件（报告、图表、脚本）会出现在这里，点击即可预览\n"
        "- 想换成别的目录（比如某个代码仓库），在「设置 → 工作区」里改路径即可\n\n"
        "> 出于安全考虑，模型只能访问这个目录内部的路径，越界会被拒绝。\n"
    )


def _default_memory() -> str:
    return (
        "# HelloAgent 长期记忆\n\n"
        "> 这里保存跨会话需要记住的信息，会被自动注入系统提示词。\n\n"
        "## 用户偏好\n\n"
        "## 项目约定\n\n"
        "## 事实与决定\n"
    )


def paths_info() -> dict[str, str]:
    """给系统提示词和前端用的路径清单。"""
    return {
        "workspace": str(workspace_dir()),
        "skills": str(SKILLS_DIR),
        "mcp": str(MCP_CONFIG_PATH),
        "memory": str(MEMORY_PATH),
        "data": str(DATA_DIR),
        "project": str(BASE_DIR),
    }
