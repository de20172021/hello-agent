"""把「每天九点」「每周一 10:30」「每隔两小时」这类说法解析成定时规则。

两个用途：
1. 界面上的「一句话创建」——本地解析，不花模型 token，也不怕小模型乱答；
2. 模型工具 `schedule_create` 的 `when` 参数——模型只要把用户原话传进来即可。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

WEEKDAY_LABEL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_CN_DIGIT = {
    "零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_PERIOD = {"凌晨": 0, "早上": 0, "上午": 0, "中午": 12, "下午": 12, "傍晚": 12, "晚上": 12, "夜里": 12}


def _num(token: str) -> int | None:
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    if token == "半":
        return 30
    if token in _CN_DIGIT:
        return _CN_DIGIT[token]
    if len(token) == 2 and token[0] == "十" and token[1] in _CN_DIGIT:
        return 10 + _CN_DIGIT[token[1]]
    if len(token) == 2 and token[1] == "十" and token[0] in _CN_DIGIT:
        return _CN_DIGIT[token[0]] * 10
    if len(token) == 3 and token[1] == "十":
        return _CN_DIGIT.get(token[0], 0) * 10 + _CN_DIGIT.get(token[2], 0)
    return None


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    out = []
    last = 0
    for start, end in sorted(spans):
        if start < last:
            continue
        out.append(text[last:start])
        last = end
    out.append(text[last:])
    cleaned = "".join(out)
    cleaned = re.sub(r"^[\s,，。、;；:：\-—~～]+", "", cleaned)
    cleaned = re.sub(r"[\s,，。、;；:：]+$", "", cleaned)
    return cleaned.strip()


def _find_time(text: str) -> tuple[int, int, str] | None:
    """返回 (hour, minute, 匹配片段)；支持 9:30 / 9点 / 9点30 / 晚上8点。"""
    pattern = re.compile(
        r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?\s*"
        r"(\d{1,2}|[零一两二三四五六七八九十]{1,3})\s*"
        r"(?:[:：]\s*(\d{1,2})|点\s*(\d{1,2})?\s*分?|时\s*(\d{1,2})?\s*分?)"
    )
    match = pattern.search(text)
    if not match:
        return None
    period, hour_raw, m1, m2, m3 = match.groups()
    hour = _num(hour_raw)
    minute = _num(m1 or m2 or m3 or "0") or 0
    if hour is None:
        return None
    if period:
        if period in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute, match.group(0)


def parse(text: str, now: float | None = None) -> dict[str, Any]:
    """解析一句话，返回 {ok, kind, ..., prompt, summary}；失败时 ok=False + hint。"""
    now = time.time() if now is None else now
    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "hint": "先说一句完整的话，例如「每天 9:00 帮我总结昨天的新闻」"}
    work = raw.replace("：", ":").replace("　", " ")
    spans: list[tuple[int, int]] = []

    def add_span(match: re.Match) -> None:
        spans.append((match.start(), match.end()))

    # ---- 间隔型 ----
    match = re.search(r"每(?:隔)?\s*(半|\d+|[一两二三四五六七八九十]{1,3})\s*分钟", work)
    if match:
        count = _num(match.group(1))
        if count:
            add_span(match)
            return _finish("every", work, spans, raw, every_minutes=max(1, count))
    match = re.search(r"每(?:隔)?\s*(半|\d+|[一两二三四五六七八九十]{1,3})\s*个?\s*小时", work)
    if match:
        # 「每半小时」是 30 分钟，不是 30 小时
        count = 30 if match.group(1) == "半" else _num(match.group(1))
        if count:
            add_span(match)
            minutes = 30 if match.group(1) == "半" else max(1, count * 60)
            return _finish("every", work, spans, raw, every_minutes=max(1, minutes))
    match = re.search(r"每(?:个)?\s*小时", work)
    if match:
        add_span(match)
        return _finish("every", work, spans, raw, every_minutes=60)

    # ---- 每周 ----
    match = re.search(r"每(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", work)
    if match:
        token = match.group(1)
        weekday = (int(token) - 1) if token.isdigit() else _CN_NUM.get(token, 0) - 1
        add_span(match)
        found = _find_time(work)
        if found is None:
            return {"ok": False, "hint": "每周的任务需要写明时间，例如「每周一 9:00」"}
        spans.append((work.find(found[2]), work.find(found[2]) + len(found[2])))
        return _finish(
            "weekly", work, spans, raw,
            time_of_day=f"{found[0]:02d}:{found[1]:02d}",
            weekday=max(0, min(6, weekday)),
        )

    # ---- 每天 ----
    match = re.search(r"每(?:天|日)|天天|每个工作日|工作日", work)
    if match:
        add_span(match)
        found = _find_time(work)
        if found is None:
            return {"ok": False, "hint": "每天的任务需要写明时间，例如「每天 9:00」"}
        spans.append((work.find(found[2]), work.find(found[2]) + len(found[2])))
        return _finish("daily", work, spans, raw, time_of_day=f"{found[0]:02d}:{found[1]:02d}")

    # ---- 一次性（今天 / 明天 / 后天 / 具体日期）----
    base: datetime | None = None
    match = re.search(r"(今天|明天|后天)", work)
    if match:
        base = datetime.fromtimestamp(now) + timedelta(days={"今天": 0, "明天": 1, "后天": 2}[match.group(1)])
        add_span(match)
    else:
        match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?", work)
        if match:
            try:
                base = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                return {"ok": False, "hint": "日期看起来不合法"}
            add_span(match)
        else:
            match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", work)
            if match:
                year = datetime.fromtimestamp(now).year
                try:
                    base = datetime(year, int(match.group(1)), int(match.group(2)))
                except ValueError:
                    return {"ok": False, "hint": "日期看起来不合法"}
                if base.timestamp() < now - 86400:
                    base = base.replace(year=year + 1)
                add_span(match)

    if base is not None:
        found = _find_time(work)
        if found is None:
            return {"ok": False, "hint": "一次性任务需要写明时间，例如「明天 9:30」"}
        spans.append((work.find(found[2]), work.find(found[2]) + len(found[2])))
        at = base.replace(hour=found[0], minute=found[1], second=0, microsecond=0)
        if at.timestamp() <= now:
            return {"ok": False, "hint": "这个时间已经过去了，换个未来的时间？"}
        return _finish("once", work, spans, raw, at=at.timestamp())

    return {
        "ok": False,
        "hint": "没看清重复规则。可以这样说：每天 9:00 / 每周一 10:30 / 每隔 2 小时 / 明天 15:00",
    }


def _finish(kind: str, work: str, spans: list[tuple[int, int]], raw: str, **fields: Any) -> dict[str, Any]:
    prompt = _strip_spans(work, spans)
    if len(prompt) < 2:
        return {
            "ok": False,
            "hint": "时间看懂了，但没看出要做什么。把任务写在时间后面，例如「每天 9:00 整理昨天的日报」",
        }
    result: dict[str, Any] = {"ok": True, "kind": kind, "prompt": prompt, **fields}
    result["summary"] = describe(result)
    return result


def describe(schedule: dict[str, Any]) -> str:
    kind = schedule.get("kind")
    if kind == "once":
        at = schedule.get("at")
        if not at:
            return "一次性（时间未设置）"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(at)))
    if kind == "every":
        minutes = int(schedule.get("every_minutes") or 60)
        if minutes % 60 == 0:
            return f"每 {minutes // 60} 小时"
        return f"每 {minutes} 分钟"
    if kind == "daily":
        return f"每天 {schedule.get('time_of_day') or '09:00'}"
    if kind == "weekly":
        weekday = int(schedule.get("weekday") or 0)
        return f"每{WEEKDAY_LABEL[max(0, min(6, weekday))]} {schedule.get('time_of_day') or '09:00'}"
    return "未设置"


def next_run(schedule: dict[str, Any], now: float | None = None) -> float | None:
    """算下一次触发时间；返回 None 表示不再触发（一次性任务已过期）。"""
    now = time.time() if now is None else now
    kind = schedule.get("kind")
    if kind == "once":
        at = schedule.get("at")
        if not at:
            return None
        at = float(at)
        return at if at > now else None
    if kind == "every":
        minutes = max(1, int(schedule.get("every_minutes") or 60))
        return now + minutes * 60
    if kind in ("daily", "weekly"):
        try:
            hour, minute = (schedule.get("time_of_day") or "09:00").split(":")[:2]
            hour, minute = int(hour), int(minute)
        except (ValueError, AttributeError):
            hour, minute = 9, 0
        current = datetime.fromtimestamp(now)
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if kind == "daily":
            if target.timestamp() <= now:
                target += timedelta(days=1)
            return target.timestamp()
        weekday = int(schedule.get("weekday") or 0)
        ahead = (weekday - current.weekday()) % 7
        target += timedelta(days=ahead)
        if target.timestamp() <= now:
            target += timedelta(days=7)
        return target.timestamp()
    return None
