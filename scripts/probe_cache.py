"""验证：带 past_key_values 续 prefill / 续生成，结果是否与全量 prefill 完全一致。"""

from __future__ import annotations

import time
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

BASE = Path(__file__).resolve().parent.parent
MODEL = BASE / "models" / "Qwen" / "Qwen3-0___6B"

torch.set_num_threads(8)
tok = AutoTokenizer.from_pretrained(str(MODEL), trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(
    str(MODEL), dtype=torch.float32, low_cpu_mem_usage=True
)
model.eval()
gen_kw = dict(max_new_tokens=10, do_sample=False, pad_token_id=tok.eos_token_id,
              eos_token_id=tok.eos_token_id, repetition_penalty=1.05)

text = (
    "你是 HelloAgent，一个完全运行在用户本机的 AI 智能体。可用工具包括读取文件、执行命令、搜索。"
    "工作区固定在项目目录下。请回答问题：如何统计一个目录下最大的十个文件？"
) * 3
ids = tok(text, add_special_tokens=False, return_tensors="pt")["input_ids"]
L = ids.shape[1]
print("prompt tokens", L)

with torch.inference_mode():
    t0 = time.perf_counter()
    ref = model.generate(ids, **gen_kw)
    t_full = time.perf_counter() - t0
    ref_tail = ref[0, L:].tolist()
print(f"full prefill generate {t_full*1000:.0f}ms -> {ref_tail}")

def continue_gen(cache, new_ids, past_len):
    """从 cache 续 prefill + 生成，返回新 token 列表。"""
    full_len = past_len + new_ids.shape[1]
    mask = torch.ones((1, full_len), dtype=torch.long)
    with torch.inference_mode():
        out = model.generate(
            new_ids, past_key_values=cache, attention_mask=mask, **gen_kw
        )
    return [t for t in out[0].tolist() if t != tok.eos_token_id][:10]

for split in (110, 64, L - 40):
    with torch.inference_mode():
        t0 = time.perf_counter()
        pre = model(ids[:, :split], use_cache=True)
        t_pre = time.perf_counter() - t0
        cache = pre.past_key_values
        past_len = cache.get_seq_length()
    t0 = time.perf_counter()
    tail = continue_gen(cache, ids[:, split:], past_len)
    t_rec = time.perf_counter() - t0
    print(f"split={split:4d} past={past_len} prefill {t_pre*1000:6.0f}ms gen {t_rec*1000:6.0f}ms "
          f"match={tail == ref_tail}")

# 二级续 prefill
with torch.inference_mode():
    o = model(ids[:, :64], use_cache=True)
    o2 = model(ids[:, 64:128], past_key_values=o.past_key_values, use_cache=True)
    c2 = o2.past_key_values
    print("2-step seq_len", c2.get_seq_length())
tail = continue_gen(c2, ids[:, 128:], c2.get_seq_length())
print("2-step match =", tail == ref_tail)

# crop
with torch.inference_mode():
    o = model(ids, use_cache=True)
    c = o.past_key_values
    print("full seq_len", c.get_seq_length(), "-> crop 100")
    c.crop(100)
    print("after crop", c.get_seq_length())
tail = continue_gen(c, ids[:, 100:], c.get_seq_length())
print("crop match =", tail == ref_tail)

# 单 token 续生成（模拟下一轮只有 1 个 token 是新的）
with torch.inference_mode():
    o = model(ids[:, :L - 1], use_cache=True)
    c = o.past_key_values
tail = continue_gen(c, ids[:, L - 1:], c.get_seq_length())
print("1-token continuation match =", tail == ref_tail)
