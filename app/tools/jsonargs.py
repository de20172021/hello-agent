"""工具参数的宽松解析。

模型给的 `arguments` 字符串经常不是标准 JSON，实际见到的几类：

1. 外面套了代码块围栏：```` ```json {...} ``` ````；
2. JSON 后面还跟了一段重复的碎片（模型复读），json.loads 报 Extra data；
3. 字符串里有未转义的裸换行 / 制表符（写 HTML、Python 代码时最常见）；
4. 用了 Python 字面量：True / False / None、单引号；
5. 输出被 max_tokens 截断，JSON 没有闭合。

前四类可以无损修回来，第五类只能判定「残缺」，由调用方决定怎么处理
（写文件这类操作不能拿半截内容将就，必须让模型分段重写）。
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_fence(text: str) -> str:
    match = _FENCE.match(text)
    if match:
        return match.group(1).strip()
    # 只有开头没有结尾的围栏（被截断时常见）
    if text.lstrip().startswith("```"):
        body = text.lstrip()[3:]
        body = re.sub(r"^[a-zA-Z0-9_-]*\s*\n?", "", body, count=1)
        return body.strip()
    return text


def _escape_controls(text: str) -> str:
    """把字符串里的裸换行/回车/制表符转义，不动结构上的空白。"""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            if ord(ch) < 0x20:
                out.append("\\u%04x" % ord(ch))
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def _py_literals(text: str) -> str:
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b", "null", text)
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return text


def _drop_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _close_truncated(text: str) -> str | None:
    """尝试补全被截断的 JSON：闭合未结束的字符串和括号。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
    if not stack and not in_string:
        return None
    body = text
    if escaped:
        body = body[:-1]
    if in_string:
        body += '"'
    # 末尾停在 "key": 这种半截上，补 null 之外更稳的是直接砍掉
    body = re.sub(r",?\s*\"[^\"\\]*\"\s*:\s*$", "", body)
    body = re.sub(r",\s*$", "", body)
    return body + "".join(reversed(stack))


def loads_lenient(text: Any) -> tuple[Any, dict[str, Any]]:
    """尽量解析成 Python 对象。返回 (值, 说明)。

    说明字段：
    - ok        是否成功
    - repaired  是否经过修复
    - truncated 是否是「被截断后半截补全」得来的（内容不可信）
    - detail    人话描述，可直接回给模型
    """
    info: dict[str, Any] = {"ok": False, "repaired": False, "truncated": False, "detail": ""}
    if text is None:
        return {}, {**info, "ok": True}
    if isinstance(text, (dict, list)):
        return text, {**info, "ok": True}
    if not isinstance(text, str):
        return text, {**info, "ok": True, "detail": "非字符串参数"}

    raw = _strip_fence(text.strip().lstrip("\ufeff"))
    if not raw:
        return {}, {**info, "ok": True, "detail": "参数为空"}

    # 1. 标准解析
    try:
        return json.loads(raw), {**info, "ok": True}
    except json.JSONDecodeError:
        pass

    # 2. 前面有杂字符 / 后面跟着复读碎片：从第一个 { 或 [ 开始 raw_decode
    start = min(
        [i for i in (raw.find("{"), raw.find("[")) if i >= 0],
        default=-1,
    )
    if start > 0:
        raw = raw[start:]
    try:
        value, _end = json.JSONDecoder().raw_decode(raw)
        return value, {
            **info,
            "ok": True,
            "repaired": True,
            "detail": "忽略了 JSON 之后的多余内容",
        }
    except json.JSONDecodeError:
        pass

    # 3. 逐级修复后再试
    candidates = [
        (_escape_controls(raw), "转义了字符串里的换行/制表符"),
        (_drop_trailing_commas(_py_literals(_escape_controls(raw))), "修正了字面量与尾逗号"),
        (_drop_trailing_commas(_py_literals(raw)), "修正了字面量与尾逗号"),
    ]
    for candidate, why in candidates:
        try:
            return json.loads(candidate), {**info, "ok": True, "repaired": True, "detail": why}
        except json.JSONDecodeError:
            continue

    # 4. 单引号 / Python dict 字面量
    for candidate in (raw, _escape_controls(raw)):
        try:
            value = ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, (dict, list)):
            return value, {**info, "ok": True, "repaired": True, "detail": "按 Python 字面量解析"}

    # 5. 截断补全：结构能拼回来，但内容不完整
    for candidate in (raw, _escape_controls(raw)):
        closed = _close_truncated(candidate)
        if not closed:
            continue
        for variant in (closed, _drop_trailing_commas(_py_literals(closed))):
            try:
                value = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(value, (dict, list)):
                return value, {
                    **info,
                    "ok": True,
                    "repaired": True,
                    "truncated": True,
                    "detail": "JSON 被截断，已按残缺内容补全",
                }

    return None, {
        **info,
        "detail": "参数不是合法 JSON，且无法自动修复",
    }
