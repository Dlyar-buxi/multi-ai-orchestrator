"""五阶段协同 pipeline：解决用户两大痛点
    1) 分派时不带背景 → 统一框架+4段式分派prompt(需求/框架/子任务/他子任务概览)
    2) 串行等待关页面   → 组级并发(normal/private独立worker,组内串行)+组间并行
    3) 模型间有鸿沟     → 讨论会(全体交叉评审→协调员合并→统一框架+1版本→修订迭代→收敛自动停)

五阶段循环:
    PHASE1 KICKOFF   全员讨论统一框架v0.1(技术栈/目录/命名/模块边界/数据流)
    PHASE2 DECOMPOSE 协调员分解子任务(指派+交付物)
    PHASE3 PRODUCE   子任务并行产出(每个worker带足4段背景)
    PHASE4 REVIEW    讨论会: 所有产出→发给所有模型交叉评审→协调员合并"冲突/缺漏/接口"
        → 若问题数≤阈值: 收敛,退出循环
    PHASE5 REVISE    每个模型根据评审意见+新版本统一框架,修订自己的交付物
        → 回到 PHASE4, 直到收敛或达到最大循环次数
"""
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict

from agents.registry import Registry, Adapter
from browser.pool import BrowserPool
from core.models import Reply, SessionLog
from local_llm.cloud_client import CloudClient
from local_llm.ollama_client import OllamaClient

STAGE_EMOJI = {"kickoff": "🎯", "decompose": "✂️", "produce": "⚒️", "review": "🔍", "revise": "🔧", "final": "🏁"}

# ---------- 提示词模板 ----------

KICKOFF_PROMPT = """你是项目架构师。我们有多个AI共同完成项目。请你作为其中一员，先给出你认同的【统一开发框架】。

项目需求原文:
{requirement}

参与的AI助手: {names}
你是: {who}

请从以下几个维度直接输出 Markdown 文档:
1. 项目目标一句话澄清
2. 建议的技术栈选型 + 理由
3. 模块拆分及职责边界(推荐3~8个模块,并说明每个模块属于谁的专业领域)
4. 目录结构草案(树状)
5. 模块间数据流/接口约定(关键的入参/出参形态)
6. 命名规范/约定(文件/变量/错误码)
7. 里程碑顺序(从V0.1到V1.0分哪些阶段)
8. 风险点(你认为最可能踩坑的2~3处)

直接输出Markdown,不要写"好的""如下"等开场白。"""


DECOMPOSE_PROMPT = """你是项目经理。根据完整需求和当前统一框架, 分解成若干子任务并指派给最合适的AI助手。
可用助手(按专业方向给你参考):
{ref}

只输出 JSON 数组, 每项格式: {{"subtask": "<一句话任务名>", "assignee": "<助手名>", "deliverable": "<具体交付物说明>", "background_note": "<给该助手的特别提醒,可空>"}}
不要输出其他文字。

项目需求原文:
{requirement}

当前统一框架 v{fv}:
{framework}"""


ASSIGN_PROMPT = """【协作总原则】你是{who},参与的全体AI为: {names}。
所有人都在同一个项目下工作, 每人负责自己的子任务, 但必须遵循【统一框架】(有版本号)。
所有交付物输出后, 会被发给全体AI评审(包括你), 评审意见会导致统一框架升级, 再让每个人修订自己的交付物。

■ 项目需求原文
{requirement}

■ 当前统一框架 v{fv} (所有AI都在这份框架下工作,若发现错误/缺漏, 最后在你的交付物末尾写【建议】区块单独列出, 不要擅自改框架内容来适配你的代码)
{framework}

■ 其他AI负责的子任务概览(让你知道别人在做什么, 接口对齐时需要)
{others}

■ 你的子任务
任务名: {subtask}
交付物要求: {deliverable}
协调员备注: {note}

■ 你的输出格式要求
1) 先输出一段"## 任务理解",用1~2句话复述你对自己职责和与其他模块接口的理解
2) 输出交付物本体(代码/文档/设计方案,按上面要求)
3) 如果统一框架和你的交付物存在矛盾, 或你需要其他模块补充接口/修改约定, 在末尾"## 【对统一框架的建议】"单独列出

直接输出Markdown,不要写"好的"开场白。"""


REVIEW_PROMPT = """你是{who}。现在是项目全体讨论会环节。
所有AI的子任务交付物都已产出。请你作为【交叉评审者】, 通读全部交付物并给出独立评审意见。
评审只针对: 1)交付物之间的冲突/矛盾  2)接口约定不匹配/缺失  3)重大遗漏/偏离统一框架  4)潜在风险
不要做重复"我觉得写得不错"这类无意义的客套话。

■ 项目需求
{requirement}

■ 统一框架 v{fv}
{framework}

■ 各AI的交付物(你负责评审,不是重写它们的产出)
{deliverables}

输出格式(严格按此结构):
## 总体打分(1~10)
X分: <一句话理由>

## 发现的问题(编号列出,每条单独列出)
1. <问题主体是哪两个模块/哪份交付物> → <具体冲突/缺漏/风险描述> → <建议修复方向>
2. ...

## 我自己交付物的修正承诺(如果你发现你自己的产出需要改,也写在这里)
- ...

没发现的地方写"暂无"。不要改动别人的交付物,只给评审意见。直接输出Markdown。"""


MERGE_REVIEW_PROMPT = """你是项目主持人(协调员)。多个AI给出了独立的交叉评审意见。
请你:
1) 把所有评审中提到的"问题"做去重合并(同样或同类问题只保留一条最清晰的描述,并注明被提到次数)
2) 给出"建议修复方向"时, 明确指定"由哪个AI负责改"(如果该问题只涉及单个AI的交付物, 指派给它; 如果是跨模块接口, 指派给涉及双方中的一方作为主责+另一方配合)
3) 统计合并后总问题数 N
4) 输出 JSON (不要写其他文字):
{{
  "total_issues": N,
  "issues": [
    {{"id": 1, "summary": "一句话问题", "description": "详细描述", "fix_by": "指派的AI名(或'all'全体修改接口规范)", "action": "一句话如何改"}},
    ...
  ]
}}

合并后的评审意见总数:
{counts}

所有评审原文(按AI分开):
{reviews}"""


REVISE_PROMPT = """你是{who}。这是项目修订轮。
上一轮你交付了一个产出, 然后全体AI开了讨论会, 协调员合并出了N条问题清单。请你据此修订你的交付物。

【协作原则】
- 统一框架已由协调员根据上轮评审升级到了新版本(v{fv}), 以它为准
- 只有被指派给你(或主责/配合含你)的问题, 你需要改自己的交付物
- 其他问题(属于别人的), 你只需确保自己的接口/约束和修复方向一致, 不必替别人改

■ 项目需求
{requirement}

■ 统一框架 v{fv}
{framework}

■ 你上一轮的交付物
{last}

■ 本轮总问题清单(所有问题都在这里, 你只改跟你有关的条目)
{issues}

■ 你的输出
1) 先输出"## 修订说明": 列出你采纳并修改了哪些问题(只列你负责的), 编号引用上面清单的id
2) 然后输出【修订后的完整交付物】(重新整份输出, 不要只输出diff)
3) 末尾"## 【遗留/风险】": 如果你认为有问题无法按现有信息解决, 写在这里"""


UPDATE_FRAMEWORK_PROMPT = """你是项目协调员。当前统一框架 v{old_fv}。
多个AI交叉评审后合并出了以下问题清单, 请你根据这些问题把统一框架升级为新版本 v{new_fv}。
修改原则:
- 只改被问题影响到的章节(模块拆分/接口/目录/命名/里程碑), 不要重写全文
- 对于模块责任归属变化/接口新增或修改, 要明确写入(这就是让大家对齐的依据)
- 末尾追加一个"## v{new_fv}变更记录"小节, 列清楚你这次改了什么

旧框架全文:
{old_framework}

问题清单(每条都标了fix_by):
{issues}

直接输出新版完整 Markdown 框架, 不要写其他说明。"""


# ---------- 数据结构 ----------

@dataclass
class Subtask:
    subtask: str
    assignee: str
    deliverable: str
    background_note: str = ""


@dataclass
class Issue:
    id: int
    summary: str
    description: str
    fix_by: str
    action: str


@dataclass
class PipelineResult:
    requirement: str
    framework: str            # 最终版统一框架全文
    framework_version: int
    deliverables: dict[str, str]  # agent -> 最终交付物
    review_rounds: int
    final_issue_count: int
    log: list[dict] = field(default_factory=list)

    def to_markdown(self) -> str:
        md = [f"# 项目产出 — {self.requirement[:40]}",
              f"- 统一框架版本: v{self.framework_version}",
              f"- 讨论会循环轮次: {self.review_rounds}",
              f"- 收敛时剩余问题数: {self.final_issue_count}",
              "",
              "---",
              "## 统一框架 v%d\n\n" % self.framework_version + self.framework,
              ""]
        for agent, body in self.deliverables.items():
            md.append(f"## 交付物 — {agent}\n\n{body}\n")
        return "\n".join(md)


# ---------- 主 orchestrator 混入 ----------

class PipelineEngine:
    def __init__(self, orchestrator, settings: dict, registry: Registry, pool: BrowserPool, coordinator):
        self.orch = orchestrator
        self.settings = settings
        self.registry = registry
        self.pool = pool
        self.coord = coordinator          # OllamaClient | CloudClient
        self.log = SessionLog(settings.get("log_file", "./logs/session.jsonl"))
        pl = settings.get("pipeline", {})
        self.max_rounds = pl.get("max_rounds", 3)         # 最大讨论会循环轮次
        self.converge_under = pl.get("converge_under", 2) # 问题数<=此值即收敛
        self.progress_cb = pl.get("progress_cb")          # 可选回调 fn(stage, payload)

    # ---------- 工具: 并发模型 ----------

    async def _group_concurrent(self, names: list[str], worker):
        """按 group 分桶, 每桶独立串行worker, 桶间并行。return {name: result}

        为什么不 asyncio.gather 全并发? 同一 BrowserContext 下Playwright是单线程的,
        gather(同一实例的多个标签页) 实际上还是轮询切, 而且更容易被 DOM 状态互相踩。
        只有组间(normal vs private)是两个独立 Firefox 进程, 真正并行。
        """
        buckets: dict[str, list[str]] = {}
        for n in names:
            g = self.registry.get(n).group
            buckets.setdefault(g, []).append(n)

        async def bucket_worker(group_name: str, group_names: list[str]):
            out = {}
            for n in group_names:
                out[n] = await worker(n)
            return group_name, out

        results = await asyncio.gather(*(bucket_worker(g, ns) for g, ns in buckets.items()))
        merged = {}
        for _, bucket_out in results:
            merged.update(bucket_out)
        return merged

    def _progress(self, stage: str, payload: dict):
        if self.progress_cb:
            try:
                self.progress_cb(stage, payload)
            except Exception:
                pass
        short = payload.pop("__short__", None)
        print(f"[pipeline · {STAGE_EMOJI.get(stage, '·')} {stage}]", short or "")

    # ---------- P1 Kickoff ----------

    async def kickoff(self, requirement: str, names: list[str]) -> str:
        self._progress("kickoff", {"__short__": f"统一框架讨论, 参与={len(names)}模型"})

        async def _kick(who: str) -> Reply:
            return await self.orch.ask_one(who, KICKOFF_PROMPT.format(
                requirement=requirement, names=", ".join(names), who=who,
            ))

        kick_replies = await self._group_concurrent(names, _kick)

        # 协调员合并为 v0.1 统一框架
        answers_block = "\n\n".join(
            f"## {n}\n{r.text if r.ok else f'(失败: {r.error})'}"
            for n, r in kick_replies.items()
        )
        prompt = (
            "你是项目主持人。多个AI给出了各自对统一框架的看法。\n"
            "请把它们合并成一份综合版本：冲突处采用更合理的；缺失的维度补上；\n"
            "保持8个章节的结构(项目目标/技术栈/模块拆分/目录/接口约定/命名规范/里程碑/风险)。\n\n"
            f"# 项目需求\n{requirement}\n\n# 各AI的框架草案\n{answers_block}\n\n直接输出最终Markdown。"
        )
        framework_v1 = await self.coord.chat(prompt, system="输出一份完整的Markdown文档, 用中文。") or answers_block
        self.log.write({"kind": "pipeline", "phase": "kickoff",
                        "framework_v1_length": len(framework_v1)})
        return framework_v1

    # ---------- P2 Decompose ----------

    async def decompose(self, requirement: str, framework: str, framework_v: int, names: list[str]) -> list[Subtask]:
        ref_lines = []
        for n in names:
            ref_lines.append(f"- {n} ({self.registry.get(n).info or '综合'})")
        ref = "\n".join(ref_lines)
        self._progress("decompose", {"__short__": f"协调员分解子任务(指派给{len(names)}模型)"})

        raw = await self.coord.chat(
            DECOMPOSE_PROMPT.format(ref=ref, requirement=requirement,
                                    framework=framework, fv=framework_v),
            system="只输出合法JSON数组。不要解释。",
        )
        try:
            js = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
            tasks = []
            for item in js:
                tasks.append(Subtask(
                    subtask=str(item.get("subtask", "")).strip(),
                    assignee=str(item.get("assignee", names[0])).strip(),
                    deliverable=str(item.get("deliverable", "")).strip(),
                    background_note=str(item.get("background_note", "")).strip(),
                ))
            # 指派矫正: 不在names里的指派→拉回可用模型
            for t in tasks:
                if t.assignee not in names:
                    t.assignee = names[0]
            # 至少保证一个子任务
            if not tasks:
                tasks.append(Subtask("整体设计", names[0], "输出完整方案文档", ""))
            return tasks
        except (ValueError, json.JSONDecodeError, IndexError):
            # 兜底: 每个模型一份"出方案"子任务
            return [Subtask(f"方案({n})", n, f"从你的专业视角给出完整方案: {requirement}", "")
                    for n in names]

    # ---------- P3 Produce (带 4 段式 prompt) ----------

    async def produce(self, requirement: str, framework: str, framework_v: int,
                      tasks: list[Subtask], revision_hint: dict | None = None) -> dict[str, str]:
        """revision_hint={agent: (last_text, [负责的Issue])} 为非空时走修订提示词"""
        names = [t.assignee for t in tasks]
        self._progress("produce", {"__short__": (
            f"修订·子任务{len(tasks)}个" if revision_hint else f"子任务并行产出 {len(tasks)}个"
        )})

        other_by_agent: dict[str, str] = {}
        for t in tasks:
            others = [f"- {o.assignee} 负责《{o.subtask}》, 交付: {o.deliverable}"
                      for o in tasks if o.assignee != t.assignee]
            other_by_agent[t.assignee] = "\n".join(others) or "（你是唯一参与者）"

        task_by_agent = {t.assignee: t for t in tasks}

        async def _worker(who: str) -> Reply:
            t = task_by_agent[who]
            if revision_hint and who in revision_hint:
                last_text, my_issues = revision_hint[who]
                issues_block = "\n".join(
                    f"- #{i.id} [{i.fix_by}] {i.summary}: {i.description} → {i.action}"
                    for i in my_issues
                ) or "（本次无指派给你的问题, 你只需基于新框架做格式一致性检查并重新输出整份交付物）"
                prompt = REVISE_PROMPT.format(
                    who=who, fv=framework_v, requirement=requirement,
                    framework=framework, last=last_text, issues=issues_block,
                )
            else:
                prompt = ASSIGN_PROMPT.format(
                    who=who, names=", ".join(names), requirement=requirement,
                    fv=framework_v, framework=framework,
                    others=other_by_agent[who], subtask=t.subtask,
                    deliverable=t.deliverable, note=t.background_note or "无",
                )
            return await self.orch.ask_one(who, prompt)

        replies = await self._group_concurrent(names, _worker)
        out = {}
        for who, r in replies.items():
            out[who] = r.text if r.ok else f"(失败 {r.error})"
        return out

    # ---------- P4 Review (全体交叉评审) ----------

    async def review_all(self, requirement: str, framework: str, framework_v: int,
                         deliverables: dict[str, str]) -> tuple[list[Issue], dict[str, str]]:
        names = list(deliverables.keys())
        self._progress("review", {"__short__": f"全体交叉评审 · {len(names)}模型"})
        d_block = "\n\n".join(f"## 交付物 — {n}\n{body}" for n, body in deliverables.items())

        async def _r(who: str) -> Reply:
            return await self.orch.ask_one(who, REVIEW_PROMPT.format(
                who=who, requirement=requirement, fv=framework_v,
                framework=framework, deliverables=d_block,
            ))

        reviews_reply = await self._group_concurrent(names, _r)
        reviews_text = {n: (r.text if r.ok else f"(失败 {r.error})") for n, r in reviews_reply.items()}

        # 协调员合并问题
        counts = "\n".join(f"- {n}: {len(t)}字符" for n, t in reviews_text.items())
        reviews_block = "\n\n".join(f"## {n}\n{t}" for n, t in reviews_text.items())
        raw = await self.coord.chat(
            MERGE_REVIEW_PROMPT.format(counts=counts, reviews=reviews_block),
            system="只输出合法JSON对象。不要写其他文字。",
        )
        issues: list[Issue] = []
        try:
            js = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
            for item in js.get("issues", []):
                issues.append(Issue(
                    id=int(item.get("id", 0) or len(issues) + 1),
                    summary=str(item.get("summary", "")).strip(),
                    description=str(item.get("description", "")).strip(),
                    fix_by=str(item.get("fix_by", "all")).strip(),
                    action=str(item.get("action", "")).strip(),
                ))
        except (ValueError, json.JSONDecodeError, IndexError):
            # 兜底: 直接把评审原文作为 1 条 issue
            issues.append(Issue(id=1, summary="协调员合并失败,请人工检查评审原文",
                                description=reviews_block[:800], fix_by="all", action="重跑"))
        return issues, reviews_text

    # ---------- 框架升级 ----------

    async def update_framework(self, old_framework: str, old_fv: int, new_fv: int,
                               issues: list[Issue]) -> str:
        issues_block = "\n".join(
            f"- #{i.id} fix_by={i.fix_by} | {i.summary} | 修复方向: {i.action}"
            for i in issues
        ) or "（无问题）"
        prompt = UPDATE_FRAMEWORK_PROMPT.format(
            old_fv=old_fv, new_fv=new_fv, old_framework=old_framework, issues=issues_block,
        )
        new_fw = await self.coord.chat(prompt, system="输出完整Markdown, 中文。")
        if not new_fw or len(new_fw) < 100:
            # 协调员没写, 手动加版本号段落兜底
            new_fw = old_framework + f"\n\n## v{new_fv} 变更记录\n" + issues_block
        return new_fw

    # ---------- 入口 ----------

    async def run(self, requirement: str, names: list[str],
                  project_dir: str | None = None) -> PipelineResult:
        names = names or self.registry.names()
        pdir = project_dir
        if pdir:
            os.makedirs(pdir, exist_ok=True)

        # PHASE1 框架讨论
        framework = await self.kickoff(requirement, names)
        fv = 1
        self._dump(pdir, f"framework_v{fv:02d}.md", framework)

        # PHASE2 任务分解
        tasks = await self.decompose(requirement, framework, fv, names)
        self._dump(pdir, "tasks.json",
                   json.dumps([asdict(t) for t in tasks], ensure_ascii=False, indent=2))

        # 循环: P3 -> P4 -> (收敛?) -> (P5 修订+框架升级) -> 回到 P3
        deliverables: dict[str, str] = {}
        revision_hint: dict | None = None
        round_idx = 0
        last_issues: list[Issue] = []
        while True:
            round_idx += 1
            # PHASE3 产出/修订
            deliverables = await self.produce(requirement, framework, fv, tasks, revision_hint)
            self._dump_deliverables(pdir, round_idx, deliverables)

            # PHASE4 全体评审
            issues, reviews = await self.review_all(requirement, framework, fv, deliverables)
            last_issues = issues
            self._dump(pdir, f"round_{round_idx:02d}_reviews.md",
                       "\n\n".join(f"## {n}\n{r}" for n, r in reviews.items()))
            self._dump(pdir, f"round_{round_idx:02d}_issues.json",
                       json.dumps([asdict(i) for i in issues], ensure_ascii=False, indent=2))

            self._progress("review", {"__short__": f"合并出 {len(issues)} 条问题"})
            # 收敛判断
            if len(issues) <= self.converge_under or round_idx >= self.max_rounds:
                break

            # PHASE5 框架升级 + 构造修订提示
            new_fv = fv + 1
            framework = await self.update_framework(framework, fv, new_fv, issues)
            fv = new_fv
            self._dump(pdir, f"framework_v{fv:02d}.md", framework)
            self._progress("revise", {
                "__short__": f"统一框架升到v{fv}, 准备修订·剩{len(issues)}条问题"
            })

            # 每个 agent 算出自己负责的 issue
            revision_hint = {}
            for t in tasks:
                who = t.assignee
                my_issues = [i for i in issues
                             if i.fix_by == who or i.fix_by == "all"
                             or (isinstance(i.fix_by, str) and who in i.fix_by)]
                revision_hint[who] = (deliverables.get(who, ""), my_issues)

        # 最终
        self._progress("final", {
            "__short__": f"收敛·循环{round_idx}轮·v{fv}·剩{len(last_issues)}条问题"
        })
        result = PipelineResult(
            requirement=requirement,
            framework=framework,
            framework_version=fv,
            deliverables=deliverables,
            review_rounds=round_idx,
            final_issue_count=len(last_issues),
        )
        self._dump(pdir, "final.md", result.to_markdown())
        return result

    # ---------- 落盘 ----------

    def _dump(self, pdir: str | None, name: str, body: str):
        if not pdir:
            return
        with open(os.path.join(pdir, name), "w", encoding="utf-8") as f:
            f.write(body)

    def _dump_deliverables(self, pdir: str | None, rnd: int, deliverables: dict[str, str]):
        if not pdir:
            return
        d = os.path.join(pdir, f"round_{rnd:02d}_deliverables")
        os.makedirs(d, exist_ok=True)
        for agent, body in deliverables.items():
            with open(os.path.join(d, f"{agent}.md"), "w", encoding="utf-8") as f:
                f.write(body)
