"""项目会话：跨命令的多轮迭代上下文

目录结构:
  projects/<项目名>/
    project.json          # 元数据: 需求/轮次/当前权威版本指针
    rounds/round_NN_<模型>.md   # 每轮产出全文

多轮原理: 每轮发送前组装 prompt = 需求 + 当前权威版本产出(裁剪) + 历史轮次摘要
          + 本轮指示。模型每次拿到完整项目记忆, 等效于多轮对话,
          且不依赖各站点自身会话存活(每次新会话也无妨)。

隐私: projects/ 整体在 .gitignore 红线内, 不会上传 GitHub。
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_DIR = os.path.join(ROOT, "projects")

# 注入 prompt 的预算(字符): 当前产出全文 + 历史摘要
CURRENT_BUDGET = 9000
HISTORY_BUDGET = 2500


def clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...(已截断, 全文见项目文件)"


class Project:
    def __init__(self, name: str, data: dict | None = None):
        self.name = name
        self.dir = os.path.join(PROJECTS_DIR, name)
        self.meta_path = os.path.join(self.dir, "project.json")
        self.data = data or {
            "name": name,
            "requirement": "",
            "rounds": [],       # [{n, agent, instruction, file, ts, summary}]
            "current": 0,       # 当前权威轮次号; 0=尚无
        }

    # ---------- 存取 ----------
    @staticmethod
    def exists(name: str) -> bool:
        return os.path.exists(os.path.join(PROJECTS_DIR, name, "project.json"))

    @staticmethod
    def list_all() -> list[str]:
        if not os.path.isdir(PROJECTS_DIR):
            return []
        return sorted(
            d for d in os.listdir(PROJECTS_DIR)
            if os.path.exists(os.path.join(PROJECTS_DIR, d, "project.json"))
        )

    def save(self):
        os.makedirs(self.rounds_dir, exist_ok=True)
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, name: str) -> "Project":
        path = os.path.join(PROJECTS_DIR, name, "project.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"项目不存在: {name} (先用 project new 创建)")
        with open(path, encoding="utf-8") as f:
            return cls(name, json.load(f))

    @property
    def rounds_dir(self) -> str:
        return os.path.join(self.dir, "rounds")

    # ---------- 生命周期 ----------
    @classmethod
    def create(cls, name: str, requirement: str) -> "Project":
        if cls.exists(name):
            raise FileExistsError(f"项目已存在: {name}")
        p = cls(name)
        p.data["requirement"] = requirement
        p.save()
        return p

    def current_text(self) -> str:
        r = self.current_round()
        if not r:
            return ""
        with open(os.path.join(self.rounds_dir, r["file"]), encoding="utf-8") as f:
            return f.read()

    def current_round(self) -> dict | None:
        n = self.data["current"]
        return self.round(n) if n else None

    def round(self, n: int) -> dict | None:
        return next((r for r in self.data["rounds"] if r["n"] == n), None)

    # ---------- 轮次操作 ----------
    def record(self, agent: str, instruction: str, reply_text: str) -> dict:
        n = len(self.data["rounds"]) + 1
        fname = f"round_{n:02d}_{agent}.md"
        with open(os.path.join(self.rounds_dir, fname), "w", encoding="utf-8") as f:
            f.write(reply_text)
        r = {
            "n": n, "agent": agent, "instruction": instruction,
            "file": fname, "ts": time.strftime("%m-%d %H:%M"),
            "summary": clip(reply_text, 120),
        }
        self.data["rounds"].append(r)
        self.data["current"] = n  # 最新产出自动成为当前版本
        self.save()
        return r

    def pick(self, n: int) -> dict:
        r = self.round(n)
        if not r:
            raise ValueError(f"轮次 {n} 不存在")
        self.data["current"] = n
        self.save()
        return r

    # ---------- prompt 组装 ----------
    def build_prompt(self, instruction: str, multi_agent: list[str] | None = None) -> str:
        """组装 prompt。
        multi_agent 非空时：表示这一轮是"多模型并行"，我们在每个模型的 prompt 里
        写清项目框架+大家都在做什么，解决用户提的"分配时不携带背景"问题。
        """
        parts = [f"【项目需求】\n{self.data['requirement']}"]

        # 统一框架（如果有，每个模型都必须看到）
        fw_path = os.path.join(self.dir, "framework.md")
        if os.path.exists(fw_path):
            try:
                with open(fw_path, encoding="utf-8") as f:
                    fw = f.read()
                # 加版本号(读项目元数据里的framework_version)
                fv = self.data.get("framework_version", 1)
                parts.append(f"\n【统一框架 v{fv}】(所有AI必须以此为准，冲突写建议区)\n{clip(fw, 8000)}")
            except OSError:
                pass

        cur = self.current_round()
        if cur:
            parts.append(
                f"\n【当前权威版本·第{cur['n']}轮·来自{cur['agent']}】\n"
                f"当时指示: {cur['instruction']}\n"
                f"{clip(self.current_text(), CURRENT_BUDGET)}"
            )

        # 多模型并发时：列清楚每个人都在做什么(历史产出+本轮指派)
        if multi_agent:
            others = [a for a in multi_agent]
            overview = []
            for a in others:
                # 最近一条该模型的产出轮次
                latest = next((r for r in reversed(self.data["rounds"]) if r["agent"] == a), None)
                if latest:
                    overview.append(f"- {a}: 第{latest['n']}轮做了 《{clip(latest['instruction'],80)}》→ {latest['summary']}")
                else:
                    overview.append(f"- {a}: (暂无历史产出)")
            parts.append("\n【本轮参与的AI及各自近况】\n" + "\n".join(overview) +
                          "\n注意：你和他们都在为同一个项目协作，接口/命名要和统一框架保持一致。")

        hist = [r for r in self.data["rounds"] if not cur or r["n"] != cur["n"]]
        if hist:
            lines = [f"- 第{r['n']}轮[{r['agent']}] {clip(r['instruction'], 80)}" for r in hist[-8:]]
            parts.append("\n【历史轮次(仅列提要)】\n" + "\n".join(lines))
        parts.append(
            f"\n【本轮指示·第{len(self.data['rounds']) + 1}轮】\n{instruction}\n\n"
            f"请基于以上项目背景完成本轮指示, 直接输出结果内容本身。"
            f"如发现统一框架与实际需求冲突，在文末『## 对统一框架的建议』单独列出。"
        )
        return "\n".join(parts)

    def status_lines(self) -> list[str]:
        cur = self.data["current"]
        lines = [f"项目: {self.name}", f"需求: {clip(self.data['requirement'], 100)}"]
        lines.append(f"当前版本: 第{cur}轮" if cur else "当前版本: 尚无产出")
        for r in self.data["rounds"]:
            mark = " ★" if r["n"] == cur else ""
            lines.append(f"  [{r['n']:02d}] {r['ts']} {r['agent']}: {clip(r['instruction'], 60)}{mark}")
        return lines
