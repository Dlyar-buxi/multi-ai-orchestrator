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
        print("--- 已发送，等 20s 后 dump 块结构 ---")
        await asyncio.sleep(20)
        blocks = page.locator(a.assistant_selectors[0])
        c = await blocks.count()
        for i in range(c):
            el = blocks.nth(i)
            try:
                cls = await el.get_attribute("class") or ""
                txt = (await el.inner_text()).strip().replace("\n", " ")[:60]
                vis = await el.is_visible()
                box = await el.bounding_box()
                print(f"[{i}] 可见={vis} 尺寸={box and (round(box['width']), round(box['height']))} cls={cls[:70]!r} 文本={txt!r}")
            except Exception as e:
                print(f"[{i}] 读取失败: {str(e)[:60]}")
    finally:
        await orch.pool.stop()


asyncio.run(main())
