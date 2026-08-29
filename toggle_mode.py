"""模式按钮实测：定位->打印状态->点击->再打印，用于确定激活态特征
用法: python toggle_mode.py [站点名...]
"""
import asyncio
import sys

from cli import build_orchestrator, load_settings

TEXTS = {
    "deepseek": "DeepThink",
    "qwen": "Thinking",
    "yuanbao": "深度思考",
    "kimi": "K3",
}


async def dump(page, text):
    loc = page.locator(f"text={text}")
    n = await loc.count()
    print(f"  text={text!r} 命中 {n}")
    for i in range(min(n, 4)):
        el = loc.nth(i)
        try:
            vis = await el.is_visible()
            info = await el.evaluate(
                "e => e.tagName + ' aria=' + (e.getAttribute('aria-pressed') ?? e.getAttribute('aria-checked') ?? '-')"
                " + ' cls=' + String(e.className).slice(0, 90)"
            )
            print(f"    [{i}] 可见={vis} {info}")
        except Exception as e:
            print(f"    [{i}] 读取失败: {str(e)[:60]}")


async def main():
    names = sys.argv[1:] or list(TEXTS)
    settings = load_settings()
    orch = build_orchestrator(settings)
    reg = orch.registry
    await orch.pool.start()
    try:
        for name in names:
            a = reg.get(name)
            page = await orch.pool.ensure_page(a)
            print(f"\n=== {name} ===")
            text = TEXTS.get(name)
            if not text:
                print("  无预置模式文本，跳过")
                continue
            await dump(page, text)
            loc = page.locator(f"text={text}")
            clicked = False
            for i in range(await loc.count()):
                if await loc.nth(i).is_visible():
                    await loc.nth(i).click()
                    clicked = True
                    break
            if not clicked:
                print("  无可见按钮可点击")
                continue
            await asyncio.sleep(1.2)
            print("  -- 点击后 --")
            await dump(page, text)
    finally:
        await orch.pool.stop()


asyncio.run(main())
