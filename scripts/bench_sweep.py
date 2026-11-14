"""线程数 / 注意力后端 / 量化 的快速扫描。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import torch

BASE = Path(__file__).resolve().parent.parent
MODEL = BASE / "models" / "Qwen" / "Qwen3-0___6B"

from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

tok = AutoTokenizer.from_pretrained(str(MODEL), trust_remote_code=True)
print("mkldnn", torch.backends.mkldnn.is_available())

filler = "工具调用需要读取文件内容并执行命令，工作区路径固定在项目目录下。上下文压缩会保留最近若干轮对话。"
text = (filler * 300)[:12000]
ids_all = tok(text, return_tensors="pt", add_special_tokens=False)["input_ids"]
print("corpus", ids_all.shape[1])


def measure(model, ctx=512, n_new=16, prefill_n=256):
    ids = ids_all[:, :ctx]
    with torch.inference_mode():
        t0 = time.perf_counter()
        out = model(ids[:, :prefill_n], use_cache=True)
        tp = time.perf_counter() - t0
        # 续 prefill 到 ctx
        t0 = time.perf_counter()
        out = model(ids[:, prefill_n:], past_key_values=out.past_key_values, use_cache=True)
        tc = time.perf_counter() - t0
        cache = out.past_key_values
        nxt = out.logits[:, -1:, :].argmax(-1)
        t0 = time.perf_counter()
        for _ in range(n_new):
            o = model(nxt, past_key_values=cache, use_cache=True)
            cache = o.past_key_values
            nxt = o.logits[:, -1:, :].argmax(-1)
        td = time.perf_counter() - t0
    return prefill_n / tp, (ctx - prefill_n) / tc, n_new / td


for attn in ("sdpa", "eager"):
    try:
        model = AutoModelForCausalLM.from_pretrained(
            str(MODEL), dtype=torch.float32, low_cpu_mem_usage=True,
            attn_implementation=attn,
        )
        model.eval()
    except Exception as exc:  # noqa: BLE001
        print(f"attn={attn} load failed: {exc}")
        continue
    print(f"\n=== attn={attn} impl={model.config._attn_implementation} ===")
    for nt in (4, 6, 8, 12, 14):
        torch.set_num_threads(nt)
        p, c, d = measure(model)
        print(f" threads={nt:2d}  prefill {p:7.1f} tok/s   decode {d:6.2f} tok/s")
    del model

# 量化
print("\n=== int8 dynamic quant ===")
torch.set_num_threads(8)
model = AutoModelForCausalLM.from_pretrained(
    str(MODEL), dtype=torch.float32, low_cpu_mem_usage=True, attn_implementation="sdpa"
)
model.eval()
from torch.ao.quantization import quantize_dynamic

t0 = time.perf_counter()
qmodel = quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8)
print(f" quantize {time.perf_counter() - t0:.1f}s")
qmodel.eval()
p, c, d = measure(qmodel)
print(f" threads= 8  prefill {p:7.1f} tok/s   decode {d:6.2f} tok/s")

# bf16 对照（AVX2 机器上大概率更慢）
print("\n=== bf16 ===")
torch.set_num_threads(8)
m16 = AutoModelForCausalLM.from_pretrained(
    str(MODEL), dtype=torch.bfloat16, low_cpu_mem_usage=True, attn_implementation="sdpa"
)
m16.eval()
p, c, d = measure(m16)
print(f" threads= 8  prefill {p:7.1f} tok/s   decode {d:6.2f} tok/s")
