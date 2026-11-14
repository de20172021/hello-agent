"""本地 transformers 推理后端（CPU / CUDA 通吃）。

相对最初版本，这里做了三件影响速度的事：

1. **提示词只 encode 一次**：以前 build_prompt 之后先 count_tokens 编一遍、
   generate 里再编一遍，现在统一走 `tokenize()`，顺手把长度也算出来。
2. **前缀 KV 缓存**：同一段前缀（系统提示 + 工具 schema + 历史）不再每轮重算，
   只 prefill 增量的那几十个 token。详见 `kv_cache.py`。
3. **思考两段共享缓存**：先思考、超预算掐断、再强制回答的老逻辑里，第二段会把
   整段提示词 + 思考内容重新 prefill 一遍（在 CPU 上就是十几秒），现在直接接着
   第一段的 KV 往下写。
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator

from ..settings import THINKING_BUDGET, model_path_of
from .base import BaseProvider, ProviderError, RunHandle
from .kv_cache import MIN_REUSE_TOKENS, PrefixCache, crop_to
from .parsing import TagStripper, ThinkSplitter, ToolCallGuard

# 进程级模型缓存：多个会话共享同一份权重
_ENGINES: dict[str, "Engine"] = {}
_LOADING: dict[str, threading.Event] = {}
_ENGINE_LOCK = threading.Lock()
_RUNTIME: dict[str, dict[str, Any]] = {}


def runtime_state(profile_id: str) -> dict[str, Any]:
    return _RUNTIME.get(profile_id) or {"state": "unloaded", "detail": ""}


def _set_state(profile_id: str, state: str, detail: str = "") -> None:
    _RUNTIME[profile_id] = {"state": state, "detail": detail, "ts": time.time()}


def evict_all_engines() -> int:
    """推理参数变了（int8 / 线程 / 缓存额度）就清掉旧引擎，避免两份权重同时驻留。"""
    with _ENGINE_LOCK:
        engines = list(_ENGINES.items())
        _ENGINES.clear()
    for _key, engine in engines:
        try:
            engine.prefix.clear()
            del engine.model
        except Exception:  # noqa: BLE001
            pass
    import gc

    gc.collect()
    return len(engines)


def clear_prefix_caches() -> int:
    """只清 KV 前缀缓存，不动权重。"""
    with _ENGINE_LOCK:
        engines = list(_ENGINES.values())
    for engine in engines:
        try:
            engine.prefix.clear()
        except Exception:  # noqa: BLE001
            pass
    return len(engines)


def cache_stats() -> dict[str, Any]:
    """给 /api/perf 用的全局缓存统计。"""
    with _ENGINE_LOCK:
        engines = list(_ENGINES.values())
    if not engines:
        return {"engines": 0}
    merged = {
        "engines": len(engines),
        "entries": 0,
        "cached_tokens": 0,
        "hits": 0,
        "misses": 0,
        "reused_tokens": 0,
        "hit_rate": 0.0,
    }
    for engine in engines:
        st = engine.prefix.stats()
        for key in ("entries", "cached_tokens", "hits", "misses", "reused_tokens"):
            merged[key] += st.get(key, 0)
    total = merged["hits"] + merged["misses"]
    merged["hit_rate"] = round(merged["hits"] / total, 3) if total else 0.0
    return merged


class _StopOnEvent:
    """动态构造的 StoppingCriteria，避免 import 期依赖 transformers。"""

    @staticmethod
    def build(event: threading.Event):
        from transformers import StoppingCriteria

        class _Stop(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):  # noqa: D102
                return event.is_set()

        return _Stop()


class _LinkedStop:
    """两段式生成用的停止信号：自己的事件 + 全局取消事件，任一置位就算停。

    第一段（思考）有自己的预算截断，又必须能被用户「停止」立刻打断；
    直接把 handle 的 thread_stop 传进去会让预算截断连带把第二段也停掉。
    """

    def __init__(self, own: threading.Event, shared: threading.Event | None = None) -> None:
        self._own = own
        self._shared = shared

    def set(self) -> None:
        self._own.set()

    def is_set(self) -> bool:
        return self._own.is_set() or (self._shared is not None and self._shared.is_set())


class Engine:
    def __init__(
        self,
        path: Path,
        device: str,
        dtype: str,
        enable_thinking: bool,
        perf: dict[str, Any] | None = None,
    ):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        perf = dict(perf or {})
        self.torch = torch
        self.path = path
        self.enable_thinking = enable_thinking
        self.gen_lock = threading.Lock()
        self.perf = perf
        self.int8 = bool(perf.get("int8", False))
        self.cache_enabled = bool(perf.get("prefix_cache", True))

        if not path.exists():
            raise FileNotFoundError(f"模型目录不存在：{path}")

        threads = int(perf.get("threads") or 0)
        if threads > 0:
            # 12 代酷睿这类大小核机器上，线程开满反而不如留几个给系统
            torch.set_num_threads(threads)
        self.threads = torch.get_num_threads()

        self.tokenizer = AutoTokenizer.from_pretrained(str(path), trust_remote_code=True)
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device

        if dtype == "auto":
            if device == "cuda":
                target = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
            else:
                # AVX2-only 的 CPU 上 bf16 会退化到软件模拟，反而慢好几倍，
                # 所以 CPU 上用 fp32（需要更省内存/更快可以开 int8）。
                target = torch.float32
        else:
            target = getattr(torch, dtype, torch.float32)

        extra: dict[str, Any] = {"low_cpu_mem_usage": True}
        if device == "cuda":
            extra["device_map"] = "auto"

        try:
            self.model = AutoModelForCausalLM.from_pretrained(str(path), dtype=target, **extra)
        except TypeError:
            self.model = AutoModelForCausalLM.from_pretrained(
                str(path), torch_dtype=target, **extra
            )
        if device != "cuda":
            self.model.to(device)
        self.model.eval()

        if self.int8:
            self.model = self._quantize(self.model)

        cfg = getattr(self.model, "config", None)
        self.max_context = int(getattr(cfg, "max_position_embeddings", 32768) or 32768)

        self.prefix = PrefixCache(
            max_tokens=int(perf.get("cache_tokens", 4096)),
            max_entries=int(perf.get("cache_entries", 2)),
        )

    @staticmethod
    def _quantize(model):
        """INT8 动态量化：权重从 4 字节压到 1 字节，解码受内存带宽限制时直接提速。"""
        import warnings

        import torch
        import torch.nn as nn
        from torch.ao.quantization import quantize_dynamic

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            quantized = quantize_dynamic(model, {nn.Linear}, dtype=torch.qint8)
        quantized.eval()
        return quantized

    # ---------------- 提示词 ----------------

    def build_prompt(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        enable_thinking: bool | None = None,
        add_generation_prompt: bool = True,
    ) -> str:
        hf_messages = BaseProvider.to_hf_messages(messages)
        thinking = self.enable_thinking if enable_thinking is None else bool(enable_thinking)
        kwargs: dict[str, Any] = {
            "tokenize": False,
            "add_generation_prompt": add_generation_prompt,
            "enable_thinking": thinking,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            return self.tokenizer.apply_chat_template(hf_messages, **kwargs)
        except TypeError:
            kwargs.pop("enable_thinking", None)
            return self.tokenizer.apply_chat_template(hf_messages, **kwargs)

    def tokenize(self, text: str, max_new: int = 0) -> list[int]:
        """整个提示词只在这里 encode 一次。"""
        if not text:
            return []
        limit = max(512, self.max_context - max(0, int(max_new)))
        try:
            enc = self.tokenizer(
                text,
                add_special_tokens=False,
                truncation=True,
                max_length=limit,
            )
            return list(enc["input_ids"])
        except Exception:  # noqa: BLE001
            return []

    def count_tokens(self, text: str) -> int:
        return len(self.tokenize(text))

    # ---------------- 前缀缓存 ----------------

    def take_prefix(self, key: str, tokens: list[int]):
        if not self.cache_enabled:
            return None
        return self.prefix.take(key, tokens)

    def release_prefix(self, key: str, tokens: list[int], cache: Any) -> None:
        if self.cache_enabled:
            self.prefix.release(key, tokens, cache)

    def drop_prefix(self, key: str) -> None:
        self.prefix.drop(key)

    def prefetch(
        self,
        tokens: list[int],
        key: str = "__base__",
        chunk: int = 0,
        should_stop: Any = None,
    ) -> dict[str, Any]:
        """只 prefill 不生成：把系统提示 + 工具 schema 这段固定前缀先算好。

        分块进行，每块之间放锁 —— 用户真发消息时可以让出 CPU 给对话，
        已经算完的那部分前缀照样能用。
        """
        if not self.cache_enabled or len(tokens) < MIN_REUSE_TOKENS:
            return {"ok": False, "detail": "缓存未启用或前缀太短"}
        if len(tokens) > 8000:
            # 上万 token 的前缀本身就算不动，预热只会拖慢启动
            return {
                "ok": False,
                "detail": f"固定前缀有 {len(tokens)} token，太长，跳过预热（建议关闭 MCP 工具内联）",
            }

        dev = self._input_device()
        total = len(tokens)
        if chunk <= 0:
            # 分块越小越好抢占，但每块都要重拷一次 KV；切成 8 块是速度与响应的折中
            chunk = max(256, (total + 7) // 8)
        cache = None
        done = 0
        started = time.perf_counter()
        while done < total:
            if should_stop is not None and should_stop():
                break
            part = tokens[done : done + chunk]
            ids = self.torch.tensor([part], dtype=self.torch.long, device=dev)
            with self.gen_lock:
                with self.torch.inference_mode():
                    kwargs: dict[str, Any] = {
                        "input_ids": ids,
                        "use_cache": True,
                        "logits_to_keep": 1,
                    }
                    if cache is not None:
                        kwargs["past_key_values"] = cache
                        kwargs["attention_mask"] = self.torch.ones(
                            (1, done + len(part)), dtype=self.torch.long, device=dev
                        )
                    out = self.model(**kwargs)
                cache = out.past_key_values
                done += len(part)

        if cache is None or done < MIN_REUSE_TOKENS:
            return {"ok": False, "detail": "预热中断"}
        self.prefix.release(key, tokens[:done], cache)
        return {
            "ok": True,
            "tokens": done,
            "total": total,
            "seconds": round(time.perf_counter() - started, 2),
            "partial": done < total,
        }

    # ---------------- 生成 ----------------

    def _generation_kwargs(self, params: dict[str, Any], stop_event: threading.Event) -> dict:
        temperature = float(params.get("temperature", 0.6))
        top_p = float(params.get("top_p", 0.95))
        max_new = int(params.get("max_new_tokens_override") or params.get("max_tokens", 2048))
        kwargs: dict[str, Any] = {
            "max_new_tokens": max_new,
            "repetition_penalty": float(params.get("repetition_penalty", 1.05)),
            "pad_token_id": self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
            "eos_token_id": self.tokenizer.eos_token_id,
        }
        if temperature <= 0.01:
            kwargs["do_sample"] = False
        else:
            kwargs.update(
                do_sample=True,
                temperature=temperature,
                top_p=top_p,
                top_k=int(params.get("top_k", 20)),
            )
        from transformers import StoppingCriteriaList

        kwargs["stopping_criteria"] = StoppingCriteriaList([_StopOnEvent.build(stop_event)])
        return kwargs

    def generate_stream(
        self,
        tokens: list[int],
        params: dict[str, Any],
        out: "queue.Queue[tuple[str, Any]]",
        stop_event: threading.Event,
        lease: tuple[Any, int] | None = None,
        box: dict[str, Any] | None = None,
    ) -> None:
        """流式生成。

        `lease` 是 (cache, 已 prefill 长度)，表示前 reuse 个 token 不用再算。
        生成结束后，最终的 cache 与新增 token id 会写进 `box`，供调用方接着复用。
        """
        from transformers import TextIteratorStreamer

        box = box if box is not None else {}
        try:
            with self.gen_lock:
                total = len(tokens)
                cache, reuse = (lease if lease else (None, 0))
                if reuse >= total:
                    reuse = max(0, total - 1)
                    if cache is not None:
                        crop_to(cache, reuse)
                new_ids = tokens[reuse:]
                if not new_ids:
                    out.put(("end", None))
                    return

                dev = self._input_device()
                input_ids = self.torch.tensor([new_ids], dtype=self.torch.long, device=dev)

                streamer = TextIteratorStreamer(
                    self.tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=300
                )
                kwargs = self._generation_kwargs(params, stop_event)
                kwargs["input_ids"] = input_ids
                kwargs["streamer"] = streamer
                kwargs["return_dict_in_generate"] = True
                if cache is not None:
                    kwargs["past_key_values"] = cache
                    # 带 cache 续生成时必须给全长的 attention_mask，
                    # 否则库会用「mask 长度 - 输入长度」推切片，得到空输入。
                    kwargs["attention_mask"] = self.torch.ones(
                        (1, total), dtype=self.torch.long, device=dev
                    )

                box["reuse"] = reuse
                box["cache"] = None

                def _run() -> None:
                    try:
                        result = self.model.generate(**kwargs)
                        final = getattr(result, "past_key_values", None)
                        box["cache"] = final if final is not None else cache
                        seq = getattr(result, "sequences", None)
                        if seq is not None:
                            gen = seq[0, len(new_ids) :].tolist()
                            box["gen_ids"] = gen
                            box["new_tokens"] = len(gen)
                    except Exception as exc:  # noqa: BLE001
                        box["cache"] = None
                        box["error"] = f"{type(exc).__name__}: {exc}"
                        out.put(("error", box["error"]))
                    finally:
                        out.put(("end", None))

                worker = threading.Thread(target=_run, daemon=True)
                worker.start()

                for chunk in streamer:
                    if stop_event.is_set():
                        break
                    if chunk:
                        out.put(("delta", chunk))
                worker.join(timeout=30)
        except Exception as exc:  # noqa: BLE001
            out.put(("error", f"{type(exc).__name__}: {exc}"))
            out.put(("end", None))

    def _input_device(self):
        if self.device == "cuda":
            return getattr(self.model, "device", "cuda")
        return "cpu"


class LocalHFProvider(BaseProvider):
    kind = "local_hf"

    def __init__(self, profile: dict[str, Any]):
        super().__init__(profile)
        self._load_lock = threading.Lock()
        self.profile_id = profile.get("id", "local")

    # ---------------- 状态 ----------------

    def _perf(self) -> dict[str, Any]:
        from ..settings import load_config

        return dict(load_config().get("perf") or {})

    def _cache_key(self) -> str:
        path = model_path_of(self.profile)
        perf = self._perf()
        flags = (
            f"int8={int(bool(perf.get('int8')))}"
            f"|th={int(perf.get('threads') or 0)}"
            f"|cache={int(perf.get('cache_tokens', 4096))}x{int(perf.get('cache_entries', 2))}"
        )
        return (
            f"{path}|{self.profile.get('device', 'auto')}|"
            f"{self.profile.get('dtype', 'auto')}|{flags}"
        )

    def _engine(self) -> Engine | None:
        return _ENGINES.get(self._cache_key())

    async def status(self) -> dict[str, Any]:
        engine = self._engine()
        state = runtime_state(self.profile_id)
        path = model_path_of(self.profile)
        base = {
            "profile_id": self.profile_id,
            "provider": self.kind,
            "model_path": str(path),
            "path_exists": path.exists(),
            "device": self.profile.get("device", "auto"),
        }
        if engine is not None and state.get("state") == "ready":
            return {**base, "state": "ready", "detail": self._device_detail(engine)}
        if state.get("state") == "loading":
            return {**base, "state": "loading", "detail": state.get("detail", "")}
        if state.get("state") == "error":
            return {**base, "state": "error", "detail": state.get("detail", "")}
        return {**base, "state": "unloaded", "detail": ""}

    @staticmethod
    def _device_detail(engine: Engine) -> str:
        detail = "CPU" if engine.device == "cpu" else "CUDA"
        dtype = getattr(engine.model, "dtype", None)
        if dtype is not None:
            detail += f" · {str(dtype).replace('torch.', '')}"
        if engine.int8:
            detail += " · int8"
        detail += f" · {engine.threads} 线程"
        if engine.cache_enabled:
            detail += " · 前缀缓存"
        return detail

    async def load(self) -> dict[str, Any]:
        await asyncio.to_thread(self._load_sync)
        return await self.status()

    def _load_sync(self) -> Engine:
        key = self._cache_key()
        ticket: threading.Event | None = None
        while ticket is None:
            with _ENGINE_LOCK:
                if key in _ENGINES:
                    return _ENGINES[key]
                existing = _LOADING.get(key)
                if existing is None:
                    ticket = threading.Event()
                    _LOADING[key] = ticket
                else:
                    waiter = existing
            if ticket is None:
                # 别的协程正在加载，等它完成
                waiter.wait(timeout=900)
                if not waiter.is_set():
                    raise ProviderError("模型加载超时")
            else:
                break

        _set_state(self.profile_id, "loading", "正在加载权重…")
        try:
            engine = Engine(
                path=model_path_of(self.profile),
                device=self.profile.get("device", "auto"),
                dtype=self.profile.get("dtype", "auto"),
                enable_thinking=bool(self.profile.get("enable_thinking", False)),
                perf=self._perf(),
            )
        except Exception as exc:  # noqa: BLE001
            _set_state(self.profile_id, "error", str(exc))
            with _ENGINE_LOCK:
                _LOADING.pop(key, None)
            ticket.set()
            raise ProviderError(f"模型加载失败：{exc}") from exc

        with _ENGINE_LOCK:
            _ENGINES[key] = engine
            _LOADING.pop(key, None)
        _set_state(self.profile_id, "ready", self._device_detail(engine))
        ticket.set()
        return engine

    async def unload(self) -> None:
        key = self._cache_key()
        with _ENGINE_LOCK:
            engine = _ENGINES.pop(key, None)
        if engine is not None:
            try:
                engine.prefix.clear()
                del engine.model
                import gc

                gc.collect()
                if engine.device == "cuda":
                    engine.torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
        _set_state(self.profile_id, "unloaded", "")

    async def warm(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        should_stop: Any = None,
    ) -> dict[str, Any]:
        """预读固定前缀（系统提示 + 工具 schema），启动后调一次即可。"""
        try:
            engine = await asyncio.to_thread(self._load_sync)
            if not engine.cache_enabled:
                return {"ok": False, "detail": "前缀缓存已关闭"}
            prompt = await asyncio.to_thread(
                engine.build_prompt, messages, tools, False, False
            )
            tokens = await asyncio.to_thread(engine.tokenize, prompt)
            return await asyncio.to_thread(engine.prefetch, tokens, "__base__", 0, should_stop)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    async def evict_all(self) -> None:
        """参数（int8/线程/缓存额度）变了以后，把所有旧权重的引擎清掉。"""
        with _ENGINE_LOCK:
            engines = list(_ENGINES.items())
            _ENGINES.clear()
        for _key, engine in engines:
            try:
                engine.prefix.clear()
                del engine.model
            except Exception:  # noqa: BLE001
                pass
        import gc

        gc.collect()

    # ---------------- 推理 ----------------

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        handle: RunHandle | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        from ..context import estimate_tokens

        params = dict(params or {})
        engine = await asyncio.to_thread(self._load_sync)
        if handle is not None and handle.stopping:
            # 用户在这之前就点了停止（最常见的是「首次加载权重」阶段）：直接收工
            return

        thinking_on = bool(params.get("thinking", self.profile.get("enable_thinking", False)))
        level = str(params.get("thinking_level") or "medium")
        budget = max(64, int(params.get("thinking_budget") or THINKING_BUDGET.get(level, 900)))
        max_new = int(params.get("max_tokens", 2048))

        prompt = await asyncio.to_thread(engine.build_prompt, messages, tools, thinking_on)
        tokens = await asyncio.to_thread(engine.tokenize, prompt, max_new)
        prompt_tokens = len(tokens)

        conv_key = str(params.get("conversation_id") or "default")
        lease = await asyncio.to_thread(engine.take_prefix, conv_key, tokens)
        reused = lease[1] if lease else 0

        stop_event = handle.thread_stop if handle else threading.Event()
        started = time.perf_counter()
        first_at: float | None = None
        guard = ToolCallGuard()
        keep: dict[str, Any] = {"cache": None, "ok": False}

        def first_event() -> dict[str, Any] | None:
            nonlocal first_at
            if first_at is None:
                first_at = time.perf_counter()
                return {"type": "first_token", "ttft": first_at - started}
            return None

        reasoning_text = ""
        visible_text = ""
        gen_tokens = 0
        raw1 = ""
        cut_by_budget = False
        answered = False

        try:
            if thinking_on:
                # ---- 第一段：只让它思考，超出预算就掐断 ----
                q1: "queue.Queue[tuple[str, Any]]" = queue.Queue()
                # 自己的预算事件 + 全局取消事件：用户点停止时第一段也能立刻断
                stop1 = _LinkedStop(threading.Event(), stop_event)
                p1 = dict(params)
                p1["max_new_tokens_override"] = budget
                box1: dict[str, Any] = {}
                threading.Thread(
                    target=engine.generate_stream,
                    args=(tokens, p1, q1, stop1),
                    kwargs={"lease": lease, "box": box1},
                    daemon=True,
                ).start()

                splitter = ThinkSplitter()
                while True:
                    if handle is not None and handle.cancelled:
                        stop1.set()
                    try:
                        kind, payload = q1.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.02)
                        continue
                    if kind == "end":
                        break
                    if kind == "error":
                        raise ProviderError(str(payload))
                    if kind != "delta":
                        continue

                    chunk = str(payload)
                    raw1 += chunk
                    event = first_event()
                    if event:
                        yield event
                    for channel, text in splitter.feed(chunk):
                        if channel == "reasoning":
                            reasoning_text += text
                            yield {"type": "reasoning", "text": text}
                            if not cut_by_budget and estimate_tokens(reasoning_text) >= budget:
                                cut_by_budget = True
                                stop1.set()
                        else:
                            answered = True
                            for visible in guard.feed(text):
                                visible_text += visible
                                yield {"type": "delta", "text": visible}
                    if cut_by_budget:
                        break

                if cut_by_budget:
                    deadline = time.perf_counter() + 20
                    while time.perf_counter() < deadline:
                        try:
                            kind, _ = q1.get_nowait()
                        except queue.Empty:
                            await asyncio.sleep(0.05)
                            continue
                        if kind == "end":
                            break
                    yield {
                        "type": "thinking_done",
                        "budget": budget,
                        "tokens": estimate_tokens(reasoning_text),
                    }

                gen_tokens += int(box1.get("new_tokens") or 0)
                keep["cache"] = box1.get("cache")

                if not answered and not (handle is not None and handle.cancelled):
                    # ---- 第二段：接着第一段的 KV 往下写，不重新 prefill ----
                    lease2: tuple[Any, int] | None = None
                    tokens2 = tokens
                    gen_ids = box1.get("gen_ids") or []
                    cache1 = box1.get("cache")
                    if cache1 is not None and gen_ids:
                        tokens2 = tokens + list(gen_ids)
                        lease2 = (cache1, len(tokens2))
                        reused += len(tokens2) - len(tokens)
                    else:
                        # 拿不到 token id 就退回文本拼接，至少要复用提示词本身
                        extra = await asyncio.to_thread(engine.tokenize, raw1 + "\n\n")
                        tokens2 = tokens + extra
                        if cache1 is not None and crop_to(cache1, len(tokens)):
                            lease2 = (cache1, len(tokens))

                    q2: "queue.Queue[tuple[str, Any]]" = queue.Queue()
                    p2 = dict(params)
                    p2["max_new_tokens_override"] = max_new
                    box2: dict[str, Any] = {}
                    threading.Thread(
                        target=engine.generate_stream,
                        args=(tokens2, p2, q2, stop_event),
                        kwargs={"lease": lease2, "box": box2},
                        daemon=True,
                    ).start()
                    stripper = TagStripper()
                    first_visible = True
                    while True:
                        if handle is not None and handle.cancelled:
                            stop_event.set()
                        try:
                            kind, payload = q2.get_nowait()
                        except queue.Empty:
                            await asyncio.sleep(0.02)
                            continue
                        if kind == "end":
                            break
                        if kind == "error":
                            raise ProviderError(str(payload))
                        if kind != "delta":
                            continue
                        text = stripper.feed(str(payload))
                        if first_visible:
                            text = text.lstrip("\n\r\t ")
                            if not text:
                                continue
                            first_visible = False
                        if not text:
                            continue
                        event = first_event()
                        if event:
                            yield event
                        for visible in guard.feed(text):
                            visible_text += visible
                            yield {"type": "delta", "text": visible}
                    tail = stripper.flush()
                    if tail:
                        for visible in guard.feed(tail):
                            visible_text += visible
                            yield {"type": "delta", "text": visible}
                    gen_tokens += int(box2.get("new_tokens") or 0)
                    keep["cache"] = box2.get("cache") or keep["cache"]
            else:
                # ---- 不开启思考：单段生成，仍兼容模型自发输出的 think 块 ----
                q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
                p = dict(params)
                p["max_new_tokens_override"] = max_new
                box: dict[str, Any] = {}
                threading.Thread(
                    target=engine.generate_stream,
                    args=(tokens, p, q, stop_event),
                    kwargs={"lease": lease, "box": box},
                    daemon=True,
                ).start()
                splitter = ThinkSplitter()
                while True:
                    if handle is not None and handle.cancelled:
                        stop_event.set()
                    try:
                        kind, payload = q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.02)
                        continue
                    if kind == "end":
                        break
                    if kind == "error":
                        raise ProviderError(str(payload))
                    if kind != "delta":
                        continue
                    event = first_event()
                    if event:
                        yield event
                    for channel, text in splitter.feed(str(payload)):
                        if channel == "reasoning":
                            reasoning_text += text
                            yield {"type": "reasoning", "text": text}
                        else:
                            for visible in guard.feed(text):
                                visible_text += visible
                                yield {"type": "delta", "text": visible}

                for channel, text in splitter.flush():
                    if channel == "reasoning":
                        yield {"type": "reasoning", "text": text}
                    else:
                        for visible in guard.feed(text):
                            visible_text += visible
                            yield {"type": "delta", "text": visible}
                gen_tokens += int(box.get("new_tokens") or 0)
                keep["cache"] = box.get("cache")

            if handle is not None and handle.cancelled:
                # 停止时不要把缓冲区里的半截标记刷成正文，也不复用这次中途的 KV
                return

            keep["ok"] = True

            for visible in guard.flush():
                visible_text += visible
                yield {"type": "delta", "text": visible}

            calls = guard.parsed_calls()
            if calls:
                yield {"type": "tool_calls", "calls": calls}

            # 输出侧按「思考 / 正文 / 工具调用」拆开，前端用量面板直接用这些数
            reasoning_tokens = len(engine.tokenize(reasoning_text)) if reasoning_text else 0
            content_tokens = len(engine.tokenize(visible_text)) if visible_text else 0
            tool_call_tokens = 0
            if calls:
                tool_call_tokens = len(
                    engine.tokenize(
                        json.dumps([c.to_openai() for c in calls], ensure_ascii=False)
                    )
                )
            counted = reasoning_tokens + content_tokens + tool_call_tokens
            # gen_tokens 含模板特殊符号，比三项之和略多，差额单独记为 other
            total_out = max(gen_tokens, counted)
            yield {
                "type": "usage",
                "prompt_tokens": prompt_tokens,
                "reused_tokens": reused,
                "prefill_tokens": max(0, prompt_tokens - reused),
                "cache_hit": reused >= MIN_REUSE_TOKENS,
                "completion_tokens": total_out,
                "reasoning_tokens": reasoning_tokens,
                "content_tokens": content_tokens,
                "tool_call_tokens": tool_call_tokens,
                "other_tokens": max(0, total_out - counted),
                "duration": time.perf_counter() - started,
            }
        finally:
            # 成功就把这份前缀留着给下一轮用，失败就丢掉，避免留下脏 KV
            cache = keep.get("cache")
            if keep.get("ok") and cache is not None:
                try:
                    engine.release_prefix(conv_key, tokens, cache)
                except Exception:  # noqa: BLE001
                    engine.drop_prefix(conv_key)
            elif cache is None:
                engine.drop_prefix(conv_key)
