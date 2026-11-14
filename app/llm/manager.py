"""推理后端工厂。"""

from __future__ import annotations

from typing import Any

from .base import BaseProvider
from .hf_local import LocalHFProvider
from .openai_compat import OpenAICompatProvider

_PROVIDERS: dict[str, BaseProvider] = {}

PROVIDER_KINDS = [
    {"value": "local_hf", "label": "本地 transformers（进程内推理）"},
    {"value": "openai_compat", "label": "OpenAI 兼容端点（LM Studio / Ollama / vLLM）"},
]


def get_provider(profile: dict[str, Any]) -> BaseProvider:
    pid = profile.get("id") or "default"
    signature = (
        pid,
        profile.get("provider"),
        profile.get("model_path"),
        profile.get("base_url"),
        profile.get("model"),
        profile.get("api_key"),
        profile.get("device"),
        profile.get("dtype"),
        profile.get("enable_thinking"),
    )
    cached = _PROVIDERS.get(pid)
    if cached is not None and getattr(cached, "_signature", None) == signature:
        return cached

    kind = profile.get("provider") or "openai_compat"
    if kind == "local_hf":
        provider: BaseProvider = LocalHFProvider(profile)
    else:
        provider = OpenAICompatProvider(profile)
    provider._signature = signature  # type: ignore[attr-defined]
    _PROVIDERS[pid] = provider
    return provider


def reset_providers() -> None:
    _PROVIDERS.clear()
