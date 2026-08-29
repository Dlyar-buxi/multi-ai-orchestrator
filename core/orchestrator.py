"""编排器：单发 / 广播 / 辩论 / 流水线 四种协作模式"""
import asyncio
import json
import time

from agents.registry import Registry
from browser.driver import WebAgentDriver
from browser.pool import BrowserPool
from core.aggregator import summarize
from core.models import Reply, SessionLog
from core.pipeline import PipelineEngine
from local_llm.cloud_client import CloudClient
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
        ollama = OllamaClient(b["base_url"], b["model"], b.get("enabled", True))
        c = settings.get("coordinator", {})
        provider = c.get("provider", "auto")  # auto | ollama | cloud
        cloud = CloudClient(
            c.get("api_key_file", "zhipu_key.txt"),
            c.get("base_url", "https://open.bigmodel.cn/api/paas/v4"),
            c.get("model", "glm-4-flash"),
        ) if provider in ("auto", "cloud") else None
        # provider 选择: cloud 优先(有key时)；显式 ollama 则只用本地；cloud 无 key 自动回退 ollama
        if provider == "ollama":
            self.ollama = ollama
        elif cloud and cloud.available():
            self.ollama = cloud
        else:
            self.ollama = ollama
        self.log = SessionLog(settings.get("log_file", "./logs/session.jsonl"))

    def _driver(self, name: str) -> WebAgentDriver:
        return WebAgentDriver(self.pool, self.registry.get(name), self.settings)

    async def ask_one(self, name: str, prompt: str) -> Reply:
        """单发；带 prompt 长度摘要记录到会话日志。同一命令内复用 driver → 无需关页面。"""
        r = Reply(agent=name, prompt=prompt[:200] + ("..." if len(prompt) > 200 else ""))
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

    async def pipeline(self, task: str, names: list[str] | None = None,
                       project_dir: str | None = None) -> str:
        """五阶段协同 pipeline: kickoff→decompose→produce→review→revise 循环直至收敛"""
        names = names or self.registry.names()
        engine = PipelineEngine(self, self.settings, self.registry, self.pool, self.ollama)
        result = await engine.run(task, names, project_dir=project_dir)
        self.log.write({"kind": "pipeline", "task": task, "framework_v": result.framework_version,
                        "rounds": result.review_rounds, "issues": result.final_issue_count})
        return result.to_markdown()

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
