"""浏览器连接池 —— Firefox 双实例版
Playwright 无法附加到已打开的普通 Firefox（CDP 仅支持 Chromium 系），
因此由框架启动两个 Playwright 特制 Firefox 实例（任务栏各一个窗口）：
  private 实例 → ChatGPT / Claude（独立 profile，登录态持久）
  normal  实例 → 其余站点（独立 profile，登录态持久）
每个站点按 adapter.group 路由到对应实例；已存在的标签页优先复用。

会话复用: 每个站点记住最近会话页 URL（state/session_urls.json）。
下一轮 ask 直接回到同一会话继续发消息——站点原生记住全部上下文，
无需每轮重开新会话。达到 uses 上限自动无缝开新会话（防 DOM 膨胀卡死）。
"""
import asyncio
import json
import os

from playwright.async_api import async_playwright, BrowserContext, Page

from agents.registry import Adapter

STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "session_urls.json"
)


class BrowserPool:
    def __init__(self, settings: dict):
        b = settings["browser"]
        self.engine = b.get("engine", "firefox")
        self.headless = b.get("headless", False)
        self.nav_timeout = (b.get("nav_timeout", 45)) * 1000
        self.session_reuse = b.get("session_reuse", True)
        self.session_reuse_limit = b.get("session_reuse_limit", 20)
        # groups: {"private": {"profile": <dir>, "proxy": <url|null>}, ...}
        self.groups: dict[str, dict] = {
            name: {"profile": os.path.abspath(cfg["profile"]),
                   "proxy": cfg.get("proxy"),
                   "user_agent": cfg.get("user_agent")}
            for name, cfg in b.get(
                "groups", {"normal": {"profile": "./ff_profile", "proxy": None}}
            ).items()
        }
        self._pw = None
        self.contexts: dict[str, BrowserContext] = {}
        self._sessions = self._load_state()

    # ---------- 会话记忆 ----------
    def _load_state(self) -> dict:
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self):
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(self._sessions, f, ensure_ascii=False, indent=2)

    def remembered_session(self, adapter: Adapter) -> str | None:
        """该站点记忆的会话 URL；超过复用上限则返回 None 并清除（下轮开新会话）"""
        if not self.session_reuse:
            return None
        rec = self._sessions.get(adapter.name)
        if not rec or "url" not in rec:
            return None
        if rec.get("uses", 0) >= self.session_reuse_limit:
            self._sessions.pop(adapter.name, None)
            self._save_state()
            return None
        return rec["url"]

    def remember_session(self, adapter: Adapter, url: str):
        """成功回复后更新会话 URL（driver 在每轮成功后调用）"""
        if not self.session_reuse or "/chat" not in url and "/c/" not in url:
            return  # 只记会话页，不记首页/设置页
        rec = self._sessions.get(adapter.name, {})
        rec["url"] = url
        rec["uses"] = rec.get("uses", 0) + 1
        self._sessions[adapter.name] = rec
        self._save_state()

    def forget_sessions(self):
        """清空全部会话记忆（所有站点下轮开新会话）"""
        self._sessions = {}
        self._save_state()

    # ---------- 生命周期 ----------
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
            if g.get("user_agent"):  # 组级 UA 伪装（配合导入的 cf_clearance）
                kwargs["user_agent"] = g["user_agent"]
            self.contexts[name] = await launcher.launch_persistent_context(
                g["profile"], **kwargs
            )
            # 基础反自动化指纹：隐藏 navigator.webdriver（缓解 Cloudflare/Turnstile 敏感度）
            await self.contexts[name].add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
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
        """标签页优先级: 已开的站点标签 > 记忆的会话URL(恢复原会话) > 站点首页(新会话)"""
        page = self.find_page(adapter)
        if page is not None and "about:blank" not in page.url:
            return page  # 同一命令内多轮：直接复用现成标签页（含会话页）
        # 无现成标签：优先恢复记忆的会话（站点原生上下文延续）
        resume = self.remembered_session(adapter)
        if resume:
            if page is None:
                page = await self.context_for(adapter).new_page()
            try:
                await page.goto(resume, timeout=self.nav_timeout)
                await asyncio.sleep(2.5)  # 等站点异步加载历史消息，baseline 才准
                return page
            except Exception:
                pass  # 会话失效（被删/过期），降级到新会话
        if page is None:
            page = await self.context_for(adapter).new_page()
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
