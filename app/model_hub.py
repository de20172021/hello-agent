"""模型库下载：把 ModelScope / HuggingFace 上的权重拉到项目 models/ 下。

设计要点：
- 目标目录固定成 `models/<org>--<name>`，不让用户手填路径，也避免出现嵌套目录；
- 缓存目录默认落在项目内 `models/.cache`，不往 C 盘用户目录写；
- 下载在后台线程跑，进度靠扫描目标目录 + 预取文件总大小估算；
- 下载完成后自动在 config 里登记一个 local_hf 模型档案，前端刷新即可选中。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .settings import BASE_DIR, load_config, save_config

MODELS_DIR = BASE_DIR / "models"
DEFAULT_CACHE_DIR = "models/.cache"

# 常用的小体积模型，前端做成输入提示
KNOWN_REPOS = [
    "Qwen/Qwen3-0.6B",
    "Qwen/Qwen3-1.7B",
    "Qwen/Qwen3-4B",
    "Qwen/Qwen2.5-0.5B-Instruct",
    "Qwen/Qwen2.5-1.5B-Instruct",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B",
]

_TASKS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def hub_config() -> dict[str, str]:
    hub = load_config().get("hub") or {}
    source = str(hub.get("source") or "modelscope").lower()
    if source not in ("modelscope", "huggingface"):
        source = "modelscope"
    return {
        "source": source,
        "cache_dir": str(hub.get("cache_dir") or DEFAULT_CACHE_DIR),
        "hf_endpoint": str(hub.get("hf_endpoint") or ""),
    }


def models_root() -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return MODELS_DIR


def cache_path() -> Path:
    raw = Path(hub_config()["cache_dir"]).expanduser()
    path = raw if raw.is_absolute() else (BASE_DIR / raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def repo_dirname(repo_id: str) -> str:
    return str(repo_id or "").strip().strip("/").replace("/", "--")


def target_dir(repo_id: str) -> Path:
    return models_root() / repo_dirname(repo_id)


def _apply_cache_env(cache: Path, endpoint: str = "") -> None:
    """把两个库的缓存都指到项目内，避免污染 C:\\Users\\<name>\\.cache。"""
    os.environ["HF_HOME"] = str(cache / "huggingface")
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(cache / "huggingface" / "hub")
    os.environ["HUB_CACHE"] = str(cache / "huggingface" / "hub")
    os.environ["MODELSCOPE_CACHE"] = str(cache / "modelscope")
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    if endpoint:
        # 国内直连 huggingface.co 经常被代理拦成 502，换镜像更稳
        os.environ["HF_ENDPOINT"] = endpoint.rstrip("/")


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def remote_total(repo_id: str, source: str) -> int:
    """尽量问一下模型库总大小，问不到就返回 0（前端只显示已下载量）。"""
    try:
        if source == "modelscope":
            from modelscope.hub.api import HubApi

            files = HubApi().get_model_files(repo_id, recursive=True)
            return sum(int(f.get("Size") or 0) for f in files if f.get("Type") == "blob")
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo_id, files_metadata=True)
        return sum(int(s.size or 0) for s in (info.siblings or []))
    except Exception:  # noqa: BLE001
        return 0


def _public(task: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in task.items() if not k.startswith("_")}


def get_task(task_id: str) -> dict[str, Any]:
    task = _TASKS.get(task_id)
    return _public(task) if task else {}


def active_tasks() -> list[dict[str, Any]]:
    with _LOCK:
        return [_public(t) for t in _TASKS.values() if t["state"] == "downloading"]


def start(repo_id: str, source: str = "") -> dict[str, Any]:
    repo = str(repo_id or "").strip().strip("/")
    if not repo or repo.count("/") != 1:
        raise ValueError("模型 ID 需要形如 Qwen/Qwen3-0.6B")
    src = str(source or hub_config()["source"]).lower()
    if src not in ("modelscope", "huggingface"):
        src = "modelscope"

    with _LOCK:
        for task in _TASKS.values():
            if task["repo_id"] == repo and task["state"] == "downloading":
                return _public(task)
        task_id = uuid.uuid4().hex[:8]
        target = target_dir(repo)
        task: dict[str, Any] = {
            "id": task_id,
            "repo_id": repo,
            "source": src,
            "state": "downloading",
            "detail": "连接模型库…",
            "bytes": 0,
            "total": 0,
            "speed": 0.0,
            "target": str(target.relative_to(BASE_DIR)).replace("\\", "/"),
            "model_id": "",
            "error": "",
            "started_at": time.time(),
            "_stop": threading.Event(),
        }
        _TASKS[task_id] = task

    threading.Thread(target=_run, args=(task_id,), daemon=True).start()
    return _public(task)


def cancel(task_id: str) -> dict[str, Any]:
    task = _TASKS.get(task_id)
    if not task:
        return {}
    task["_stop"].set()
    if task["state"] == "downloading":
        task["state"] = "canceled"
        task["detail"] = "已取消（当前文件下完后才会停）"
    return _public(task)


def _watch(task: dict[str, Any], target: Path, stop: threading.Event) -> None:
    last = dir_size(target)
    last_at = time.time()
    while not stop.wait(0.7):
        cur = dir_size(target)
        now = time.time()
        dt = max(0.001, now - last_at)
        inst = max(0.0, (cur - last) / dt)
        task["speed"] = round(task["speed"] * 0.55 + inst * 0.45, 1) if inst else 0.0
        task["bytes"] = cur
        last, last_at = cur, now
    task["bytes"] = dir_size(target)


def _fetch(repo: str, source: str, target: Path, cache: Path) -> Path:
    if source == "modelscope":
        from modelscope import snapshot_download

        path = snapshot_download(
            repo, local_dir=str(target), cache_dir=str(cache / "modelscope")
        )
        return Path(path)

    from huggingface_hub import snapshot_download

    try:
        path = snapshot_download(
            repo, local_dir=str(target), cache_dir=str(cache / "huggingface")
        )
    except TypeError:
        # 老版本 hub 需要显式关掉符号链接
        path = snapshot_download(
            repo,
            local_dir=str(target),
            cache_dir=str(cache / "huggingface"),
            local_dir_use_symlinks=False,
        )
    return Path(path)


def _run(task_id: str) -> None:
    task = _TASKS[task_id]
    repo, source = task["repo_id"], task["source"]
    target = target_dir(repo)
    stop: threading.Event = task["_stop"]
    try:
        target.mkdir(parents=True, exist_ok=True)
        cache = cache_path()
        cfg = hub_config()
        _apply_cache_env(cache, cfg["hf_endpoint"] if source == "huggingface" else "")
        task["detail"] = f"正在从 {source} 下载…"
        task["total"] = remote_total(repo, source)
        threading.Thread(target=_watch, args=(task, target, stop), daemon=True).start()

        path = _fetch(repo, source, target, cache)
        stop.set()
        if task["state"] == "canceled":
            return
        task["bytes"] = dir_size(target)
        task["model_id"] = _register(repo, path)
        task["state"] = "done"
        task["detail"] = "下载完成，已加入模型列表"
    except Exception as exc:  # noqa: BLE001
        stop.set()
        # 失败时别留一个空目录，免得模型列表里出现打不开的档案
        try:
            if target.exists() and not any(target.iterdir()):
                target.rmdir()
        except OSError:
            pass
        if task["state"] == "canceled":
            return
        task["state"] = "error"
        detail = f"{type(exc).__name__}: {exc}"
        if "ProxyError" in detail or "502" in detail:
            detail += "（网络代理拦截，试试切到 ModelScope 源）"
        task["error"] = detail
        task["detail"] = "下载失败"


def _register(repo: str, path: Path) -> str:
    """下载完成后自动登记一个模型档案，id 保持稳定可复用。"""
    target = target_dir(repo)
    real = Path(path or target)
    try:
        rel = str(real.resolve().relative_to(BASE_DIR)).replace("\\", "/")
    except ValueError:
        rel = str(real).replace("\\", "/")

    cfg = load_config()
    for model in cfg["models"]:
        if model.get("provider") != "local_hf":
            continue
        if str(model.get("model_path") or "").replace("\\", "/").lstrip("./") == rel.lstrip("./"):
            return model["id"]

    base_id = f"local-{repo.split('/')[-1].lower()}"
    taken = {m["id"] for m in cfg["models"]}
    model_id, n = base_id, 2
    while model_id in taken:
        model_id, n = f"{base_id}-{n}", n + 1

    cfg["models"].append(
        {
            "id": model_id,
            "label": repo.split("/")[-1],
            "provider": "local_hf",
            "model_path": rel,
            "device": "auto",
            "dtype": "auto",
            "enable_thinking": False,
            "load_on_start": False,
            "vision": "auto",
            "context_window": 32768,
        }
    )
    save_config(cfg)
    return model_id
