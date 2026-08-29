"""将 Cookie-Editor 导出的 JSON 注入框架 private profile
用法: python import_claude_cookies.py
"""
import asyncio
import json

JSON_PATH = r"D:\diedai\claude_cookies.json"

SAME_SITE = {
    "no_restriction": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": "Lax",
}


def load_cookies():
    with open(JSON_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    out = []
    for c in raw:
        pw = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure")),
            "httpOnly": bool(c.get("httpOnly")),
        }
        if c.get("session"):
            pw["expires"] = -1
        else:
            pw["expires"] = int(c.get("expirationDate") or -1)
        ss = SAME_SITE.get(str(c.get("sameSite", "")).lower())
        if ss:
            pw["sameSite"] = ss
        out.append(pw)
    return out


async def inject(cookies):
    from cli import load_settings
    from browser.pool import BrowserPool

    settings = load_settings()
    pool = BrowserPool(settings)
    await pool.start()
    try:
        ctx = pool.contexts["private"]
        await ctx.add_cookies(cookies)
        print(f"已注入 {len(cookies)} 个 cookie 到 private profile")
        # 立即验证登录态
        page = await ctx.new_page()
        await page.goto("https://claude.ai/new", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(6)
        print(f"URL: {page.url}")
        print(f"标题: {await page.title()}")
        n_login = await page.locator("text=Log in").count()
        print(f"出现Log in(未登录标志): {n_login}")
    finally:
        await pool.stop()  # 关闭时持久化


def main():
    cookies = load_cookies()
    print(f"从 JSON 读取 {len(cookies)} 个 cookie")
    names = {c["name"] for c in cookies}
    assert "sessionKey" in names, "缺少 sessionKey，导入无意义"
    print("sessionKey 存在 ✓  cf_clearance 存在:",
          "cf_clearance" in names)
    asyncio.run(inject(cookies))


if __name__ == "__main__":
    main()
