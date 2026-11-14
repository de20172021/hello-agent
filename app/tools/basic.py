"""基础工具：时间、计算、联网抓取、天气。"""

from __future__ import annotations

import ast
import datetime as _dt
import json
import math
import operator
import re
import unicodedata

import httpx

from .registry import tool


# ---------------------------------------------------------------- 时间

@tool(
    name="get_current_time",
    description=(
        "获取当前日期、时间和星期。"
        "用户问现在几点、今天几号、算「还有几天」时用。系统提示里给的时间可能不是最新，跨天时以这个工具为准。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA 时区名，如 Asia/Shanghai、UTC；留空用本机时区",
            }
        },
    },
    category="基础",
)
def get_current_time(timezone: str = "") -> str:
    if timezone:
        try:
            from zoneinfo import ZoneInfo

            now = _dt.datetime.now(ZoneInfo(timezone))
        except Exception:  # noqa: BLE001
            now = _dt.datetime.now()
            timezone = "本机时区"
    else:
        now = _dt.datetime.now()
        timezone = "本机时区"
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return (
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} {weekdays[now.weekday()]}"
        f"（{timezone}，UTC{now.strftime('%z') or '+00:00'}）"
    )


# ---------------------------------------------------------------- 计算

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_ALLOWED_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "floor": math.floor,
    "ceil": math.ceil,
    "factorial": math.factorial,
    "pow": math.pow,
    "radians": math.radians,
    "degrees": math.degrees,
}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("只支持数字常量")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        func = _ALLOWED_FUNCS.get(node.func.id)
        if func is None:
            raise ValueError(f"不支持的函数：{node.func.id}")
        return func(*[_eval_node(arg) for arg in node.args])
    raise ValueError("表达式包含不支持的语法")


@tool(
    name="calculate",
    description=(
        "精确计算数学表达式，避免心算出错。支持 + - * / // % ** 和 sqrt/log/sin/cos/round 等函数，"
        "常量 pi、e；^ 会当成幂。不支持变量、赋值和中文单位（先换算成纯数字）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "纯数学表达式，例如 (1234*5+6)/7"}
        },
        "required": ["expression"],
    },
    category="基础",
)
def calculate(expression: str) -> str:
    expr = (expression or "").strip()
    if len(expr) > 500:
        raise ValueError("表达式过长")
    expr = expr.replace("^", "**").replace("×", "*").replace("÷", "/")
    parsed = ast.parse(expr, mode="eval")
    result = _eval_node(parsed)
    if isinstance(result, float) and result.is_integer() and abs(result) < 1e16:
        result = int(result)
    return f"{expression.strip()} = {result}"


# ---------------------------------------------------------------- 网络

@tool(
    name="fetch_url",
    description=(
        "抓取网页或接口的文本（自动去掉 HTML 标签）。查最新信息、读在线文档时用。"
        "只支持 http/https；返回纯文本，单页默认最多 4000 字符。"
        "需要更多内容就调大 max_chars 再抓一次。部分站点有反爬，会返回 HTTP 4xx。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整网址，必须以 http:// 或 https:// 开头"},
            "max_chars": {
                "type": "integer",
                "description": "最多返回多少字符，默认 4000，上限 20000",
                "default": 4000,
            },
        },
        "required": ["url"],
    },
    category="网络",
)
def fetch_url(url: str, max_chars: int = 4000) -> str:
    target = (url or "").strip()
    if not re.match(r"^https?://", target):
        return "只支持 http/https 地址"
    try:
        with httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 HelloAgent/0.1"},
        ) as client:
            resp = client.get(target)
    except httpx.HTTPError as exc:
        return f"请求失败：{exc}"
    if resp.status_code >= 400:
        return f"HTTP {resp.status_code}"
    content_type = resp.headers.get("content-type", "")
    body = resp.text
    if "html" in content_type or body.lstrip().startswith("<"):
        body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        body = html_unescape(body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    limit = max(500, min(int(max_chars or 4000), 20000))
    if len(body) > limit:
        body = body[:limit] + f"\n\n…（已截断，原始长度 {len(body)} 字符）"
    return body or "（页面无可读文本）"


def html_unescape(text: str) -> str:
    import html

    return unicodedata.normalize("NFKC", html.unescape(text))


@tool(
    name="get_weather",
    description=(
        "查询某个城市当前的天气（数据来自 wttr.in，需联网）。"
        "只给实时天气，不给未来预报。城市名用中文或英文都行，重名城市建议加国家，如 Paris,FR。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名，例如 北京 / 上海 / Tokyo"}
        },
        "required": ["city"],
    },
    category="网络",
)
def get_weather(city: str) -> str:
    name = (city or "").strip()
    if not name:
        return "请提供城市名称"
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            resp = client.get(
                f"https://wttr.in/{name}",
                params={"format": "j1", "lang": "zh"},
                headers={"User-Agent": "curl/8.0"},
            )
        if resp.status_code >= 400:
            return f"天气服务返回 HTTP {resp.status_code}"
        data = resp.json()
        current = (data.get("current_condition") or [{}])[0]
        desc = (current.get("lang_zh") or current.get("weatherDesc") or [{}])[0].get("value", "")
        return json.dumps(
            {
                "城市": name,
                "天气": desc,
                "气温": f"{current.get('temp_C')}°C",
                "体感": f"{current.get('FeelsLikeC')}°C",
                "湿度": f"{current.get('humidity')}%",
                "风速": f"{current.get('windspeedKmph')} km/h",
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"天气查询失败（可能没有联网）：{type(exc).__name__}: {exc}"
