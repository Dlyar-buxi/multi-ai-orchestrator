"""通用网页驱动：与具体站点解耦，行为完全由 adapter yaml 配置
流程: 定位输入框 → 注入 prompt → 发送 → 等待新回复块出现且文本稳定 → 抽取
"""
import asyncio
import re
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
        """发送一轮对话。标签页优先级: 已开会话 > 记忆会话URL(上下文延续) > 新会话。
        会话页内继续发消息时 URL 不变, baseline=当前历史块数, 判定照常工作。"""
        page = await self.pool.ensure_page(self.adapter)
        # 不 bring_to_front，避免自动化期间反复抢占你正在用的窗口焦点

        url_before = page.url
        baseline = await self._count_assistant_blocks(page)
        await self._send(page, prompt)
        # SPA 跳转到新会话页是异步的：观察 3s，URL 变了说明开新会话，块数从零计
        jumped = False
        for _ in range(6):
            await asyncio.sleep(0.5)
            if page.url != url_before:
                jumped = True
                break
        if jumped:
            baseline = 0
        await self._wait_reply(page, baseline)
        text = await self._extract(page)
        # 成功回复后记住会话 URL，下一轮直接回到本会话继续（上下文原生延续）
        self.pool.remember_session(self.adapter, page.url)
        return text

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

    # 回复区常见噪声文本（反馈条/推荐追问/时间戳），抽取时跳过
    NOISE_RE = re.compile(
        r"^(you are providing feedback|claude responded:|正在提供反馈|请提供反馈|复制|重新生成|收起|展开|"
        r"分享|点赞|点踩|举报|继续|换一个|换一批|今天 \d{1,2}:\d{2}|昨天 \d{1,2}:\d{2}|"
        r"\d{4}-\d{2}-\d{2})",
        re.IGNORECASE,
    )

    async def _last_nonempty_text(self, page: Page) -> str:
        """倒序找第一个可见、非空且非噪声的候选块（豆包等站点最后一块是恒空占位行）"""
        blocks = page.locator(self.adapter.assistant_selectors[0])
        count = await blocks.count()
        for i in range(count - 1, max(-1, count - 15), -1):  # 最多回看14块，防超时
            el = blocks.nth(i)
            try:
                if not await el.is_visible():
                    continue
                text = (await el.inner_text()).strip()
                if text and not self.NOISE_RE.match(text[:60]):
                    return text
            except PWTimeout:
                continue
        return ""

    # ---------- 模式切换 ----------
    async def _ensure_mode(self, page: Page):
        """发送前切换到目标模式。
        两段式（mode_trigger+mode_text）：下拉菜单型切换。触发器显示当前模式名，
        因此目标模式文本可见 = 已激活；不可见则点开菜单再选项，Escape 兜底收起。
        开关式（mode_selectors/mode_text）：aria 按压态=true 跳过；找不到自动跳过。"""
        ad = self.adapter.__dict__
        trigger = ad.get("mode_trigger")
        text = ad.get("mode_text")
        if trigger and text:
            await self._switch_dropdown_mode(page, trigger, text)
            return
        selectors = list(ad.get("mode_selectors") or [])
        if text and not selectors:
            selectors = [f"text={text}"]
        if not selectors:
            return
        for sel in selectors:
            loc = page.locator(sel)
            try:
                n = await loc.count()
            except PWTimeout:
                continue
            for i in range(min(n, 5)):
                el = loc.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    state = await el.evaluate(
                        """e => {
                            let x = e;
                            for (let d = 0; x && d < 4; d++, x = x.parentElement) {
                                const p = x.getAttribute('aria-pressed') ?? x.getAttribute('aria-checked');
                                if (p === 'true') return true;
                                if (p === 'false') return false;
                            }
                            return null;
                        }"""
                    )
                    if state is True:  # 已激活
                        return
                    await el.click()
                    await asyncio.sleep(0.6)
                    return
                except PWTimeout:
                    continue

    async def _switch_dropdown_mode(self, page: Page, trigger: str, option: str):
        opt = page.locator(f"text={option}")
        # 目标模式可见 = 已激活（触发器显示当前模式名）
        try:
            n = await opt.count()
        except PWTimeout:
            return
        for i in range(min(n, 5)):
            try:
                if await opt.nth(i).is_visible():
                    return
            except PWTimeout:
                continue
        # 点开下拉菜单
        try:
            await page.locator(f"text={trigger}").first.click(timeout=5000)
            await asyncio.sleep(0.8)
        except PWTimeout:
            return
        # 在弹层中点目标选项
        try:
            n = await opt.count()
            for i in range(min(n, 5)):
                el = opt.nth(i)
                try:
                    if await el.is_visible():
                        await el.click()
                        await asyncio.sleep(0.6)
                        return
                except PWTimeout:
                    continue
        except PWTimeout:
            pass
        await page.keyboard.press("Escape")  # 兜底收起，防残留弹层挡输入框

    # ---------- 发送 ----------
    async def _send(self, page: Page, prompt: str):
        box = await self._locate(page, self.adapter.input_selectors)
        try:
            await box.click(timeout=8000)
        except PWTimeout:  # 被遮挡/动画中时退化为聚焦
            await box.focus()
        await self._ensure_mode(page)  # 聚焦后模式按钮才渲染
        # insert_text 直接写入剪贴板式文本，支持中文/换行，比逐字 type 快
        await page.keyboard.insert_text(prompt)
        await asyncio.sleep(0.3)

        # 优先点发送按钮，点不动（遮挡）或找不到则回车兜底
        try:
            btn = await self._locate(page, self.adapter.send_selectors, timeout=3000)
            try:
                await btn.click(timeout=5000)
            except PWTimeout:
                await page.keyboard.press(self.adapter.send_key)
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
