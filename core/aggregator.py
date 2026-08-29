"""汇总器：把多个网页模型的回复合成一份结论"""
from core.models import Reply
from local_llm.ollama_client import OllamaClient

MERGE_PROMPT = """你是项目技术负责人。以下是多个 AI 助手对同一任务的不同回答，请：
1) 提取各回答中互相一致的结论
2) 指出冲突点并给出你采纳的理由
3) 输出一份合并后的最终方案（用 Markdown）

## 任务
{task}

## 各助手回答
{answers}"""


def plain_merge(task: str, replies: dict[str, Reply]) -> str:
    """Ollama 不可用时的兜底：按原样拼接"""
    parts = [f"# 任务\n{task}\n"]
    for name, r in replies.items():
        body = r.text if r.ok else f"(失败: {r.error})"
        parts.append(f"## {name}\n{body}\n")
    return "\n".join(parts)


async def summarize(task: str, replies: dict[str, Reply], ollama: OllamaClient) -> str:
    if not ollama.available():
        return plain_merge(task, replies)
    answers = "\n\n".join(
        f"### {n}\n{r.text if r.ok else '(失败: ' + r.error + ')'}"
        for n, r in replies.items()
    )
    prompt = MERGE_PROMPT.format(task=task, answers=answers)
    text = await ollama.chat(prompt, system="用中文回答，输出 Markdown。")
    return text or plain_merge(task, replies)
