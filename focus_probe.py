"""聚焦输入框后再扫模式按钮（很多站点的模式条聚焦后才渲染）
用法: python focus_probe.py [站点名...]
"""
import asyncio
import sys

from cli import build_orchestrator, load_settings
from inspect_dom import MODE_PROBE_JS


async def main():
    names = sys.argv[1:] or ["doubao", "qwen", "yuanbao", "kimi"]
    settings = load_settings()
    orch = build_orchestrator(settings)
    reg = orch.registry
    await orch.pool.start()
    try:
        for name in names:
            a = reg.get(name)
            page = await orch.pool.ensure_page(a)
            print(f"\n=== {name} ===")
            try:
                box = await orch.driver._locate(
                    page, a.input_selectors, timeout=5000
                ) if hasattr(orch, "driver") else None
            except Exception:
                from browser.driver import Driver
                drv = Driver(a, orch.pool)
                box = await drv._locate(page, a.input_selectors, timeout=5000)
            if not box:
                box = page.locator(a.input_selectors[0]).first
            try:
                await box.click(timeout=8000)
            except Exception as e:
                print(f"  聚焦失败: {str(e)[:60]}")
                continue
            await asyncio.sleep(1.0)  # 等模式条渲染
            modes = await page.evaluate(MODE_PROBE_JS)
            print("  -- 聚焦后模式/模型元素 --")
            for m in modes:
                print(f"    <{m['tag']}> y={m['y']} aria/role={m['pressed']!r} 文本={m['text']!r} cls={m['cls']!r}")
    finally:
        await orch.pool.stop()


asyncio.run(main())
