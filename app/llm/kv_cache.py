"""前缀 KV 缓存：复用已经 prefill 过的提示词前缀。

为什么需要它
------------
transformers 的 `generate()` 每次调用都会把整段提示词从头算一遍注意力。
本机 CPU 上 prefill 只有 ~200 tok/s，而一段典型的本地对话每轮固定带着
「系统提示 + 29 个工具 schema」约 4000+ token —— 也就是每轮光"读入"就要
十几秒，哪怕你只问了一句"你好"。

把这份 KV 记下来，下一轮只算新增的几十个 token，首字延迟能从十几秒降到
一秒以内。同一份前缀还能跨会话共享（系统提示和工具 schema 完全相同），
所以开新会话的第一个问题也是秒开。

安全性
------
- 只在 token 序列的最长公共前缀上复用，任何 token 不一致都退回全量 prefill，
  所以结果与"重新算一遍"等价（浮点层面可能有末位差异）。
- 每个条目独占使用：取出后从缓存里移除，生成结束再放回，避免并发写坏。
"""

from __future__ import annotations

import threading
import time
from typing import Any

# 太短的前缀不值得复用（裁剪 + 建 attention mask 也有成本）
MIN_REUSE_TOKENS = 48


def seq_len(cache: Any) -> int:
    try:
        return int(cache.get_seq_length())
    except Exception:  # noqa: BLE001
        return 0


def crop_to(cache: Any, target: int) -> bool:
    """把 cache 截断到 target 个 token（transformers 5.x 的 crop 收负数）。"""
    cur = seq_len(cache)
    if cur <= target:
        return True
    try:
        cache.crop(target - cur)  # 负数 = 从尾部删掉这么多
        return seq_len(cache) == target
    except Exception:  # noqa: BLE001
        return False


def _common_prefix(a: list[int], b: list[int]) -> int:
    n = min(len(a), len(b))
    i = 0
    # 一次比一个，命中率高的场景下差异点通常很靠后，所以从尾部先粗筛
    while i + 8 <= n and a[i : i + 8] == b[i : i + 8]:
        i += 8
    while i < n and a[i] == b[i]:
        i += 1
    return i


class _Entry:
    __slots__ = ("tokens", "cache", "label", "ts")

    def __init__(self, tokens: list[int], cache: Any, label: str) -> None:
        self.tokens = tokens
        self.cache = cache
        self.label = label
        self.ts = time.time()


class PrefixCache:
    """按「提示词前缀」保存 KV 的 LRU 缓存。"""

    def __init__(self, max_tokens: int = 4096, max_entries: int = 2) -> None:
        self.max_tokens = max(256, int(max_tokens))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self.hits = 0
        self.misses = 0
        self.reused_tokens = 0
        self.dropped = 0

    # ---------------- 取用 ----------------

    def take(self, key: str, tokens: list[int]) -> tuple[Any, int] | None:
        """找一条可复用的前缀，返回 (cache, 复用长度)；cache 所有权移交给调用方。"""
        with self._lock:
            best_key: str | None = None
            best_len = 0
            for name, entry in self._entries.items():
                n = _common_prefix(entry.tokens, tokens)
                if n > best_len:
                    best_key, best_len = name, n
            if best_key is None:
                self.misses += 1
                return None

            entry = self._entries[best_key]
            # cache 里实际有的可能比记录少（生成中途被打断），取小值
            best_len = min(best_len, seq_len(entry.cache))
            # 至少要留一个 token 喂给 generate
            best_len = min(best_len, len(tokens) - 1)
            if best_len < MIN_REUSE_TOKENS or not crop_to(entry.cache, best_len):
                self._entries.pop(best_key, None)
                self.dropped += 1
                self.misses += 1
                return None

            self._entries.pop(best_key, None)
        self.hits += 1
        self.reused_tokens += best_len
        return entry.cache, best_len

    def release(self, key: str, tokens: list[int], cache: Any, label: str = "") -> None:
        """把用完的 cache 交回来，按预算截断后保存。"""
        if cache is None:
            return
        target = min(len(tokens), self.max_tokens)
        if target < MIN_REUSE_TOKENS:
            return
        if not crop_to(cache, target):
            return
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = _Entry(list(tokens[:target]), cache, label)
            self._evict_locked()

    def drop(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _evict_locked(self) -> None:
        while self._entries and (
            len(self._entries) > self.max_entries
            or sum(len(e.tokens) for e in self._entries.values()) > self.max_tokens
        ):
            oldest = min(self._entries.items(), key=lambda kv: kv[1].ts)[0]
            self._entries.pop(oldest, None)
            self.dropped += 1

    # ---------------- 观测 ----------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._entries),
                "cached_tokens": sum(len(e.tokens) for e in self._entries.values()),
                "max_tokens": self.max_tokens,
                "max_entries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
                "reused_tokens": self.reused_tokens,
                "dropped": self.dropped,
            }
