"""Token 用量面板的端到端检查：打开、展开、明暗主题截图。

用法：python scripts/ui_usage_check.py
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

URL = "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parent.parent / "data" / "shots"


def api(path: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return json.loads(opener.open(URL + path, timeout=30).read())


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 920})
        page.on(
            "console",
            lambda m: errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_timeout(1500)
        await page.click("#openUsage")
        await page.wait_for_timeout(1800)

        print("概览卡片：", (await page.inner_text("#usageCards")).replace("\n", " | "))
        print("输入图例：", (await page.inner_text("#usageInLegend")).replace("\n", " "))
        print("输出图例：", (await page.inner_text("#usageOutLegend")).replace("\n", " "))

        rows = await page.query_selector_all("#usageTurns .usage-row.body")
        print("轮次行数：", len(rows))
        if rows:
            await rows[0].click()
            await page.wait_for_timeout(400)
        if len(rows) > 1:
            # 最后一轮通常带工具调用，展开确认工具结果预览
            await rows[-1].click()
            await page.wait_for_timeout(400)
            steps = await page.query_selector_all("#usageTurns .usage-step")
            print("最后一轮步骤数：", len(steps))
            for node in steps[-5:]:
                print("   ", (await node.inner_text()).replace("\n", " "))
        await page.screenshot(path=str(OUT / "07_usage_light.png"))

        # 滚到底看工具表
        await page.eval_on_selector("#usageBody", "n => (n.scrollTop = n.scrollHeight)")
        await page.wait_for_timeout(500)
        await page.screenshot(path=str(OUT / "08_usage_tools.png"))

        # 暗色
        await page.click("#usageBack")
        await page.wait_for_timeout(300)
        await page.click("#themeToggle")
        await page.wait_for_timeout(500)
        await page.click("#openUsage")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(OUT / "09_usage_dark.png"))

        # 尺寸检查：有没有横向溢出
        overflow = await page.evaluate(
            """() => {
            const body = document.querySelector('#usageBody');
            const table = document.querySelector('#usageTurns');
            return { bodyScroll: body.scrollWidth, bodyClient: body.clientWidth,
                     tableScroll: table.scrollWidth, tableClient: table.clientWidth };
        }"""
        )
        print("溢出检查：", overflow)
        await browser.close()

    print("控制台错误：", json.dumps(errors, ensure_ascii=False) if errors else "无")


if __name__ == "__main__":
    asyncio.run(main())
