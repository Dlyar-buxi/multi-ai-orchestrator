"""通用网页驱动：与具体站点解耦，行为完全由 adapter yaml 配置
流程: 定位输入框 → 注入 prompt → 发送 → 等待新回复块出现且文本稳定 → 抽取
"""
import asyncio
import time

from playwright.async_api import Locator, Page, TimeoutError as PWTimeout

from agents.registry import Adapter
from browser.pool import BrowserPool


class WebAgentDriver:
    def __init__(self, pool: BrowserPool, adapter: Adapter, settings: dict):
        self.pool = pool
        self.adapter = adapter
        b = settings["browser"]
        self.stability_ms = b.get("stability_ms", 2500)
        self.poll_ms = b.get("poll_interval_ms", 800)
        self.reply_timeout = b.get("reply_timeout", 300)

    # ---------- 对外主流程 ----------
    async def ask(self, prompt: str) -> str:
        page = await self.pool.ensure_page(self.adapter)
        # 不 bring_to_front，避免自动化期间反复抢占你正在用的窗口焦点

        baseline = await self._count_assistant_blocks(page)
        url_before = page.url
        await self._send(page, prompt)
        # 站点可能因新消息跳转新会话页（如豆包），块数会重置，baseline 失效
        if page.url != url_before:
            baseline = 0
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PWTimeout:
                pass
        await self._wait_reply(page, baseline)
        return await self._extract(page)

    # ---------- 定位 ----------
    async def _locate(self, page: Page, selectors: list[str], timeout: int = 20000) -> Locator:
        self.last_hit_selector = None
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            for sel in selectors:
                loc = page.locator(sel).last
                try:
                    if await loc.count() and await loc.is_visible():
                        self.last_hit_selector = sel
                        return loc
                except PWTimeout:
                    continue
            await asyncio.sleep(0.3)
        raise RuntimeError(
            f"[{self.adapter.name}] 未找到元素，候选选择器全部失效: {selectors}"
        )

    async def _count_assistant_blocks(self, page: Page) -> int:
        try:
            loc = page.locator(self.adapter.assistant_selectors[0])
            return await loc.count()
        except PWTimeout:
            return 0

    async def _last_nonempty_text(self, page: Page) -> str:
        """倒序找第一个可见且非空的候选块（豆包等站点最后一块是恒空占位行）"""
        blocks = page.locator(self.adapter.assistant_selectors[0])
        count = await blocks.count()
        for i in range(count - 1, max(-1, count - 15), -1):  # 最多回看14块，防超时
            el = blocks.nth(i)
            try:
                if not await el.is_visible():
                    continue
                text = (await el.inner_text()).strip()
                if text:
                    return text
            except PWTimeout:
                continue
        return ""

    # ---------- 发送 ----------
    async def _send(self, page: Page, prompt: str):
        box = await self._locate(page, self.adapter.input_selectors)
        await box.click()
        # insert_text 直接写入剪贴板式文本，支持中文/换行，比逐字 type 快
        await page.keyboard.insert_text(prompt)
        await asyncio.sleep(0.3)

        # 优先点发送按钮，失败则回车
        try:
            btn = await self._locate(page, self.adapter.send_selectors, timeout=3000)
            await btn.click()
        except RuntimeError:
            await page.keyboard.press(self.adapter.send_key)

    # ---------- 等待回复完成 ----------
    async def _wait_reply(self, page: Page, baseline: int):
        """判定完成 = 出现新的助手消息块，且其文本连续 stability_ms 无变化"""
        start = time.time()
        stable_deadline = self.stability_ms / 1000
        last_text, stable_since = "", None

        while time.time() - start < self.reply_timeout:
            count = await page.locator(self.adapter.assistant_selectors[0]).count()
            if count > baseline:
                text = await self._last_nonempty_text(page)
                if text and text == last_text:
                    if stable_since and time.time() - stable_since >= stable_deadline:
                        return
                elif text:
                    stable_since = time.time()
                    last_text = text
            await asyncio.sleep(self.poll_ms / 1000)
        raise TimeoutError(f"[{self.adapter.name}] 等待回复超时({self.reply_timeout}s)")

    # ---------- 抽取 ----------
    async def _extract(self, page: Page) -> str:
        text = await self._last_nonempty_text(page)
        if not text:
            raise RuntimeError(f"[{self.adapter.name}] 页面上没有可抽取的回复")
        return text
