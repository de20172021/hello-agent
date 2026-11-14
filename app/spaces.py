"""空间（Space）与任务（Task）：两类工作目录。

产品上的划分：

- **任务**：直接执行的对话，工作目录就是项目根目录（`BASE_DIR`）。
  不需要命名、不需要管理，问一句、跑个脚本就走，文件和代码都留在项目里。
- **空间**：有名字、有目录、有自己的一组对话。两种来源：
  - `created`：在某个父目录下**新建一个文件夹**（默认建在项目目录里）；
  - `linked`：**关联已有的本地文件夹**（不移动文件，适合代码仓库、资料目录）。

一个会话要么属于任务（`space_id IS NULL`），要么属于某个空间。切换空间 = 切换模型看到的
工作目录，文件工具、命令执行、产物清单都跟着走。
"""

from __future__ import annotations

import os
import re
import string
import time
from pathlib import Path
from typing import Any

from .settings import BASE_DIR, workspace_dir
from .store import store

TASK = "task"
SCHEDULE = "schedule"
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_DIRS = 300


# ------------------------------------------------------------------ 根目录

def task_root() -> Path:
    """任务的工作目录：项目根目录。"""
    return BASE_DIR


def space_root(space_id: str) -> Path:
    space = store.get_space(space_id)
    if not space:
        raise ValueError("空间不存在或已被移除")
    path = Path(space["path"]).expanduser()
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def root_for(space_id: str | None) -> Path:
    return space_root(space_id) if space_id else task_root()


def default_root() -> Path:
    """没有建空间时，会话都放在「默认空间」（原来的 workspace/）下面。"""
    root = workspace_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def base_root_for(conversation: dict[str, Any] | None) -> Path:
    if not conversation:
        return default_root()
    space_id = conversation.get("space_id")
    if space_id and store.get_space(space_id):
        return space_root(space_id)
    return default_root()


def workdir_for(conversation: dict[str, Any] | None) -> Path:
    """会话真正的工作目录：空间里的对话用空间目录，任务对话用自己的会话目录。"""
    base = base_root_for(conversation)
    rel = (conversation or {}).get("output_dir")
    return (base / rel) if rel else base


def root_for_conversation(conversation: dict[str, Any] | None) -> Path:
    return workdir_for(conversation)


def resolve_root(
    space_id: str | None = None, conversation_id: str | None = None
) -> tuple[Path, dict[str, Any] | None]:
    """按 `space_id` / `conversation_id` 找出本轮的工作目录。"""
    conv = None
    if conversation_id:
        conv = store.get_conversation(conversation_id)
        if conv is not None:
            return workdir_for(conv), store.get_space(conv.get("space_id"))
    space = store.get_space(space_id) if space_id else None
    if space_id and space is None:
        raise ValueError("空间不存在或已被移除")
    return root_for(space_id), space


# ------------------------------------------------------------------ 视图

def scope_view(space: dict[str, Any] | None) -> dict[str, Any]:
    """统一的「当前工作位置」描述，前后端共用一套字段。"""
    if space is None:
        root = default_root()
        return {
            "type": TASK,
            "id": None,
            "name": "任务",
            "title": "任务",
            "icon": "⚡",
            "path": str(root),
            "root": str(root),
            "mode": "task",
            "description": "每个对话在默认空间下有自己的目录（chats/），互不干扰",
            "instructions": "",
        }
    root = Path(space["path"])
    return {
        "type": "space",
        "id": space["id"],
        "name": space["name"],
        "title": space["name"],
        "icon": space.get("icon") or "📁",
        "path": str(root),
        "root": str(root),
        "mode": space.get("mode") or "created",
        "description": space.get("description") or "",
        "instructions": space.get("instructions") or "",
        "conversation_count": space.get("conversation_count", 0),
    }


def task_view() -> dict[str, Any]:
    view = scope_view(None)
    view["conversation_count"] = len(store.list_conversations(scope=TASK))
    return view


def list_view() -> dict[str, Any]:
    spaces = [
        scope_view(s) | {"pinned": s["pinned"], "kind": s.get("kind") or "space"}
        for s in store.list_spaces()
    ]
    return {
        "task": task_view(),
        "spaces": spaces,
        "project_dir": str(BASE_DIR),
        "default_parent": str(BASE_DIR),
    }


# ------------------------------------------------------------------ 创建 / 编辑 / 移除

def _slug(name: str) -> str:
    text = INVALID_CHARS.sub("-", (name or "").strip())
    text = re.sub(r"\s+", "-", text).strip(".- ")
    return (text or "space")[:48]


def _unique_dir(parent: Path, base_name: str) -> Path:
    candidate = parent / base_name
    index = 2
    while candidate.exists():
        candidate = parent / f"{base_name}-{index}"
        index += 1
    return candidate


def _norm(path: str | Path) -> Path:
    raw = Path(str(path).strip().strip('"')).expanduser()
    if not raw.is_absolute():
        raw = BASE_DIR / raw
    return raw.resolve()


def _same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))
    except OSError:  # pragma: no cover
        return str(a) == str(b)


def create(
    name: str,
    *,
    mode: str = "created",
    parent: str | None = None,
    path: str | None = None,
    icon: str = "",
    description: str = "",
    instructions: str = "",
) -> dict[str, Any]:
    """新建空间：`created` 建文件夹，`linked` 关联已有目录。"""
    title = (name or "").strip() or "新空间"
    mode = "linked" if str(mode).lower() in ("link", "linked", "existing") else "created"

    if mode == "linked":
        if not path:
            raise ValueError("请选择要关联的本地文件夹")
        target = _norm(path)
        if not target.exists():
            raise ValueError(f"目录不存在：{target}")
        if not target.is_dir():
            raise ValueError(f"这不是文件夹：{target}")
    else:
        base = _norm(parent) if parent else BASE_DIR
        if not base.exists():
            raise ValueError(f"父目录不存在：{base}")
        if not base.is_dir():
            raise ValueError(f"父目录不是文件夹：{base}")
        target = _unique_dir(base, _slug(title))
        target.mkdir(parents=True, exist_ok=False)

    if _same_path(target, BASE_DIR):
        raise ValueError("项目目录本身就是「任务」的工作目录，不用再建成空间")
    for existing in store.list_spaces(include_archived=True):
        if _same_path(_norm(existing["path"]), target):
            raise ValueError(f"这个目录已经是空间「{existing['name']}」了")

    return store.add_space(
        name=title,
        path=str(target),
        mode=mode,
        icon=icon or ("📁" if mode == "created" else "🔗"),
        description=description,
        instructions=instructions,
    )


def update(space_id: str, **fields: Any) -> dict[str, Any]:
    space = store.get_space(space_id)
    if not space:
        raise ValueError("空间不存在或已被移除")
    if "path" in fields and fields["path"]:
        target = _norm(fields["path"])
        if not target.is_dir():
            raise ValueError(f"目录不存在：{target}")
        for existing in store.list_spaces(include_archived=True):
            if existing["id"] != space_id and _same_path(_norm(existing["path"]), target):
                raise ValueError(f"这个目录已经是空间「{existing['name']}」了")
        fields["path"] = str(target)
    if "name" in fields:
        fields["name"] = str(fields["name"]).strip() or space["name"]
    return store.update_space(space_id, **fields)


def remove(space_id: str, *, delete_conversations: bool = False) -> dict[str, Any]:
    """移除空间。只移除登记，不动磁盘上的文件。"""
    if not store.get_space(space_id):
        raise ValueError("空间不存在或已被移除")
    return store.delete_space(space_id, delete_conversations=delete_conversations)


# ------------------------------------------------------------------ 目录浏览

def _places() -> list[dict[str, str]]:
    home = Path.home()
    candidates = [
        ("项目目录", BASE_DIR),
        ("主目录", home),
        ("桌面", home / "Desktop"),
        ("下载", home / "Downloads"),
        ("文档", home / "Documents"),
    ]
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for label, path in candidates:
        try:
            if not path.exists() or not path.is_dir():
                continue
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            out.append({"label": label, "path": str(path)})
        except OSError:
            continue
    return out


def _drives() -> list[str]:
    if os.name != "nt":
        return ["/"]
    return [f"{letter}:\\" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]


def browse(path: str | None = None) -> dict[str, Any]:
    """给「选择文件夹」用的目录浏览：列出某个目录下的子目录。"""
    places = _places()
    if not path:
        return {
            "path": "",
            "parent": None,
            "dirs": [],
            "places": places,
            "drives": _drives(),
            "home": str(Path.home()),
            "project_dir": str(BASE_DIR),
        }
    target = _norm(path)
    if not target.exists():
        raise ValueError(f"目录不存在：{target}")
    if not target.is_dir():
        raise ValueError(f"这不是文件夹：{target}")
    dirs: list[dict[str, Any]] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except PermissionError as exc:
        raise ValueError(f"没有权限读取：{target}") from exc
    for child in children:
        if len(dirs) >= MAX_DIRS:
            break
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        dirs.append({"name": child.name, "path": str(child), "hidden": child.name.startswith(".")})
    parent = target.parent if target.parent != target else None
    return {
        "path": str(target),
        "parent": str(parent) if parent else None,
        "dirs": dirs,
        "places": places,
        "drives": _drives(),
        "home": str(Path.home()),
        "project_dir": str(BASE_DIR),
    }


def reveal(path: str | None = None) -> dict[str, Any]:
    target = _norm(path) if path else task_root()
    try:
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys_platform() == "darwin":
            os.system(f'open "{target}"')
        else:
            os.system(f'xdg-open "{target}"')
        return {"ok": True, "path": str(target)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": str(target), "detail": str(exc)}


def sys_platform() -> str:
    import sys

    return sys.platform


# ------------------------------------------------------------------ 迁移

def ensure_seed() -> None:
    """老版本只有「一个全局工作区」：把它登记成空间，历史对话跟着搬过去。

    新装用户不建空间——默认就是「任务」，空间由用户自己创建。
    """
    if store.list_spaces(include_archived=True):
        return
    conversations = store.list_conversations()
    if not conversations:
        return
    legacy = workspace_dir()
    legacy.mkdir(parents=True, exist_ok=True)
    space = store.add_space(
        name="默认空间",
        path=str(legacy),
        mode="created" if BASE_DIR in legacy.parents else "linked",
        icon="📦",
        description="从旧版「工作区」迁移过来的历史对话",
        instructions="",
    )
    moved = store.assign_conversations_to_space(space["id"])
    print(f"  空间迁移: 已把 {moved} 个历史对话放入「{space['name']}」({legacy})")


# ------------------------------------------------------------------ 产物目录

def output_dir_for(root: Path, title: str, conv_id: str) -> str:
    """（兼容旧调用）在 root 下建 outputs/<名字>，给定时任务以外的场景用。"""
    """给一个会话分配产物目录（相对工作目录），并建好。"""
    slug = _slug(title or "chat")[:24] or "chat"
    rel = f"outputs/{slug}-{conv_id[-6:]}"
    try:
        (Path(root) / rel).mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    return rel


def ensure_conversation_dir(conversation: dict[str, Any] | None) -> str | None:
    """确保会话有自己的目录，返回相对「所属根目录」的路径。

    - 任务（没有空间）：在默认空间下开 `chats/<会话名>-<id>`，就是它的工作目录；
    - 空间里的对话：共用空间目录（空间本来就是有组织的目录），不套子文件夹；
    - 关联的真实目录：原样使用，绝不额外造目录；
    - 定时任务的执行对话：由调度器显式给 `runs/<时间戳>`。
    """
    if not conversation:
        return None
    rel = conversation.get("output_dir")
    if rel:
        try:
            workdir_for(conversation).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return rel
    space_id = conversation.get("space_id")
    space = store.get_space(space_id) if space_id else None
    if space:
        return None
    slug = _slug(conversation.get("title") or "会话")[:24] or "会话"
    rel = f"chats/{slug}-{conversation['id'][-6:]}"
    try:
        (default_root() / rel).mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    store.update_conversation(conversation["id"], output_dir=rel)
    conversation["output_dir"] = rel
    return rel


def run_output_dir(root: Path, when: float | None = None) -> str:
    """定时任务每次执行的产物目录：`runs/2026-11-14_0900`。"""
    stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(when or time.time()))
    base = f"runs/{stamp}"
    target = Path(root) / base
    index = 2
    while target.exists():
        target = Path(root) / f"{base}-{index}"
        index += 1
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    return target.relative_to(root).as_posix()


# ------------------------------------------------------------------ 定时任务目录

def schedule_root() -> Path:
    root = BASE_DIR / "schedules"
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_schedule_space(schedule: dict[str, Any]) -> dict[str, Any]:
    """每个定时任务对应一个目录（空间），名字就是任务名。"""
    space_id = schedule.get("space_id")
    if space_id:
        space = store.get_space(space_id)
        if space:
            _adopt_legacy_run(schedule, space)
            return space
    name = (schedule.get("title") or schedule.get("prompt") or "定时任务").strip() or "定时任务"
    parent = schedule_root()
    target = _unique_dir(parent, _slug(name))
    target.mkdir(parents=True, exist_ok=True)
    space = store.add_space(
        name=name,
        path=str(target),
        mode="created",
        kind=SCHEDULE,
        icon="⏰",
        description=f"定时任务「{name}」的专属目录：每次执行都会在这里新建一个对话",
    )
    store.update_schedule(schedule["id"], space_id=space["id"])
    schedule["space_id"] = space["id"]
    _adopt_legacy_run(schedule, space)
    return space


def _adopt_legacy_run(schedule: dict[str, Any], space: dict[str, Any]) -> None:
    """老版本的任务会话建在「任务」里，搬进任务目录，历史记录不丢。"""
    conv_id = schedule.get("conversation_id")
    if not conv_id:
        return
    try:
        conv = store.get_conversation(conv_id)
        if conv and not conv.get("space_id"):
            store.update_conversation(conv_id, space_id=space["id"])
    except Exception:  # noqa: BLE001
        pass


def retitle_schedule_space(schedule: dict[str, Any], new_title: str) -> None:
    """任务改名时同步空间名；目录还是默认位置的话，顺手把文件夹也改了。"""
    space_id = schedule.get("space_id")
    if not space_id:
        return
    space = store.get_space(space_id)
    if not space:
        return
    new_title = (new_title or "").strip()
    if not new_title or new_title == space["name"]:
        return
    old = Path(space["path"])
    fields: dict[str, Any] = {"name": new_title}
    try:
        if old.parent.resolve() == schedule_root().resolve():
            target = old.parent / _slug(new_title)
            if not target.exists():
                old.rename(target)
                fields["path"] = str(target)
    except OSError:
        pass
    store.update_space(space_id, **fields)
