"""编排器：单发 / 广播 / 辩论 / 流水线 四种协作模式"""
import asyncio
import json
import time

from agents.registry import Registry
from browser.driver import WebAgentDriver
from browser.pool import BrowserPool
from core.aggregator import summarize
from core.models import Reply, SessionLog
from local_llm.ollama_client import OllamaClient

DECOMPOSE_PROMPT = """你是项目经理。把下面的任务分解成若干子任务，并指派给最合适的 AI 助手。
可用助手: {names}
只输出 JSON 数组，格式: [{{"subtask": "...", "assignee": "助手名"}}]，不要输出其他内容。

任务: {task}"""


class Orchestrator:
    def __init__(self, pool: BrowserPool, registry: Registry, settings: dict):
        self.pool = pool
        self.registry = registry
        self.settings = settings
        b = settings["ollama"]
        self.ollama = OllamaClient(b["base_url"], b["model"], b.get("enabled", True))
        self.log = SessionLog(settings.get("log_file", "./logs/session.jsonl"))

    def _driver(self, name: str) -> WebAgentDriver:
        return WebAgentDriver(self.pool, self.registry.get(name), self.settings)

    async def ask_one(self, name: str, prompt: str) -> Reply:
        r = Reply(agent=name, prompt=prompt)
        t0 = time.time()
        try:
            r.text = await self._driver(name).ask(prompt)
            r.ok = True
        except Exception as e:
            r.error = f"{type(e).__name__}: {e}"
        r.elapsed = round(time.time() - t0, 1)
        self.log.write_reply(r)
        return r

    async def broadcast(self, prompt: str, names: list[str] | None = None) -> dict[str, Reply]:
        """同一问题并发发给多个网页模型（每个站点各一个标签页，互不干扰）"""
        names = names or self.registry.names()
        replies = await asyncio.gather(
            *(self.ask_one(n, prompt) for n in names), return_exceptions=True
        )
        out = {}
        for n, r in zip(names, replies):
            out[n] = r if isinstance(r, Reply) else Reply(
                agent=n, prompt=prompt, ok=False, error=str(r)
            )
        return out

    async def debate(self, prompt: str, proponent: str, critic: str, rounds: int = 1) -> str:
        """提出者作答 → 评审者挑毛病 → 本地模型合并出最终结论"""
        r1 = await self.ask_one(proponent, prompt)
        r2 = await self.ask_one(
            critic,
            f"评审以下方案，指出错误、遗漏和风险，给出改进建议：\n\n任务：{prompt}\n\n方案：\n{r1.text}",
        )
        return await summarize(
            prompt, {"proponent": r1, "critic": r2}, self.ollama
        )

    async def pipeline(self, task: str, names: list[str] | None = None) -> str:
        """本地模型分解任务 → 分派给网页模型执行 → 汇总成文档"""
        names = names or self.registry.names()
        plan = await self._decompose(task, names)

        replies: dict[str, Reply] = {}
        if plan:
            for item in plan:
                sub, who = item["subtask"], item.get("assignee")
                if who not in names:
                    who = names[0]
                replies[who] = await self.ask_one(who, sub)
        else:  # 分解失败退化为广播
            replies = await self.broadcast(task, names)

        final = await summarize(task, replies, self.ollama)
        self.log.write({"kind": "pipeline", "task": task, "plan": plan, "final": final})
        return final

    async def _decompose(self, task: str, names: list[str]) -> list[dict] | None:
        if not self.ollama.available():
            return None
        raw = await self.ollama.chat(
            DECOMPOSE_PROMPT.format(names=", ".join(names), task=task),
            system="只输出合法 JSON，不要解释。",
        )
        try:
            plan = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
            return plan if isinstance(plan, list) and plan else None
        except (ValueError, json.JSONDecodeError):
            return None
