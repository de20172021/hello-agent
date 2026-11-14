"""UI 冒烟测试：打开页面、设置里的性能面板、发一条消息看统计。

用法：python scripts/ui_check.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

URL = "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parent.parent / "data" / "shots"


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(OUT / "01_main.png"), full_page=False)

        # 打开设置 → 性能
        await page.click("#openSettings")
        await page.wait_for_timeout(600)
        await page.click('#settingsTabs .tab[data-tab="perf"]')
        await page.wait_for_timeout(1200)
        perf_text = await page.inner_text("#perfStats")
        print("性能面板：", perf_text.replace("\n", " | "))
        await page.screenshot(path=str(OUT / "02_perf.png"))

        # 保存一次（不改值），验证写入链路
        await page.click("#perfSaveBtn")
        await page.wait_for_timeout(800)

        # MCP 面板里的内联开关
        await page.click('#settingsTabs .tab[data-tab="mcp"]')
        await page.wait_for_timeout(800)
        inline = await page.is_checked("#mcpInline")
        print("MCP 内联开关 =", inline)
        await page.screenshot(path=str(OUT / "03_mcp.png"))

        await page.click('[data-close="settingsModal"]')
        await page.wait_for_timeout(400)

        # 发一条消息
        await page.fill("#input", "用一句话说明你能做什么")
        await page.keyboard.press("Enter")
        try:
            await page.wait_for_selector(".msg.assistant .stat", timeout=180_000)
        except Exception as exc:  # noqa: BLE001
            print("等待回复超时：", exc)
        await page.wait_for_timeout(1500)
        stats = await page.inner_text(".msg.assistant .stat") if await page.query_selector(".msg.assistant .stat") else "(无)"
        print("消息统计：", stats)
        await page.screenshot(path=str(OUT / "04_chat.png"))

        # 明暗主题
        await page.click("#themeToggle") if await page.query_selector("#themeToggle") else None
        await page.wait_for_timeout(600)
        await page.screenshot(path=str(OUT / "05_dark.png"))

        await browser.close()

    print("控制台错误：", json.dumps(errors, ensure_ascii=False) if errors else "无")


if __name__ == "__main__":
    asyncio.run(main())
