"""端到端测量：真实对话提示词下，前缀缓存带来的首字延迟变化。

用法：python scripts/bench_turn.py [--no-cache] [--int8]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.llm import get_provider, reset_providers  # noqa: E402
from app.llm.base import RunHandle  # noqa: E402
from app.settings import get_model_profile, load_config, save_config  # noqa: E402
from app.tools import schema_list  # noqa: E402

QUESTIONS = [
    "你好，简单介绍一下你自己。",
    "在上面基础上，用一句话说明你能帮我做什么。",
    "再补一句：你运行在哪台机器上？",
]


async def one_turn(provider, messages, params, tools) -> dict:
    handle = RunHandle()
    t0 = time.perf_counter()
    ttft = None
    text = ""
    usage = {}
    async for ev in provider.stream(messages, tools, params, handle):
        if ev["type"] == "first_token":
            ttft = ev["ttft"]
        elif ev["type"] == "delta":
            text += ev["text"]
            if ttft is None:
                ttft = time.perf_counter() - t0
        elif ev["type"] == "usage":
            usage = ev
    return {
        "ttft": round(ttft or 0, 2),
        "total": round(time.perf_counter() - t0, 2),
        "text": text[:60].replace("\n", " "),
        **{k: usage.get(k) for k in ("prompt_tokens", "reused_tokens", "prefill_tokens")},
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--int8", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    perf = dict(cfg.get("perf") or {})
    perf["prefix_cache"] = not args.no_cache
    perf["int8"] = args.int8
    perf["cache_tokens"] = 8192
    save_config({**cfg, "perf": perf})
    reset_providers()

    profile = get_model_profile(None)
    provider = get_provider(profile)
    params = {
        "conversation_id": "bench-conv",
        "thinking": False,
        "thinking_level": "off",
        "max_tokens": 40,
        "temperature": 0.0,  # 贪心，便于对比一致性
        "top_p": 0.95,
    }
    tools = schema_list([])
    system = cfg["defaults"]["system_prompt"]
    messages = [{"role": "system", "content": system}]

    print(f"prefix_cache={perf['prefix_cache']} int8={perf['int8']}")
    print(f"tools={len(tools)} 个\n")
    for i, q in enumerate(QUESTIONS, 1):
        messages.append({"role": "user", "content": q})
        r = await one_turn(provider, messages, params, tools)
        print(
            f"轮次 {i}: 首字 {r['ttft']:5.2f}s  总计 {r['total']:5.2f}s  "
            f"提示词 {r['prompt_tokens']} tok（复用 {r['reused_tokens']}，"
            f"新读入 {r['prefill_tokens']}）"
        )
        print(f"         {r['text']}")
        messages.append({"role": "assistant", "content": r["text"]})


if __name__ == "__main__":
    asyncio.run(main())
