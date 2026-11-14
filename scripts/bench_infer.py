"""本地推理性能基准：定位 prefill / decode 瓶颈，对比 dtype 与量化。

用法（llm-run 环境）：
    python scripts/bench_infer.py            # 完整基准
    python scripts/bench_infer.py --quick    # 只测关键几项
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import torch

BASE = Path(__file__).resolve().parent.parent
MODEL = BASE / "models" / "Qwen" / "Qwen3-0___6B"


def _timed(fn, repeat: int = 3) -> float:
    vals = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        vals.append(time.perf_counter() - t0)
    return statistics.median(vals)


def bench_prefill(model, input_ids, n_tokens: int) -> float:
    """返回 prefill 的 tok/s。"""
    ids = input_ids[:, :n_tokens]
    with torch.inference_mode():
        model(ids, use_cache=True)

        def run():
            model(ids, use_cache=True)

        dt = _timed(run)
    return n_tokens / dt


def bench_decode(model, input_ids, ctx_len: int, n_new: int = 24) -> float:
    """返回 decode 的 tok/s（带 KV cache，逐步解码）。"""
    ids = input_ids[:, :ctx_len]
    with torch.inference_mode():
        out = model(ids, use_cache=True)
        cache = out.past_key_values
        nxt = out.logits[:, -1:, :].argmax(-1)

        def run():
            nonlocal cache, nxt
            t0 = time.perf_counter()
            for _ in range(n_new):
                o = model(nxt, past_key_values=cache, use_cache=True)
                cache = o.past_key_values
                nxt = o.logits[:, -1:, :].argmax(-1)
            return time.perf_counter() - t0

        dt = run()
    return n_new / dt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--threads", type=str, default="")
    ap.add_argument("--dtype", type=str, default="float32")
    ap.add_argument("--quant", action="store_true")
    ap.add_argument("--json", type=str, default="")
    args = ap.parse_args()

    if args.threads:
        n = int(args.threads)
        torch.set_num_threads(n)

    from transformers import AutoModelForCausalLM, AutoTokenizer

    print(f"torch={torch.__version__} cap={torch.backends.cpu.get_cpu_capability()} "
          f"threads={torch.get_num_threads()} dtype={args.dtype} quant={args.quant}")

    tok = AutoTokenizer.from_pretrained(str(MODEL), trust_remote_code=True)
    t0 = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(
        str(MODEL), dtype=getattr(torch, args.dtype), low_cpu_mem_usage=True
    )
    model.eval()
    load_s = time.perf_counter() - t0
    print(f"load {load_s:.1f}s")

    if args.quant:
        from torch.ao.quantization import quantize_dynamic

        t0 = time.perf_counter()
        model = quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8)
        model.eval()
        print(f"quantize {time.perf_counter() - t0:.1f}s")

    # 用真实语料构造长文本
    filler = (
        "工具调用需要读取文件内容并执行命令，工作区路径固定在项目目录下。"
        "上下文压缩会保留最近的若干轮对话以及一份摘要。"
    )
    long_text = (filler * 400)[:20000]
    enc = tok(long_text, return_tensors="pt", add_special_tokens=False)
    print(f"corpus tokens: {enc['input_ids'].shape[1]}")

    result = {"dtype": args.dtype, "quant": args.quant,
              "threads": torch.get_num_threads(), "load_s": round(load_s, 2)}

    ctx_lens = [256, 1024, 3072] if args.quick else [256, 1024, 2048, 3072, 4096]
    print("\n-- prefill (tok/s) --")
    for n in ctx_lens:
        if n > enc["input_ids"].shape[1]:
            continue
        r = bench_prefill(model, enc["input_ids"], n)
        result[f"prefill_{n}"] = round(r, 1)
        print(f"  ctx={n:5d}  {r:8.1f} tok/s  ({n / r * 1000:6.0f} ms)")

    print("\n-- decode (tok/s) --")
    for n in ([512, 3072] if args.quick else [512, 1024, 3072]):
        if n > enc["input_ids"].shape[1]:
            continue
        r = bench_decode(model, enc["input_ids"], n)
        result[f"decode_{n}"] = round(r, 1)
        print(f"  ctx={n:5d}  {r:8.1f} tok/s  ({1000 / r:5.0f} ms/token)")

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
