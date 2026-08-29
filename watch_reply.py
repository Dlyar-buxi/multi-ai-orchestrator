"""动态观察某站点的回复流：baseline -> send -> 每3秒打印 块数/最后块文本/URL
用法: python watch_reply.py <站点名>
"""
import asyncio
import sys

from cli import build_orchestrator, load_settings
from browser.driver import WebAgentDriver


async def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "doubao"
    settings = load_settings()
    orch = build_orchestrator(settings)
    reg = orch.registry
    await orch.pool.start()
    a = reg.get(name)
    drv = WebAgentDriver(orch.pool, a, settings)
    try:
        page = await orch.pool.ensure_page(a)
        baseline = await drv._count_assistant_blocks(page)
        url0 = page.url
        print(f"URL={url0}  baseline={baseline}  assistant={a.assistant_selectors[0]}")
        await drv._send(page, "只回复两个字：收到")
        print("--- 已发送，开始观察 ---")
        url0 = page.url
        for i in range(24):
            await asyncio.sleep(5)
            blocks = page.locator(a.assistant_selectors[0])
            c = await blocks.count()
            t = (await blocks.nth(c - 1).inner_text()).strip().replace("\n", " ")[:50] if c else ""
            print(f"{i*5+5:>3}s url变={str(page.url != url0):<5} url={page.url[:60]} count={c:<3} last={t!r}")
    finally:
        await orch.pool.stop()


asyncio.run(main())
