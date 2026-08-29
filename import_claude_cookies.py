"""检查 FirefoxFQ profile 的 claude.ai cookie 并导入框架 private profile
用法: python import_claude_cookies.py            # 检查+导入
"""
import os
import shutil
import sqlite3
import tempfile

FFQ_COOKIES = r"D:\Firefox download\FirefoxFQ\FirefoxFQ\Firefox\Profile\cookies.sqlite"


def read_cookies():
    tmp = os.path.join(tempfile.gettempdir(), "ffq_cookies.db")
    shutil.copy2(FFQ_COOKIES, tmp)
    # Firefox 运行中，未合并的 WAL 里可能有新 cookie，一并复制
    for ext in ("-wal", "-shm"):
        src = FFQ_COOKIES + ext
        if os.path.exists(src):
            shutil.copy2(src, tmp + ext)
    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite "
        "FROM moz_cookies WHERE host LIKE '%claude.ai%'"
    ).fetchall()
    con.close()
    print(f"FirefoxFQ 中 claude.ai cookies: {len(rows)}")
    for r in rows:
        v = r["value"]
        print(f"  {r['name']:<20} len={len(v)} host={r['host']}")
    return rows


def to_pw_cookie(r):
    same_site_map = {0: "None", 1: "Lax", 2: "Strict"}
    c = {
        "name": r["name"],
        "value": r["value"],
        "domain": r["host"],
        "path": r["path"] or "/",
        "expires": r["expiry"] or -1,
        "secure": bool(r["isSecure"]),
        "httpOnly": bool(r["isHttpOnly"]),
    }
    ss = same_site_map.get(r["sameSite"] if r["sameSite"] is not None else 0)
    if ss:
        c["sameSite"] = ss
    return c


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
    finally:
        await pool.stop()  # 关闭时持久化到 profile


def main():
    rows = read_cookies()
    if not rows:
        print("没有可导入的 cookie")
        return
    names = {r["name"] for r in rows}
    if "sessionKey" not in names:
        print("警告: 未发现 sessionKey（可能未登录）")
    cookies = [to_pw_cookie(r) for r in rows]
    import asyncio

    asyncio.run(inject(cookies))


if __name__ == "__main__":
    main()
