"""后台运行管理：一次生成 = 一个 Run，与 HTTP 连接彻底解耦。

为什么需要这一层：

- 以前的实现里，SSE 连接就是生成本身。前端一切走会话（`openConversation`）
  或者刷新页面，连接断掉，`asyncio.CancelledError` 就把生成取消了 ——
  所以「正在生成时不能切会话」。
- 现在生成跑在独立的后台 task 里，事件带自增 `seq` 缓存在内存，
  前端随时可以断开（切会话 / 关页面），回来用 `from=seq` 重放接着看。
- 会话级消息队列：模型正忙时用户继续发的消息进队列，
  当前这轮结束自动接着跑，队列可排序 / 编辑 / 删除 / 立即插队。
- 并发用「槽位」控制：本地 transformers 后端只能一次算一个（单模型、单 KV 缓存），
  强行并发只会互相阻塞；OpenAI 兼容端点可以按 `perf.parallel_runs` 并行。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import agent, filechanges, multimodal, spaces
from . import skills as skills_lib
from .llm import RunHandle
from .settings import load_config
from .store import new_id, store

TERMINAL = ("done", "stopped", "error")

# 会话标题的「客套话前缀」：循环剥掉，让标题落在真正的问题上
_INTRO_RE = re.compile(
    r"^(?:你好|您好|hi|hello|hey|嗨|哈喽|在吗|请|请问|麻烦|劳烦|帮忙|帮我|帮我一下|"
    r"能不能|可不可以|我想|我要|我需要|我打算|我准备|我想问|你想|你帮我|"
    r"你能不能|你可以|请帮我|帮我请|问一下|问下|请教|求助|跪求|急|"
    r"帮我看看|帮我写|帮我做|给|替我)[，,、：:！!。\s]+",
    re.I,
)
# 紧贴动词的前缀（后面没有标点也能剥）：请帮我写… / 能帮我看看… / 我想要一个…
_INTRO_LOOSE_RE = re.compile(
    r"^(?:请帮我|请你|麻烦你|劳烦您|能帮我|我想要|我想让|我想请|"
    r"你好|您好|请问|帮我|能不能|可不可以|我想|我要|我需要|问一下|问下|请教|在吗)",
    re.I,
)
# 序号前缀：1、 (1) ① 一、 * - 等
_NUM_RE = re.compile(
    r"^(?:[（(]?[0-9０-９一二三四五六七八九十]{1,3}[）)]?[、.．,，:：]\s*|[-*+•·>]+\s*)"
)
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;…]")
_URL_RE = re.compile(r"^(?:https?://|www\.|ftp://)", re.I)


def _clip_title(text: str, limit: int) -> str:
    """收尾：去首尾标点、压空白、超长硬截（英文优先在词边界断开）。"""
    t = " ".join((text or "").split())
    t = t.strip("。！？!?；;，,、：:…·　 \"'“”‘’「」《》`")
    if len(t) > limit:
        seg = t[:limit]
        if re.search(r"[A-Za-z]$", seg) and " " in seg:
            space = seg.rfind(" ")
            if space >= limit // 2:
                seg = seg[:space]
        t = seg.rstrip("，,、：:；; ") + "…"
    return t or "新对话"


def auto_title(text: str, limit: int = 20) -> str:
    """从第一个问题提炼一个简短标题（纯规则，不调模型）。

    步骤：跳过代码块/URL/纯符号行选素材 → 剥 markdown 标记、序号、
    客套前缀 → 按句读切分取第一条有内容的 → 超长时找逗号切点收尾。
    """
    text = text or ""
    # 1) 选素材行：跳过围栏代码块、空行、URL、纯符号行；优先含中文的行
    candidates: list[str] = []
    in_fence = False
    fence_lang = ""
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("```"):
            if not in_fence:
                fence_lang = s[3:].strip()
            in_fence = not in_fence
            continue
        if in_fence or not s or _URL_RE.match(s):
            continue
        if not re.search(r"[\w\u4e00-\u9fa5]", s):  # 纯符号/表情行
            continue
        candidates.append(s)
    if not candidates:
        # 纯代码/纯 URL 输入：退而求其次，用代码块语言或第一行硬截
        if fence_lang:
            return _clip_title(fence_lang + " 代码", limit)
        first = next((l.strip() for l in text.splitlines() if l.strip()), "")
        first = re.sub(r"^(?:#{1,6}|>+|[*\-–—]{2,}|`{1,3})\s*", "", first)
        return _clip_title(first, limit) if first else "新对话"
    cjk = [s for s in candidates if re.search(r"[\u4e00-\u9fa5]", s)]
    body = (cjk or candidates)[0]

    # 2) 剥 markdown 标记 / 序号 / 客套前缀（各最多循环几层，防叠罗汉）
    body = re.sub(r"^(?:#{1,6}|>+|[*\-–—]{2,}|`{1,3})\s*", "", body)
    for _ in range(4):
        stripped = _NUM_RE.sub("", body)
        if stripped == body:
            break
        body = stripped
    for _ in range(8):
        stripped = _INTRO_RE.sub("", body)
        if stripped == body:  # 带标点的前缀剥不动了，再试紧贴动词的
            stripped = _INTRO_LOOSE_RE.sub("", body)
        if stripped == body or not stripped.strip():
            break
        body = stripped
    body = body.lstrip("。！？!?；;，,、：:…　 ")

    # 3) 按句读切分，取第一条足够有内容的句子（跳过「在吗」「hi」这类零碎）
    parts = [p.strip() for p in _SENT_SPLIT_RE.split(body) if p.strip()]
    if not parts:
        return _clip_title(body, limit)
    title = next((p for p in parts if len(p) >= 4), None)
    if title is None:
        title = parts[0] if len(parts) == 1 else parts[0] + parts[1]
    if len(title) < 2:
        # 整句都是问候语（「你好啊」剥完剩「啊」）：退回原始首行
        return _clip_title(candidates[0], limit)
    # 4) 超长时优先在逗号处截断，保住语义半句
    if len(title) > limit:
        cut = title.rfind("，", 0, limit)
        if cut >= 6:
            title = title[:cut]
    return _clip_title(title, limit)


@dataclass
class Run:
    id: str
    conv_id: str
    status: str = "queued"  # queued / running / done / stopped / error
    source: str = "chat"
    events: list[dict[str, Any]] = field(default_factory=list)
    subs: set[asyncio.Queue] = field(default_factory=set)
    handle: RunHandle = field(default_factory=RunHandle)
    task: asyncio.Task | None = None
    message_ids: list[str] = field(default_factory=list)
    title: str = ""
    error: str = ""
    group: str = ""  # 占用的并发资源组（local / conn:<id>）
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    on_finish: list[Callable[[Run], Any]] = field(default_factory=list)
    file_changes: list[dict[str, Any]] = field(default_factory=list)
    # 已发出插队注入、等 agent 消费后要从队列移除的排队项 id（FIFO）
    pending_inject_ids: list[str] = field(default_factory=list)
    _seq: int = 0

    def emit(self, event: dict[str, Any]) -> None:
        self._seq += 1
        payload = {**event, "seq": self._seq, "run_id": self.id}
        if event.get("type") == "done" and self.status == "running":
            self.status = "stopped" if event.get("cancelled") else "done"
        self.events.append(payload)
        # 事件缓存上限：一轮对话几百个 delta，留 4000 条足够重放，避免内存失控
        if len(self.events) > 4000:
            del self.events[: len(self.events) - 4000]
        for queue in list(self.subs):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover
                pass

    @property
    def active(self) -> bool:
        return self.status not in TERMINAL

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conv_id,
            "status": self.status,
            "source": self.source,
            "title": self.title,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "message_ids": list(self.message_ids),
        }


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.by_conv: dict[str, str] = {}
        self.queues: dict[str, list[dict[str, Any]]] = {}
        # 按资源组计数的并发占用：local 共用一个组，远端按连接分组
        self._active: dict[str, int] = {}
        self._slot = asyncio.Event()
        self._slot.set()
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------ 小工具

    def _lock(self, conv_id: str) -> asyncio.Lock:
        lock = self._locks.get(conv_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conv_id] = lock
        return lock

    @staticmethod
    def _limit_for(profile: dict[str, Any]) -> tuple[str, int]:
        """返回 (资源组, 并发上限)。

        - 本地权重：共用一个组、上限 1。同一个引擎一把 gen_lock，并行只会互相卡住；
        - OpenAI 兼容端点：按连接分组，上限取 perf.parallel_runs。
          不同服务商互不挤占（以前是全局计数，一个远端慢请求会把本地模型挤进排队）。
        """
        cfg = load_config()
        if profile.get("provider") == "local_hf":
            return "local", 1
        conn = str(profile.get("connection_id") or profile.get("base_url") or "default")
        try:
            limit = max(1, int((cfg.get("perf") or {}).get("parallel_runs", 2)))
        except (TypeError, ValueError):
            limit = 2
        return f"conn:{conn}", limit

    def _reconcile(self) -> None:
        """用实际「正在跑」的 run 数修正计数，防止任何异常路径导致槽位泄漏、后续全被排队。"""
        counts: dict[str, int] = {}
        for r in self.runs.values():
            if r.status == "running" and r.group:
                counts[r.group] = counts.get(r.group, 0) + 1
        self._active = counts

    async def _acquire(self, profile: dict[str, Any], run: Run) -> bool:
        group, limit = self._limit_for(profile)
        run.group = group
        announced = False
        while True:
            if run.handle.stopping:
                return False
            self._reconcile()
            if self._active.get(group, 0) < limit:
                self._active[group] = self._active.get(group, 0) + 1
                self._slot.set()
                return True
            waiting = sum(1 for r in self.runs.values() if r.status == "queued")
            if not announced:
                run.emit({"type": "queue", "state": "waiting", "position": max(1, waiting)})
                announced = True
            self._slot.clear()
            try:
                await asyncio.wait_for(self._slot.wait(), timeout=0.4)
            except asyncio.TimeoutError:
                pass

    def _release(self, group: str = "") -> None:
        if group:
            self._active[group] = max(0, self._active.get(group, 0) - 1)
        self._slot.set()

    # ------------------------------------------------------------ 启动一轮

    async def start(
        self,
        conv_id: str,
        *,
        content: str = "",
        regenerate: bool = False,
        run_id: str | None = None,
        model_id: str | None = None,
        thinking: bool | None = None,
        thinking_level: str | None = None,
        skills: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        source: str = "chat",
    ) -> Run:
        """校验 + 落库 + 起后台任务，立刻返回 Run（不等生成）。"""
        conv = store.get_conversation(conv_id)
        if conv is None:
            raise ValueError("会话不存在，请先创建会话")
        cfg = load_config()

        # 消息里的 /技能 标记：改写成位置标记并合并进本轮技能（保留语义位置信息）
        slash_skills: list[str] = []
        if not regenerate:
            text = (content or "").strip()
            if text:
                content, slash_skills = skills_lib.extract_tokens(text)
        base_skills = (
            list(skills) if skills is not None else list(conv.get("active_skills") or [])
        )
        merged_skills = list(dict.fromkeys([*slash_skills, *base_skills]))
        # 会话里可能残留已删除技能的 slug：先剔除，避免本轮白跑一次注入，
        # 也顺手把会话的 active_skills 自愈掉（下次勾选状态与实际一致）
        alive = set(skills_lib.prune(merged_skills))
        stale_skills = [n for n in merged_skills if n not in alive]
        if stale_skills:
            merged_skills = [n for n in merged_skills if n in alive]

        updates: dict[str, Any] = {}
        if thinking is not None:
            updates["thinking"] = thinking
        if thinking_level:
            level = thinking_level.lower()
            updates["thinking_level"] = level if level in ("off", "low", "medium", "high") else "medium"
        if skills is not None or slash_skills or stale_skills:
            updates["active_skills"] = merged_skills
        if updates:
            conv = store.update_conversation(conv_id, **updates) or conv

        if regenerate:
            messages = store.list_messages(conv_id)
            cut = len(messages) - 1
            while cut >= 0 and messages[cut]["role"] in ("assistant", "tool"):
                cut -= 1
            if cut + 1 < len(messages):
                store.truncate_from(conv_id, messages[cut + 1]["id"])
            if not any(m["role"] == "user" for m in store.list_messages(conv_id)):
                raise ValueError("没有可重新生成的内容")
        else:
            text = (content or "").strip()
            files = multimodal.normalize(attachments)
            if not text and not files:
                raise ValueError("消息内容为空")
            store.add_message(conv_id, "user", content=text, attachments=files or None)
            if conv["title"] in ("新对话", "", None):
                store.update_conversation(conv_id, title=auto_title(text or (files[0]["name"] if files else "")))

        conv = store.get_conversation(conv_id) or conv
        model_id = model_id or conv.get("model_id") or cfg["active_model_id"]
        profile = next((m for m in cfg["models"] if m.get("id") == model_id), None)
        if profile is None:
            raise ValueError(f"模型档案不存在: {model_id}")
        if conv.get("model_id") != model_id:
            store.update_conversation(conv_id, model_id=model_id)
            conv["model_id"] = model_id

        run = Run(
            id=run_id or f"run_{int(time.time() * 1000)}_{new_id()[:6]}",
            conv_id=conv_id,
            source=source,
            title=conv.get("title") or "",
        )
        self.runs[run.id] = run
        self.by_conv[conv_id] = run.id
        run.emit({"type": "accepted", "request_id": run.id, "status": "queued"})
        if stale_skills:
            run.emit(
                {
                    "type": "notice",
                    "message": f"技能已不存在，已忽略：{'、'.join(stale_skills)}",
                }
            )
        run.task = asyncio.create_task(self._drive(run, conv, profile))
        return run

    async def _drive(self, run: Run, conv: dict[str, Any], profile: dict[str, Any]) -> None:
        acquired = False
        watcher: filechanges.ChangeWatcher | None = None
        poller: asyncio.Task | None = None
        try:
            acquired = await self._acquire(profile, run)
            if not acquired:
                run.status = "stopped"
                run.emit({"type": "done", "conversation_id": run.conv_id, "cancelled": True})
                return
            run.status = "running"
            run.started_at = time.time()
            run.emit({"type": "running"})
            # 盯着工作目录和技能库：模型改了什么文件，第一时间告诉前端
            watcher = filechanges.ChangeWatcher(
                {
                    "": spaces.workdir_for(conv),
                    "skills/": skills_lib.directory(),
                }
            )
            watcher.start()
            poller = asyncio.create_task(self._watch_changes(run, watcher))
            async for event in agent.stream_turn(
                conversation=conv, profile=profile, store=store, handle=run.handle
            ):
                kind = event.get("type")
                if kind == "message_start":
                    mid = event.get("message_id")
                    if mid:
                        run.message_ids.append(mid)
                if kind == "user_injected":
                    # 排队消息已插入上下文：从队列移除，前端同步刷新
                    if event.get("message_id"):
                        run.message_ids.append(event["message_id"])
                    self._consume_injected(run.conv_id)
                if kind == "done":
                    run.status = "stopped" if event.get("cancelled") else "done"
                run.emit(event)
        except asyncio.CancelledError:
            run.handle.cancel()
            run.status = "stopped"
            run.emit({"type": "done", "conversation_id": run.conv_id, "cancelled": True})
        except Exception as exc:  # noqa: BLE001
            run.status = "error"
            run.error = f"{type(exc).__name__}: {exc}"
            run.emit({"type": "error", "message": run.error})
        finally:
            if poller is not None:
                poller.cancel()
                try:
                    await poller
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass
                poller = None
            if watcher is not None:
                try:
                    final = watcher.stop()
                    if final:
                        run.file_changes.extend(final)
                        # 先推变更再发 end：前端还能把卡片挂进当前气泡
                        run.emit({"type": "file_changes", "changes": final, "final": True})
                    self._persist_file_changes(run)
                except Exception:  # noqa: BLE001
                    pass
            if acquired:
                self._release(run.group)
            if run.status not in TERMINAL:
                run.status = "done"
            run.finished_at = time.time()
            run.emit({"type": "end"})
            if self.by_conv.get(run.conv_id) == run.id:
                self.by_conv.pop(run.conv_id, None)
            for callback in list(run.on_finish):
                try:
                    result = callback(run)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001
                    pass
            self._prune()
            asyncio.create_task(self._drain(run.conv_id))

    async def _watch_changes(self, run: Run, watcher: filechanges.ChangeWatcher) -> None:
        """Run 进行期间周期性 diff，把文件变更作为事件推给前端。

        所有变更都累积进 run.file_changes（不只看最终状态）：
        「建了又删」的文件最终 diff 是空的，但用户应该知道它存在过。
        """
        try:
            while True:
                await asyncio.sleep(filechanges.POLL_INTERVAL)
                try:
                    changes = watcher.poll()
                except Exception:  # noqa: BLE001
                    continue
                if changes:
                    run.file_changes.extend(changes)
                    run.emit({"type": "file_changes", "changes": changes})
        except asyncio.CancelledError:
            raise

    def _persist_file_changes(self, run: Run) -> None:
        """把这一轮的变更清单存进最后一条助手消息的 stats，刷新后还能回看。"""
        changes = run.file_changes
        if not changes:
            return
        msg_id = run.message_ids[-1] if run.message_ids else None
        if not msg_id:
            return
        try:
            msg = store.get_message(msg_id)
        except Exception:  # noqa: BLE001
            return
        if not msg:
            return
        stats = dict(msg.get("stats") or {})
        stats["file_changes"] = changes
        try:
            store.update_message(msg_id, stats=stats)
        except Exception:  # noqa: BLE001
            pass

    def _prune(self) -> None:
        cutoff = time.time() - 3600
        dead = [
            rid
            for rid, r in self.runs.items()
            if not r.active and (r.finished_at or 0) < cutoff
        ]
        for rid in dead:
            if not self.runs[rid].subs:
                self.runs.pop(rid, None)

    # ------------------------------------------------------------ 排队

    def queue_items(self, conv_id: str) -> list[dict[str, Any]]:
        return list(self.queues.get(conv_id) or [])

    def enqueue(
        self, conv_id: str, content: str, attachments: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        text = (content or "").strip()
        files = multimodal.normalize(attachments)
        if not text and not files:
            raise ValueError("消息内容为空")
        item = {
            "id": new_id("q_"),
            "conversation_id": conv_id,
            "content": text,
            "attachments": files,
            "created_at": time.time(),
        }
        self.queues.setdefault(conv_id, []).append(item)
        return item

    def _find(self, item_id: str) -> tuple[str, dict[str, Any]]:
        for conv_id, items in self.queues.items():
            for item in items:
                if item["id"] == item_id:
                    return conv_id, item
        raise KeyError("队列里没有这条消息")

    def update_item(self, item_id: str, content: str) -> dict[str, Any]:
        _, item = self._find(item_id)
        text = (content or "").strip()
        if not text:
            raise ValueError("消息内容为空")
        item["content"] = text
        return item

    def remove_item(self, item_id: str) -> None:
        conv_id, item = self._find(item_id)
        self.queues[conv_id].remove(item)
        if not self.queues[conv_id]:
            self.queues.pop(conv_id, None)

    def reorder(self, conv_id: str, ids: list[str]) -> list[dict[str, Any]]:
        items = self.queues.get(conv_id) or []
        index = {item["id"]: item for item in items}
        ordered = [index[i] for i in ids if i in index]
        ordered += [item for item in items if item["id"] not in ids]
        if ordered:
            self.queues[conv_id] = ordered
        return ordered

    def send_now(self, item_id: str) -> dict[str, Any]:
        """插队：移到队首。

        纯文本消息不打断当前执行——放进注入通道，由 agent 主循环在
        下一轮模型调用前自然写进上下文（Claude Code / Cline 的做法）。
        带附件的消息要做多模态预处理，没法中途插入，等当前轮结束后
        以队首顺序正常开跑。
        """
        conv_id, item = self._find(item_id)
        items = self.queues.get(conv_id) or []
        items.remove(item)
        items.insert(0, item)
        self.queues[conv_id] = items
        run = self.runs.get(self.by_conv.get(conv_id, ""))
        if run and run.active:
            if not item.get("attachments"):
                # 斜杠技能标记顺手改写，让模型看到语义位置
                text, _ = skills_lib.extract_tokens(item["content"])
                run.handle.inject(text or item["content"])
                run.pending_inject_ids.append(item["id"])
        else:
            asyncio.create_task(self._drain(conv_id))
        return item

    def _consume_injected(self, conv_id: str) -> None:
        """agent 已把队首消息插入上下文：从队列移除对应的排队项。"""
        run = self.runs.get(self.by_conv.get(conv_id, ""))
        if run is None or not run.pending_inject_ids:
            return
        item_id = run.pending_inject_ids.pop(0)
        items = self.queues.get(conv_id) or []
        for item in items:
            if item["id"] == item_id:
                items.remove(item)
                break
        if not items:
            self.queues.pop(conv_id, None)

    async def _drain(self, conv_id: str) -> None:
        async with self._lock(conv_id):
            if self.by_conv.get(conv_id):
                return
            items = self.queues.get(conv_id) or []
            if not items:
                return
            item = items.pop(0)
            if not items:
                self.queues.pop(conv_id, None)
            try:
                await self.start(
                    conv_id,
                    content=item["content"],
                    attachments=item.get("attachments") or None,
                    source="queue",
                )
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 停止 / 状态

    def stop(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if run is None or not run.active:
            return False
        run.handle.cancel()
        run.emit({"type": "stopping"})
        # 协作式取消只在下一次检查点生效：远端模型可能正卡在「等首个 token」，
        # 本地模型可能还在加载权重 —— 超过 grace 还没结束就直接取消后台任务，
        # 否则用户点了停止，界面要干等几十秒才反应。
        if run.task is not None and not run.task.done():
            try:
                asyncio.create_task(self._force_stop(run))
            except RuntimeError:  # 没有事件循环（理论上不会走到）
                run.task.cancel()
        return True

    async def _force_stop(self, run: Run, grace: float = 0.8) -> None:
        task = run.task
        if task is None:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not run.active or task.done():
                return
            await asyncio.sleep(0.05)
        if run.active and not task.done():
            task.cancel()

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def active_run(self, conv_id: str) -> Run | None:
        run = self.runs.get(self.by_conv.get(conv_id, ""))
        return run if run and run.active else None

    def snapshot(self) -> dict[str, Any]:
        recent = time.time() - 300
        runs = [
            r.summary()
            for r in self.runs.values()
            if r.active or (r.finished_at or 0) >= recent
        ]
        runs.sort(key=lambda r: r["created_at"])
        return {
            "runs": runs,
            "queues": {
                conv_id: {"count": len(items), "items": items}
                for conv_id, items in self.queues.items()
                if items
            },
        }

    async def follow(self, run: Run, from_seq: int = 0):
        """从头/中途追随一个 Run：先重放缓存，再等新事件。"""
        queue: asyncio.Queue = asyncio.Queue()
        run.subs.add(queue)
        seen = int(from_seq or 0)
        ended = False
        try:
            for event in list(run.events):
                if event["seq"] <= seen:
                    continue
                seen = event["seq"]
                if event.get("type") == "end":
                    ended = True
                yield event
            if ended:
                return
            if not run.active and not run.subs:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    if not run.active:
                        return
                    # 心跳：模型 prefill 阶段可能几十秒没有新事件，
                    # 定期发一个 ping 让连接别被中间层掐掉
                    yield {"type": "ping", "seq": seen, "run_id": run.id}
                    continue
                if event["seq"] <= seen:
                    continue
                seen = event["seq"]
                yield event
                if event.get("type") == "end":
                    return
        finally:
            run.subs.discard(queue)


manager = RunManager()
