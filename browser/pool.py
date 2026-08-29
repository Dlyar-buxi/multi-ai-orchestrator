"""浏览器连接池 —— Firefox 双实例版
Playwright 无法附加到已打开的普通 Firefox（CDP 仅支持 Chromium 系），
因此由框架启动两个 Playwright 特制 Firefox 实例（任务栏各一个窗口）：
  private 实例 → ChatGPT / Claude（独立 profile，登录态持久）
  normal  实例 → 其余站点（独立 profile，登录态持久）
每个站点按 adapter.group 路由到对应实例；已存在的标签页优先复用。
"""
import os
from playwright.async_api import async_playwright, BrowserContext, Page

from agents.registry import Adapter


class BrowserPool:
    def __init__(self, settings: dict):
        b = settings["browser"]
        self.engine = b.get("engine", "firefox")
        self.headless = b.get("headless", False)
        self.nav_timeout = (b.get("nav_timeout", 45)) * 1000
        # groups: {"private": {"profile": <dir>, "proxy": <url|null>}, ...}
        self.groups: dict[str, dict] = {
            name: {"profile": os.path.abspath(cfg["profile"]),
                   "proxy": cfg.get("proxy")}
            for name, cfg in b.get(
                "groups", {"normal": {"profile": "./ff_profile", "proxy": None}}
            ).items()
        }
        self._pw = None
        self.contexts: dict[str, BrowserContext] = {}

    async def start(self):
        """启动每个分组的 Firefox 实例（窗口可见，登录态从 profile 恢复）"""
        if self._pw:
            return
        self._pw = await async_playwright().start()
        launcher = getattr(self._pw, self.engine)
        for name, g in self.groups.items():
            os.makedirs(g["profile"], exist_ok=True)
            kwargs = {"headless": self.headless,
                      "viewport": {"width": 1600, "height": 900}}  # 宽视口避免站点切移动端布局
            if g["proxy"]:  # Firefox 不吃系统代理，必须显式指定
                kwargs["proxy"] = {"server": g["proxy"]}
            self.contexts[name] = await launcher.launch_persistent_context(
                g["profile"], **kwargs
            )

    def context_for(self, adapter: Adapter) -> BrowserContext:
        """按适配器分组找到所属浏览器实例"""
        if not self.contexts:
            raise RuntimeError("浏览器实例未启动，请先调用 pool.start()")
        ctx = self.contexts.get(adapter.group)
        if ctx is None:  # 配置里缺 group 时兜底到第一个实例
            ctx = next(iter(self.contexts.values()))
        return ctx

    def find_page(self, adapter: Adapter) -> Page | None:
        """在该实例已打开的标签页里找匹配站点（不新开）"""
        for page in self.context_for(adapter).pages:
            if any(m in page.url for m in adapter.url_match):
                return page
        return None

    async def ensure_page(self, adapter: Adapter) -> Page:
        """复用已开标签页，否则在该实例中新开并导航"""
        page = self.find_page(adapter)
        if page is None:
            page = await self.context_for(adapter).new_page()
            await page.goto(adapter.url, timeout=self.nav_timeout)
        elif "about:blank" in page.url:
            await page.goto(adapter.url, timeout=self.nav_timeout)
        return page

    async def stop(self):
        for ctx in self.contexts.values():
            try:
                await ctx.close()
            except Exception:
                pass
        self.contexts.clear()
        if self._pw:
            await self._pw.stop()
            self._pw = None
