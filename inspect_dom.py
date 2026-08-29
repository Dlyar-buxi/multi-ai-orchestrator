"""DOM 侦察工具：自动枚举各站点真实的输入框候选与消息容器候选
用法: python inspect_dom.py [--send] [站点名...]   # --send: 发送测试消息后再侦察回复DOM
"""
import asyncio
import sys

from cli import build_orchestrator, load_settings

INPUT_PROBE_JS = """
() => {
  const out = [];
  document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]').forEach(el => {
    const r = el.getBoundingClientRect();
    out.push({
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      cls: String(el.className).slice(0, 90),
      testid: el.getAttribute('data-testid') || '',
      visible: r.width > 5 && r.height > 5,
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 30),
    });
  });
  return out;
}
"""

CONTAINER_PROBE_JS = """
() => {
  // 递归穿透 shadow DOM，枚举含实际文本的叶子块（消息回复的常见形态）
  const results = [];
  const walk = (root) => {
    let els;
    try { els = root.querySelectorAll('*'); } catch { return; }
    for (const el of els) {
      if (['STYLE','SCRIPT','NOSCRIPT'].includes(el.tagName)) continue;
      if (el.shadowRoot) walk(el.shadowRoot);
      const r = el.getBoundingClientRect();
      if (r.width < 80 || r.height < 12) continue;
      const own = [...el.childNodes].filter(n => n.nodeType === 3)
        .map(n => n.textContent.trim()).join('');
      if (own.length < 6) continue;
      results.push({
        tag: el.tagName.toLowerCase(),
        cls: String(el.className).slice(0, 70),
        testid: el.getAttribute('data-testid') || el.getAttribute('data-message-id') || '',
        text: own.slice(0, 36),
      });
    }
  };
  walk(document);
  return results.slice(0, 45);
}
"""


MODE_PROBE_JS = """
() => {
  // 深度文本扫描：找含模式关键词的最深层可见文本元素（含下拉菜单内选项）
  const kws = ['思考','think','深度','turbo','swarm','k3','expert','专家','云端','电脑','电脑','智能体','agent','research','调研','联网','flash','pro','模型'];
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let node;
  while ((node = walker.nextNode())) {
    if (node.shadowRoot) {
      // shadow DOM 内递归由 querySelectorAll 分支处理，此处跳过
      continue;
    }
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) continue;
    const own = Array.from(node.childNodes)
      .filter(n => n.nodeType === 3)
      .map(n => n.textContent.trim()).join(' ');
    if (!own) continue;
    const low = own.toLowerCase();
    if (!kws.some(k => low.includes(k))) continue;
    const r = node.getBoundingClientRect();
    if (r.width < 12 || r.height < 8) continue;
    out.push({
      tag: node.tagName.toLowerCase(),
      text: own.slice(0, 40),
      pressed: node.getAttribute('aria-pressed') || node.getAttribute('aria-checked') || node.getAttribute('role') || '',
      cls: String(node.className).slice(0, 50),
      y: Math.round(r.top),
    });
  }
  const seen = new Set();
  return out.filter(x => { const k = x.tag + '|' + x.text;
    if (seen.has(k)) return false; seen.add(k); return true; }).slice(0, 40);
}
"""


async def main(names, send=False):
    settings = load_settings()
    orch = build_orchestrator(settings)
    reg = orch.registry
    await orch.pool.start()
    try:
        for name in (names or reg.names()):
            a = reg.get(name)
            print(f"\n{'='*70}\n[{name}] 实例={a.group}")
            try:
                page = await orch.pool.ensure_page(a)
                await page.wait_for_load_state("networkidle", timeout=30000)
                await asyncio.sleep(3)  # SPA 渲染缓冲
                print(f"  URL: {page.url[:90]}")
                print(f"  标题: {await page.title()}")

                if send:  # 发送测试消息，等回复出现后再侦察
                    from browser.driver import WebAgentDriver
                    drv = WebAgentDriver(orch.pool, a, settings)
                    await drv._send(page, "只回复两个字：收到")
                    await asyncio.sleep(15)
                    print(f"  发送后 URL: {page.url[:90]}")

                inputs = await page.evaluate(INPUT_PROBE_JS)
                print("  -- 输入框候选 --")
                for el in inputs:
                    mark = "可见" if el["visible"] else "隐藏"
                    print(f"    <{el['tag']}> id={el['id']!r} testid={el['testid']!r} "
                          f"cls={el['cls']!r} [{mark}] ph={el['placeholder']!r}")
                if not inputs:
                    print("    (无任何输入元素——可能未登录或页面未加载完)")
                containers = await page.evaluate(CONTAINER_PROBE_JS)
                print("  -- 文本叶子块（含 shadow DOM）--")
                for c in containers:
                    print(f"    <{c['tag']}> testid={c['testid']!r} cls={c['cls']!r} 文本={c['text']!r}")
                if send:  # 逐个验证 yaml 里的 assistant 候选
                    print("  -- assistant 候选命中数 --")
                    for sel in a.assistant_selectors:
                        try:
                            cnt = await page.locator(sel).count()
                            print(f"    {cnt:>2} 个 <- {sel}")
                        except Exception as e:
                            print(f"    ERR <- {sel}: {str(e)[:60]}")
                modes = await page.evaluate(MODE_PROBE_JS)
                print("  -- 模式/模型按钮 --")
                for m in modes:
                    print(f"    <{m['tag']}> 按压态={m['pressed']!r} 文本={m['text']!r} cls={m['cls']!r}")
            except Exception as e:
                print(f"  侦察失败: {type(e).__name__}: {str(e)[:100]}")
    finally:
        await orch.pool.stop()


if __name__ == "__main__":
    args = sys.argv[1:]
    send = "--send" in args
    names = [a for a in args if not a.startswith("--")]
    asyncio.run(main(names, send))
