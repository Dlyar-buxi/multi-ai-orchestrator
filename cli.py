"""多模型网页版自动协同框架 - 命令行入口（Firefox 双实例版）

用法示例:
  python cli.py login                          # 首次：打开两个 Firefox 实例和全部站点，人工登录一次
  python cli.py doctor                         # 体检：双实例/各站点登录态/Ollama/Git
  python cli.py ask deepseek "你好"            # 单发给某个站点
  python cli.py broadcast "方案A和方案B哪个好"  # 广播给全部（或 --to deepseek,kimi）
  python cli.py debate "..." --proponent chatgpt --critic claude
  python cli.py pipeline "做一个XX项目" --to deepseek,kimi,chatgpt
  python cli.py newrepo 新项目名 --desc "描述" [--public] [--push]   # GitHub 新建仓库（默认 private）
"""
import argparse
import asyncio
import inspect
import os

import yaml
from rich.console import Console
from rich.panel import Panel

from agents.registry import Registry
from browser.pool import BrowserPool
from core.models import Reply
from core.orchestrator import Orchestrator
from gitops.sync import GitSync

console = Console()
ROOT = os.path.dirname(os.path.abspath(__file__))


def load_settings() -> dict:
    with open(os.path.join(ROOT, "config", "settings.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_orchestrator(settings: dict) -> Orchestrator:
    return Orchestrator(
        BrowserPool(settings),
        Registry(os.path.join(ROOT, "config", "adapters")),
        settings,
    )


def save_output(name: str, content: str) -> str:
    out_dir = os.path.join(ROOT, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def show_replies(replies: dict[str, Reply]):
    for name, r in replies.items():
        if r.ok:
            console.print(Panel(r.text, title=f"[green]{name}[/] ({r.elapsed}s)"))
        else:
            console.print(Panel(f"[red]{r.error}[/]", title=f"[red]{name} 失败[/]"))


# ---------------- 命令 ----------------

async def cmd_login(args):
    """首次使用：启动两个 Firefox 实例（隐私版/正常版），打开全部站点标签页，
    轮询检测各站点登录态，全部就绪后自动保存退出；也可等 --wait 超时。"""
    settings = load_settings()
    orch = build_orchestrator(settings)
    reg = orch.registry
    await orch.pool.start()

    for name in reg.names():
        adapter = reg.get(name)
        page = await orch.pool.ensure_page(adapter)
        print(f"  [{adapter.group}] {name:<9} -> {page.url}")

    console.print(Panel(
        "请在两个 Firefox 窗口中完成各站点登录\n"
        "  隐私版窗口: ChatGPT / Claude\n"
        "  正常版窗口: 豆包 / DeepSeek / Qwen / Kimi / 元宝\n"
        f"框架每 5 秒自动检测，全部就绪或 {args.wait}s 超时后自动保存",
        title="[cyan]人工登录[/]",
    ))

    def login_state() -> dict[str, bool]:
        """login_hints: URL 中出现这些片段 = 尚未登录"""
        out = {}
        for name in reg.names():
            a = reg.get(name)
            page = orch.pool.find_page(a)
            url = page.url if page else ""
            hints = a.__dict__.get("login_hints") or []
            out[name] = page is not None and not any(h in url for h in hints)
        return out

    elapsed = 0
    states = {}
    # 仅当所有站点都能通过 URL 自动判定且全部通过时才提前结束；
    # 存在无法判定的站点（如 chatgpt 登录前后 URL 不变）则等满 --wait
    detectable = [n for n in reg.names() if reg.get(n).__dict__.get("login_hints")]
    all_detectable = len(detectable) == len(reg.names())
    while elapsed < args.wait:
        await asyncio.sleep(5)
        elapsed += 5
        states = login_state()
        if all_detectable and all(states.values()):
            break
        pend = [n for n, ok in states.items() if not ok]
        if pend:
            console.print(f"  [{elapsed:>3}s] 待登录: {', '.join(pend)}")

    await orch.pool.stop()
    ok = [n for n, s in states.items() if s]
    pend = [n for n, s in states.items() if not s]
    console.print(f"[green]已保存登录态:[/] {', '.join(ok)}")
    if pend:
        console.print(f"[yellow]未确认（可随时重跑 login 继续补登）:[/] {', '.join(pend)}")


async def cmd_doctor(args):
    settings = load_settings()
    orch = build_orchestrator(settings)

    console.print("[bold]1. Ollama[/]", end=" ")
    console.print("[green]OK[/]" if orch.ollama.available()
                  else "[red]不可达[/]（任务分解/汇总将降级为拼接模式）")

    console.print(f"[bold]2. Firefox 双实例[/] (engine={orch.pool.engine})")
    try:
        await orch.pool.start()
        for group, ctx in orch.pool.contexts.items():
            console.print(f"   [green]{group} 实例已启动[/] 标签页 {len(ctx.pages)} 个")
        console.print("[bold]3. 站点归属[/]")
        reg = orch.registry
        for name in reg.names():
            a = reg.get(name)
            hit = orch.pool.find_page(a) is not None
            console.print(
                f"   [{'green' if hit else 'yellow'}] {name:<9} "
                f"实例={a.group:<8} {'已打开' if hit else '未打开(自动新建)'}"
            )
        await orch.pool.stop()
    except Exception as e:
        console.print(f"[red]启动失败: {e}[/]")
        console.print("请先运行: python -m playwright install firefox")

    git = GitSync(settings)
    console.print("[bold]4. Git[/]", end=" ")
    console.print("[green]仓库就绪[/]" if git.has_repo() else "[red]不是 git 仓库[/]")


async def cmd_ask(args):
    settings = load_settings()
    orch = build_orchestrator(settings)
    await orch.pool.start()
    try:
        r = await orch.ask_one(args.name, args.prompt)
        show_replies({args.name: r})
    finally:
        await orch.pool.stop()


async def cmd_broadcast(args):
    settings = load_settings()
    orch = build_orchestrator(settings)
    await orch.pool.start()
    try:
        replies = await orch.broadcast(args.prompt, args.to)
        show_replies(replies)
        md = f"# 广播任务\n\n{args.prompt}\n\n"
        md += "\n\n".join(
            f"## {n}\n\n{r.text if r.ok else f'失败: {r.error}'}"
            for n, r in replies.items()
        )
        path = save_output(f"broadcast_{args.prompt[:12]}", md)
        console.print(f"已保存: {path}")
    finally:
        await orch.pool.stop()


async def cmd_debate(args):
    settings = load_settings()
    orch = build_orchestrator(settings)
    await orch.pool.start()
    try:
        final = await orch.debate(args.prompt, args.proponent, args.critic, args.rounds)
        console.print(Panel(final, title="[cyan]辩论结论[/]"))
        save_output(f"debate_{args.proponent}_vs_{args.critic}", final)
    finally:
        await orch.pool.stop()


async def cmd_pipeline(args):
    settings = load_settings()
    orch = build_orchestrator(settings)
    await orch.pool.start()
    try:
        final = await orch.pipeline(args.task, args.to)
        console.print(Panel(final, title="[cyan]流水线产出[/]"))
        path = save_output("pipeline_result", final)
        console.print(f"已保存: {path}")
    finally:
        await orch.pool.stop()
    GitSync(settings).run(f"pipeline 产出: {args.task[:50]}")


# ---------------- 项目会话（多轮迭代） ----------------

def _proj(args):
    from core.project import Project
    return Project.load(args.name)


async def cmd_project(args):
    from core.project import Project

    settings = load_settings()

    if args.action == "list":
        names = Project.list_all()
        console.print("\n".join(names) if names else "（暂无项目，用 project new 创建）")
        return

    if args.action == "new":
        p = Project.create(args.name, args.extra or "")
        console.print(f"[green]项目已创建[/]: projects/{p.name}/")
        console.print(f"需求: {args.extra}")
        return

    if args.action == "status":
        p = _proj(args)
        console.print("\n".join(p.status_lines()))
        return

    if args.action == "pick":
        p = _proj(args)
        r = p.pick(int(args.extra))
        console.print(f"[green]当前版本已切换[/] -> 第{r['n']}轮 [{r['agent']}] {r['file']}")
        return

    # ask / iterate：带项目上下文发给模型
    p = _proj(args)
    orch = build_orchestrator(settings)
    await orch.pool.start()
    try:
        prompt = p.build_prompt(args.instruction)
        if args.action == "ask":
            r = await orch.ask_one(args.extra, prompt)
            show_replies({args.extra: r})
            if r.ok:
                rec = p.record(args.extra, args.instruction, r.text)
                console.print(f"[green]已记录[/]: 第{rec['n']}轮 (当前版本)")
        else:  # iterate
            names = args.to or orch.registry.names()
            replies = await orch.broadcast(prompt, names)
            show_replies(replies)
            for n_, r in replies.items():
                if r.ok:
                    rec = p.record(n_, args.instruction, r.text)
                    console.print(f"[green]{n_} 已记录[/]: 第{rec['n']}轮")
            console.print("用 [cyan]project pick <轮次号>[/] 选定权威版本，下轮迭代将基于它")
    finally:
        await orch.pool.stop()


# ---------------- 入口 ----------------

async def cmd_probe(args):
    """全站校准：逐站发送测试消息，输出 输入框命中selector/回复抽取/失败原因 诊断表"""
    from rich.table import Table
    from browser.driver import WebAgentDriver

    settings = load_settings()
    settings["browser"]["reply_timeout"] = 120  # 校准用短超时
    orch = build_orchestrator(settings)
    reg = orch.registry
    await orch.pool.start()
    names = args.to or reg.names()

    async def probe_one(name):
        a = reg.get(name)
        drv = WebAgentDriver(orch.pool, a, settings)
        r = {"name": name, "group": a.group, "input": "-", "reply": "-", "error": ""}
        try:
            page = await orch.pool.ensure_page(a)
            hints = a.__dict__.get("login_hints") or []
            if any(h in page.url for h in hints):
                r["error"] = f"未登录({page.url[:40]})"
                return r
            r["reply"] = (await drv.ask("只回复两个字：收到"))[:24]
            r["input"] = drv.last_hit_selector or "?"
        except Exception as e:
            if "未找到元素" in str(e):
                r["input"] = "全部失效"
                r["error"] = str(e)[:80]
            else:
                r["error"] = f"{type(e).__name__}: {str(e)[:70]}"
        return r

    results = await asyncio.gather(*(probe_one(n) for n in names))

    t = Table(title="站点校准诊断")
    t.add_column("站点")
    t.add_column("实例")
    t.add_column("输入框命中")
    t.add_column("回复抽取")
    t.add_column("问题")
    for r in results:
        ok = not r["error"]
        t.add_row(
            f"[green]{r['name']}[/]" if ok else f"[red]{r['name']}[/]",
            r["group"], r["input"], r["reply"], r["error"],
        )
    console.print(t)
    await orch.pool.stop()


def cmd_newrepo(args):
    """在 GitHub 上新建仓库；--push 把当前项目推上去"""
    from gitops.github import GitHubClient, GitHubError

    settings = load_settings()
    gh = GitHubClient(settings)
    if not gh.ready:
        console.print(
            f"[red]缺少 token[/]：把 GitHub PAT 写入 [cyan]github_token.txt[/] "
            f"（见 gitops/github.py 头部说明），或设置环境变量 GITHUB_TOKEN"
        )
        raise SystemExit(1)
    user = gh.me()  # 验证 token 有效性
    try:
        repo = gh.create_repo(
            args.name,
            private=not args.public,
            description=args.desc or "",
            auto_init=args.init,
        )
    except GitHubError as e:
        console.print(f"[red]创建失败[/]: {e}")
        raise SystemExit(1)
    vis = "private" if repo.get("private") else "public"
    console.print(f"[green]仓库已创建[/] ({vis}): {repo['html_url']}")
    if args.push:
        gitsync = GitSync(settings)
        rc, out = gitsync._git("remote", "add", "origin", repo["clone_url"])
        if rc != 0:
            gitsync._git("remote", "set-url", "origin", repo["clone_url"])
        gitsync._git("add", ".")  # 首推整库（.gitignore 红线自动过滤隐私）
        gitsync._git("commit", "-m", f"init: {args.name}")
        rc, out = gitsync._git("push", "-u", "origin", gitsync.branch)
        print(out[:400] if rc == 0 else f"[red]push 失败: {out}[/]")


def main():
    p = argparse.ArgumentParser(description="多模型网页版自动协同框架")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("newrepo", help="在 GitHub 上新建仓库（默认 private）")
    s.add_argument("name", help="仓库名")
    s.add_argument("--public", action="store_true", help="建公开仓库（默认 private）")
    s.add_argument("--desc", default="", help="仓库描述")
    s.add_argument("--init", action="store_true", help="带 README 初始化")
    s.add_argument("--push", action="store_true", help="把当前项目推上去")
    s.set_defaults(fn=cmd_newrepo)

    s = sub.add_parser("login", help="首次使用：打开双 Firefox 实例与全部站点，人工登录")
    s.add_argument("--wait", type=int, default=600, help="等待登录秒数，默认 600")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("probe", help="全站校准诊断（发送测试消息，检查链路）")
    s.add_argument("--to", nargs="*", help="站点名列表，缺省为全部")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("doctor", help="环境体检")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("ask", help="单发一个站点")
    s.add_argument("name")
    s.add_argument("prompt")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("broadcast", help="广播给多个站点")
    s.add_argument("prompt")
    s.add_argument("--to", nargs="*", help="站点名列表，缺省为全部")
    s.set_defaults(fn=cmd_broadcast)

    s = sub.add_parser("debate", help="两个站点交叉辩论")
    s.add_argument("prompt")
    s.add_argument("--proponent", required=True)
    s.add_argument("--critic", required=True)
    s.add_argument("--rounds", type=int, default=1)
    s.set_defaults(fn=cmd_debate)

    s = sub.add_parser("pipeline", help="任务分解→分派→汇总")
    s.add_argument("task")
    s.add_argument("--to", nargs="*")
    s.set_defaults(fn=cmd_pipeline)

    s = sub.add_parser("project", help="项目会话：new/list/status/pick/ask/iterate（多轮迭代）")
    s.add_argument("action", choices=["new", "list", "status", "pick", "ask", "iterate"])
    s.add_argument("name", nargs="?", help="项目名（new/status/pick/ask/iterate 需要）")
    s.add_argument("extra", nargs="?", help="new=需求文本; pick=轮次号; ask=模型名")
    s.add_argument("instruction", nargs="?", help="ask/iterate 的本轮指示")
    s.add_argument("--to", nargs="*", help="iterate 的目标模型列表，缺省全部")
    s.set_defaults(fn=cmd_project)

    args = p.parse_args()
    if inspect.iscoroutinefunction(args.fn):
        asyncio.run(args.fn(args))
    else:
        args.fn(args)


if __name__ == "__main__":
    main()
