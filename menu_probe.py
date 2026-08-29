"""点开菜单触发器后再扫模式元素（下拉/面板内的选项）
用法: python menu_probe.py
"""
import asyncio

from cli import build_orchestrator, load_settings
from inspect_dom import MODE_PROBE_JS

# 站点 -> 要点击的触发器文本（None=强制点击输入框）
TASKS = {
    "qwen": "Qwen3.7-Plus",          # 模型选择器
    "doubao": "技能",                 # 技能·连接器·伙伴 面板
    "yuanbao": None,                 # 输入框 force 聚焦
}


async def main():
    settings = load_settings()
    orch = build_orchestrator(settings)
    reg = orch.registry
    await orch.pool.start()
    try:
        for name, trigger in TASKS.items():
            a = reg.get(name)
            page = await orch.pool.ensure_page(a)
            print(f"\n=== {name} ===")
            try:
                if trigger:
                    t = page.locator(f"text={trigger}").first
                    await t.click(timeout=8000)
                else:
                    box = page.locator(a.input_selectors[0]).first
                    try:
                        await box.click(timeout=5000)
                    except Exception:
                        await box.click(timeout=5000, force=True)
                await asyncio.sleep(1.2)
                modes = await page.evaluate(MODE_PROBE_JS)
                print("  -- 点击后菜单/模式元素 --")
                for m in modes:
                    print(f"    <{m['tag']}> y={m['y']} aria/role={m['pressed']!r} 文本={m['text']!r} cls={m['cls']!r}")
                if not trigger:
                    continue
                # 有弹层就按 Escape 收起，避免残留
                await page.keyboard.press("Escape")
            except Exception as e:
                print(f"  失败: {str(e)[:120]}")
    finally:
        await orch.pool.stop()


asyncio.run(main())
